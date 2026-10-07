"""Account erasure: DELETE /auth/profile handled as anonymisation (NDPA erasure request).

The person's data is wiped and the account is closed, but the rows other records depend on stay:
bookings, caregiver ledger, consents (append-only), support tickets, vitals and prescriptions are kept
for legal and audit retention. Nothing here commits; the endpoint owns the transaction.

Two phases: ``check`` refuses (ErasureBlocked) while the person has work in flight or money owed, then
``erase`` does the wiping. Everything runs under a lock on the user row and, for a caregiver, the wallet
row, so a withdrawal cannot slip in between the balance check and the wipe.
"""

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import get_password_hash
from app.db.models.booking import Booking, BookingStatus, CaregiverLocation, PaymentStatus
from app.db.models.care import CareRecipient, CareRelationship
from app.db.models.caregiver import (
    Caregiver, CaregiverBankAccount, CaregiverCertification, CaregiverDocument, CaregiverTransaction,
    CaregiverWallet,
)
from app.db.models.consent import Consent
from app.db.models.patient import Patient, PatientFamilyMember, PatientWallet
from app.db.models.push_token import PushToken
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppContact, WhatsAppConversation

logger = logging.getLogger(__name__)

ERASED_NAME = "Deleted user"
ERASURE_CHANNEL = "account_erasure"
ERASURE_CONSENT_TYPES = ("terms", "health_processing", "marketing")
# A caregiver is on the hook for these; a paid booking still waiting for its visit is too.
CAREGIVER_ACTIVE = (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS)
PATIENT_ACTIVE = (BookingStatus.PENDING,) + CAREGIVER_ACTIVE


class ErasureBlocked(Exception):
    """The account cannot be erased yet. ``str(exc)`` is safe to show the person."""


@dataclass
class ErasureResult:
    profile_image_url: Optional[str]  # to delete from Cloudinary once the transaction has committed


def erased_email() -> str:
    # example.com is reserved (RFC 2606) so nothing is delivered, and it passes EmailStr, which the
    # response models use: an "@invalid" address would make any API that returns this user fail.
    return f"deleted+{uuid.uuid4().hex}@example.com"


def _erased_wa_phone() -> str:
    # Country code 999 is unassigned, and the contacts table requires an E.164 shape.
    return "+999" + f"{uuid.uuid4().int % 10**11:011d}"


