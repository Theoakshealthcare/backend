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

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.phone import normalize_e164, phone_variants
from app.core.security import get_password_hash
from app.db.models.booking import (
    Booking, BookingStatus, CaregiverLocation, ChatMessage, PaymentStatus,
)
from app.db.models.care import CareRecipient, CareRelationship
from app.db.models.caregiver import (
    Caregiver, CaregiverBankAccount, CaregiverCertification, CaregiverDocument, CaregiverTransaction,
    CaregiverWallet,
)
from app.db.models.consent import Consent
from app.db.models.patient import (
    Patient, PatientFamilyMember, PatientRefundRequest, PatientTransaction, PatientWallet,
)
from app.db.models.patient_caregiver_access import PatientCaregiverAccess
from app.db.models.push_token import PushToken
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppContact, WhatsAppConversation

logger = logging.getLogger(__name__)

ERASED_NAME = "Deleted user"
ERASURE_CHANNEL = "account_erasure"
CANCEL_REASON_ERASED = "account_erased"
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
        phones = {p for p in (normalize_e164(x.phone_number) for x in (user, caregiver, patient) if x is not None
                              and x.phone_number) if p}  # captured before the wipe clears them
        self._check(user, caregiver, patient)
        if patient is not None:
            self._close_open_bookings(user, patient, now)

        email = erased_email()
        image_url = user.profile_image_url
        self._wipe_user(user, email, now)
        if caregiver is not None:
            self._wipe_caregiver(caregiver, email)
        if patient is not None:
            self._wipe_patient(patient, email)
        self._wipe_care_recipients(user)
        self.db.query(PushToken).filter(PushToken.user_id == user.id).delete(synchronize_session=False)
        self._withdraw_whatsapp(user, phones, now)
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

    def _active_caregiver_bookings(self, caregiver: Caregiver, lock: bool):
        query = (
            self.db.query(Booking)
            .filter(Booking.caregiver_id == caregiver.id)
            .filter(or_(
                Booking.status.in_(CAREGIVER_ACTIVE),
                (Booking.status == BookingStatus.PENDING) & (Booking.payment_status == PaymentStatus.PAID),
            ))
        )
        if lock:
            query = query.with_for_update().populate_existing()
        return query.first()

    def _check_caregiver(self, caregiver: Caregiver) -> None:
        blocked = ErasureBlocked(
            "You have a booking that is not finished yet. Complete it, or cancel it from the "
            "booking screen, then delete your account."
        )
        # Same order as accepting an offer: bookings first, then the caregiver row. An accept that is in
        # flight holds the caregiver row, so taking it here waits for that accept to commit; the second
        # look then sees the booking it created.
        if self._active_caregiver_bookings(caregiver, lock=True) is not None:
            raise blocked
        self.db.query(Caregiver).filter(Caregiver.id == caregiver.id).populate_existing().with_for_update().one()
        if self._active_caregiver_bookings(caregiver, lock=True) is not None:
            raise blocked
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

    def _patient_open_bookings(self, user: User, patient: Patient):
        return (
            self.db.query(Booking)
            .filter(or_(Booking.patient_id == patient.id, Booking.booked_by_user_id == user.id))
            .filter(Booking.status.in_(PATIENT_ACTIVE))
            .with_for_update().populate_existing().all()
        )

    def _check_patient(self, user: User, patient: Patient) -> None:
        if any(b.payment_status == PaymentStatus.PAID for b in self._patient_open_bookings(user, patient)):
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
        # Money on its way into or out of the wallet would land in an erased account.
        pending_payment = (
            self.db.query(PatientTransaction.id)
            .filter(PatientTransaction.patient_id == patient.id, PatientTransaction.status == "pending").first()
        )
        if pending_payment is not None:
            raise ErasureBlocked(
                "You have a payment that is still being processed. Wait for it to finish, then delete your account."
            )
        open_refund = (
            self.db.query(PatientRefundRequest.id)
            .filter(PatientRefundRequest.patient_id == patient.id,
                    PatientRefundRequest.status.in_(("pending", "processing"))).first()
        )
        if open_refund is not None:
            raise ErasureBlocked(
                "You have a refund that is still being processed. Wait for it to finish, then delete your account."
            )

    def _close_open_bookings(self, user: User, patient: Patient, now: datetime) -> None:
        """Unpaid bookings are cancelled so a payment link paid later takes the existing refund path
        (``paid_after_cancellation``) instead of dispatching a caregiver to an erased patient. They are
        locked by the check above, so a settlement in flight has finished first."""
        for booking in self._patient_open_bookings(user, patient):
            booking.status = BookingStatus.CANCELLED
            booking.cancel_reason = CANCEL_REASON_ERASED
            booking.updated_at = now

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
        self.db.query(ChatMessage).filter(ChatMessage.sender_id == user.id).update(
            {"message": "[deleted]", "attachment_url": None}, synchronize_session=False)

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
        # Never matchable again, whatever else happens (an open socket recreating a location, a bug): the matcher and
        # the accept path both require approved and not suspended. Any earlier admin reason is left as it was.
        cg.profile_status = "suspended"
        cg.suspended_at = cg.suspended_at or datetime.utcnow()
        cg.suspension_reason = cg.suspension_reason or "account_erased"
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
        self._wipe_finished_bookings(patient)
        patient.full_name, patient.email, patient.phone_number = ERASED_NAME, email, None
        for field in ("date_of_birth", "gender", "address", "city", "state", "postal_code",
                      "emergency_contact_name", "emergency_contact_phone", "emergency_contact_relationship",
                      "emergency_contact_address", "blood_group", "allergies", "medical_conditions",
                      "medications", "special_requirements"):
            setattr(patient, field, None)
        patient.is_active = False
        now = datetime.utcnow()
        self.db.query(PatientCaregiverAccess).filter(
            PatientCaregiverAccess.patient_id == patient.id, PatientCaregiverAccess.revoked_at.is_(None)
        ).update({"revoked_at": now}, synchronize_session=False)
        for member in self.db.query(PatientFamilyMember).filter(PatientFamilyMember.patient_id == patient.id):
            member.full_name, member.phone_number, member.email = ERASED_NAME, None, None

    def _wipe_finished_bookings(self, patient: Patient) -> None:
        """Finished and cancelled bookings keep their money and audit fields but lose where the person lives and
        what they said about their health."""
        finished = (BookingStatus.COMPLETED, BookingStatus.CANCELLED, BookingStatus.DECLINED)
        self.db.query(Booking).filter(
            or_(Booking.patient_id == patient.id, Booking.booked_by_user_id == patient.user_id),
            Booking.status.in_(finished),
        ).update({
            "patient_address": None, "patient_latitude": None, "patient_longitude": None,
            "special_instructions": None, "condition": None, "condition_encrypted": None,
        }, synchronize_session=False)

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

    def _withdraw_whatsapp(self, user: User, phones: set, now: datetime) -> None:
        """Unlink and anonymise the person's WhatsApp contact the way STOP DATA does (consents withdrawn,
        drafts closed, opted out, a compliance task opened), then free the number and the wa_id."""
        from app.channels.whatsapp import withdrawal
        from app.channels.whatsapp.engine import State

        numbers = set().union(*(phone_variants(p) for p in phones)) if phones else set()
        # Also a contact that was never linked (the number matched an app account and waited for a code):
        # it still holds this person's number and name.
        match = WhatsAppContact.user_id == user.id
        if phones:
            match = or_(match, and_(WhatsAppContact.user_id.is_(None), or_(
                WhatsAppContact.phone_e164.in_(phones),
                WhatsAppContact.wa_id.in_({n.lstrip("+") for n in numbers}))))
        contacts = self.db.query(WhatsAppContact).filter(match).populate_existing().with_for_update().all()
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