class AccountErasureService:
    def __init__(self, db: Session):
        self.db = db

    def erase(self, user_id: uuid.UUID, now: Optional[datetime] = None) -> ErasureResult:
        now = now or datetime.utcnow()
        user = (
            self.db.query(User).filter(User.id == user_id).populate_existing().with_for_update().one()
        )
        caregiver = self.db.query(Caregiver).filter(Caregiver.user_id == user.id).first()
        patient = self.db.query(Patient).filter(Patient.user_id == user.id).first()
        self._check(user, caregiver, patient)

        email = erased_email()
        image_url = user.profile_image_url
        self._wipe_user(user, email, now)
        if caregiver is not None:
            self._wipe_caregiver(caregiver, email)
        if patient is not None:
            self._wipe_patient(patient, email)
        self._wipe_care_recipients(user)
        self.db.query(PushToken).filter(PushToken.user_id == user.id).delete(synchronize_session=False)
        self._withdraw_whatsapp(user, now)
        self._record_withdrawal(user)
        self.db.flush()
        logger.info("account erased user=%s type=%s", user.id, user.user_type)
        return ErasureResult(profile_image_url=image_url)

    # --- refusals -----------------------------------------------------------------------------------
    def _check(self, user: User, caregiver: Optional[Caregiver], patient: Optional[Patient]) -> None:
        if caregiver is not None:
            self._check_caregiver(caregiver)
        if patient is not None:
            self._check_patient(user, patient)

    def _check_caregiver(self, caregiver: Caregiver) -> None:
        active = (
            self.db.query(Booking.id)
            .filter(Booking.caregiver_id == caregiver.id)
            .filter(or_(
                Booking.status.in_(CAREGIVER_ACTIVE),
                (Booking.status == BookingStatus.PENDING) & (Booking.payment_status == PaymentStatus.PAID),
            ))
            .first()
        )
        if active is not None:
            raise ErasureBlocked(
                "You have a booking that is not finished yet. Complete it, or cancel it from the "
                "booking screen, then delete your account."
            )
        wallet = (
            self.db.query(CaregiverWallet).filter(CaregiverWallet.caregiver_id == caregiver.id)
            .populate_existing().with_for_update().first()
        )
        if wallet is not None and (wallet.balance or 0) > 0:
            raise ErasureBlocked(
                "Your wallet still has a balance. Withdraw it to your bank account, then delete your account."
            )
        pending_payout = (
            self.db.query(CaregiverTransaction.id)
            .filter(CaregiverTransaction.caregiver_id == caregiver.id,
                    CaregiverTransaction.type == "withdrawal", CaregiverTransaction.status == "pending")
            .first()
        )
        if pending_payout is not None:
            raise ErasureBlocked(
                "You have a withdrawal that is still being paid out. Wait for the payout to finish, "
                "then delete your account."
            )

    def _check_patient(self, user: User, patient: Patient) -> None:
        active = (
            self.db.query(Booking.id)
            .filter(or_(Booking.patient_id == patient.id, Booking.booked_by_user_id == user.id))
            .filter(Booking.status.in_(PATIENT_ACTIVE), Booking.payment_status == PaymentStatus.PAID)
            .first()
        )
        if active is not None:
            raise ErasureBlocked(
                "You have a paid booking that is not finished yet. Wait for the visit to complete, or "
                "cancel the booking, then delete your account."
            )
        wallet = (
            self.db.query(PatientWallet).filter(PatientWallet.patient_id == patient.id)
            .populate_existing().with_for_update().first()
        )
        if wallet is not None and (wallet.balance_kobo or 0) > 0:
            raise ErasureBlocked(
                "Your wallet still has a balance. Contact support to have it refunded, then delete "
                "your account."
            )

    # --- wiping -------------------------------------------------------------------------------------
    def _wipe_user(self, user: User, email: str, now: datetime) -> None:
        user.full_name = ERASED_NAME
        user.email = email
        user.phone_number = None
        user.profile_image_url = None
        # An unusable password: nothing can match it, and the email it signed in with is gone anyway.
        user.hashed_password = get_password_hash(secrets.token_urlsafe(32))
        user.is_active = False  # get_current_user, login and refresh all refuse an inactive user
        user.is_verified = False
        user.verification_code = None
        user.verification_code_expires_at = None
        user.password_reset_token = None
        user.password_reset_expires_at = None
        user.updated_at = now

    def _wipe_caregiver(self, cg: Caregiver, email: str) -> None:
        cg.full_name, cg.email, cg.phone_number = ERASED_NAME, email, None
        for field in ("date_of_birth", "gender", "state", "address", "emergency_contact_phone",
                      "emergency_contact_name", "emergency_contact_address", "onfido_applicant_id",
                      "onfido_check_id", "document_verification_result", "face_verification_result",
                      "face_verification_status"):
            setattr(cg, field, None)
        for doc in self.db.query(CaregiverDocument).filter(CaregiverDocument.caregiver_id == cg.id):
            doc.document_name, doc.file_path, doc.extraction_data = "erased", "erased", None
        for cert in self.db.query(CaregiverCertification).filter(CaregiverCertification.caregiver_id == cg.id):
            cert.certification_number, cert.file_path = None, None
        self.db.query(CaregiverLocation).filter(CaregiverLocation.caregiver_id == cg.id).delete(
            synchronize_session=False)  # also takes them out of matching
        self.db.query(CaregiverBankAccount).filter(CaregiverBankAccount.caregiver_id == cg.id).delete(
            synchronize_session=False)
        # The ledger stays (amounts, status, reference); the payout destination is masked to the last 4.
        for tx in self.db.query(CaregiverTransaction).filter(CaregiverTransaction.caregiver_id == cg.id):
            if tx.account_number:
                tx.account_number = "*" * 6 + tx.account_number[-4:]
            tx.account_name = None

    def _wipe_patient(self, patient: Patient, email: str) -> None:
        patient.full_name, patient.email, patient.phone_number = ERASED_NAME, email, None
        for field in ("date_of_birth", "gender", "address", "city", "state", "postal_code",
                      "emergency_contact_name", "emergency_contact_phone", "emergency_contact_relationship",
                      "emergency_contact_address", "blood_group", "allergies", "medical_conditions",
                      "medications", "special_requirements"):
            setattr(patient, field, None)
        patient.is_active = False
        for member in self.db.query(PatientFamilyMember).filter(PatientFamilyMember.patient_id == patient.id):
            member.full_name, member.phone_number, member.email = ERASED_NAME, None, None

    def _wipe_care_recipients(self, user: User) -> None:
        """The recipient row that stands for the person themselves (role 'self'). Recipients they booked
        for are other people's data and the bookings that name them are kept, so they are left alone."""
        rows = (
            self.db.query(CareRecipient)
            .join(CareRelationship, CareRelationship.recipient_id == CareRecipient.id)
            .filter(CareRelationship.contact_user_id == user.id, CareRelationship.role == "self")
        )
        for rec in rows:
            rec.full_name = ERASED_NAME
            for field in ("date_of_birth", "gender", "phone_e164", "address", "city", "state", "lat", "lng",
                          "notes_encrypted"):
                setattr(rec, field, None)

    def _withdraw_whatsapp(self, user: User, now: datetime) -> None:
        """Unlink and anonymise the person's WhatsApp contact the way STOP DATA does (consents withdrawn,
        drafts closed, opted out, a compliance task opened), then free the number and the wa_id."""
        from app.channels.whatsapp import withdrawal
        from app.channels.whatsapp.engine import State

        contacts = (
            self.db.query(WhatsAppContact).filter(WhatsAppContact.user_id == user.id)
            .populate_existing().with_for_update().all()
        )
        for contact in contacts:
            withdrawal.stop_data(self.db, contact, evidence_ref=f"erasure:{user.id}", now=now,
                                 channel=ERASURE_CHANNEL)
            conversation = (
                self.db.query(WhatsAppConversation).filter_by(contact_id=contact.id)
                .populate_existing().with_for_update().first()
            )
            if conversation is not None:
                conversation.state = State.DECLINED.value
                conversation.context = {}
                conversation.automation_paused = False
                conversation.assigned_agent_id = None
                conversation.updated_at = now
            contact.user_id = None
            contact.display_name = None
            contact.wa_id = f"deleted-{uuid.uuid4().hex}"
            contact.phone_e164 = _erased_wa_phone()

    def _record_withdrawal(self, user: User) -> None:
        """The erasure itself, as consent rows: who (the user), when (created_at). Append-only."""
        for consent_type in ERASURE_CONSENT_TYPES:
            self.db.add(Consent(
                subject_user_id=user.id, granted_by_user_id=user.id, type=consent_type,
                decision="withdrawn", policy_version=settings.WHATSAPP_POLICY_VERSION,
                channel=ERASURE_CHANNEL, evidence_ref=f"erasure:{user.id}",
            ))
