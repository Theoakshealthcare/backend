"""The caregiver side of identity verification (KYC K2): what the app may read, start and hint at.

Design: docs/plans/2026-10-27-caregiver-kyc-design.md sections 6, 7, 10.2 and 12.1.

The trust rule that shapes everything here: the client's word never decides anything. This service can

* create an attempt (and, with it, move a ``pending`` caregiver to ``in_progress``),
* record a *hint* about the caregiver's own active attempt (``created`` -> ``started`` -> ``awaiting_result``, plus the provider
  session id the widget reported), and
* let a caregiver continue onboarding without verification while Prembly is down (step 3; the identity status is untouched).

It never sets ``verified``, ``failed`` or ``needs_review`` on a caregiver, and never closes an attempt: the signed webhook and the
authenticated pull (K3/K4) are the only evidence. It makes no call to Prembly: the widget is configured from our own row and the
public widget id/key, and images go phone to Prembly. Errors are ``KycError`` carrying the API's machine code.
"""
import logging
import re
import uuid
from datetime import datetime
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.caregiver import Caregiver
from app.db.models.identity_verification import (
    VERIFICATION_ACTIVE_STATUSES, VERIFICATION_AWAITING, VERIFICATION_CREATED, VERIFICATION_ERROR, VERIFICATION_EXPIRED,
    VERIFICATION_FAILED, VERIFICATION_STARTED, IdentityVerification, IdentityVerificationEvent,
)
from app.services.consent import ConsentService
from app.services.kyc_availability import ProviderAvailability
from app.services.prembly import SESSION_ID_PATTERN

logger = logging.getLogger(__name__)

CONSENT_TYPE = "identity_verification"
CONSENT_CHANNEL = "caregiver_app"
MIN_STEP = 2  # Step 1 (name, date of birth) and Step 2 are done before identity; the widget is pre-filled from Step 1
DEFER_STEP = 3
REVIEW_ETA_HOURS = 24  # "usually within 24 hours" (design 11.3 state 7)
# Attempts that do not count against the cap: nothing was decided about the person (design 7.1)
UNCOUNTED_STATUSES = (VERIFICATION_EXPIRED, VERIFICATION_ERROR)
RETRYABLE_IDENTITY_STATUSES = ("pending", "in_progress")

# A coarse tip for the "try again" screen, from the latest clean failure. Anything else (duplicate, watch-list, mismatch, the
# provider disagreeing with itself) ends in review and is never shown (design 12.1 and 11.3).
FAILURE_HINTS = {
    "face_mismatch": "lighting", "liveness_failed": "lighting", "face_grey_band": "lighting",
    "document_unreadable": "document", "record_not_found": "document",
}
GENERIC_FAILURE_HINT = "mismatch_generic"

_SESSION_ID = re.compile(SESSION_ID_PATTERN)


class KycError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


def _conflict(code: str, message: str) -> KycError:
    return KycError(409, code, message)


class CaregiverIdentityService:
    def __init__(self, db: Session, availability: ProviderAvailability):
        self.db = db
        self.availability = availability

    # --- reads -------------------------------------------------------------------------------------------------------
    def state(self, caregiver: Caregiver, available: Optional[bool] = None) -> Dict:
        attempts = self._attempts(caregiver)  # newest first
        latest = attempts[0] if attempts else None
        active = next((a for a in attempts if a.status in VERIFICATION_ACTIVE_STATUSES), None)
        used = sum(1 for a in attempts if a.status not in UNCOUNTED_STATUSES)
        if available is None:
            available = self.availability.is_available()
        eligible = caregiver.identity_verification_status in RETRYABLE_IDENTITY_STATUSES and caregiver.profile_completion_step >= MIN_STEP
        return {
            "status": caregiver.identity_verification_status,
            "attempts_used": used,
            "attempts_max": settings.KYC_MAX_ATTEMPTS,
            "active_verification_id": active.id if active else None,
            "last_attempt_status": latest.status if latest else None,
            "can_start": bool(eligible and available and (active is not None or used < settings.KYC_MAX_ATTEMPTS)),
            "failure_hint": self._failure_hint(latest) if caregiver.identity_verification_status == "in_progress" else None,
            "provider_available": available,
            "review_eta_hours": REVIEW_ETA_HOURS if caregiver.identity_verification_status == "needs_review" else None,
            "policy_version": settings.KYC_POLICY_VERSION,
        }

    def status_for(self, user_id: UUID) -> Dict:
        return self.state(self._caregiver(user_id))

    # --- POST /sessions ----------------------------------------------------------------------------------------------
    def start_session(self, user_id: UUID, policy_version: str) -> tuple[Dict, bool]:
        """Returns ({verification, widget, state}, created). ``created`` is False when the active attempt is reused."""
        caregiver = self._caregiver(user_id, lock=True)
        self._require_undecided(caregiver)
        self._require_step(caregiver)
        if policy_version != settings.KYC_POLICY_VERSION:
            raise _conflict("POLICY_VERSION_MISMATCH",
                            "The identity-check consent text has changed. Reload and agree to the current version.")
        widget_id, widget_key = settings.PREMBLY_WIDGET_ID, settings.PREMBLY_WIDGET_KEY
        available = self.availability.is_available()
        if not (widget_id and widget_key) or not available:
            raise KycError(503, "PROVIDER_UNAVAILABLE", "Identity verification is temporarily unavailable. Please try again later.")

        attempts = self._attempts(caregiver)
        row = next((a for a in attempts if a.status in VERIFICATION_ACTIVE_STATUSES), None)
        created = row is None
        if created:
            if sum(1 for a in attempts if a.status not in UNCOUNTED_STATUSES) >= settings.KYC_MAX_ATTEMPTS:
                raise _conflict("ATTEMPTS_EXHAUSTED", "You have used all your identity verification attempts. Our team will review your case.")
            row = IdentityVerification(
                caregiver_id=caregiver.id, attempt_no=max((a.attempt_no for a in attempts), default=0) + 1,
                reference=uuid.uuid4(), status=VERIFICATION_CREATED,
            )
            self.db.add(row)
            self.db.flush()
            # The separate, append-only consent (design 10.2), tied to this attempt by its opaque reference.
            ConsentService(self.db).record(
                types=[CONSENT_TYPE], decision="granted", channel=CONSENT_CHANNEL, evidence_ref=str(row.reference),
                subject_user_id=caregiver.user_id, granted_by_user_id=caregiver.user_id, policy_version=policy_version)
            self._event(row, "client", None, VERIFICATION_CREATED)
            if caregiver.identity_verification_status == "pending":
                caregiver.identity_verification_status = "in_progress"
            caregiver.updated_at = datetime.utcnow()
        self.db.commit()

        first, _, last = (caregiver.full_name or "").strip().partition(" ")
        widget = {
            "widget_id": widget_id, "widget_key": widget_key,  # the key is public by Prembly's design; every other key stays server-side
            "email": caregiver.email or caregiver.user.email, "first_name": first, "last_name": last.strip(),
            "metadata": {"oakly_ref": str(row.reference)},
        }
        return {"verification_id": row.id, "attempt_no": row.attempt_no, "widget": widget, "state": self.state(caregiver, available)}, created

    # --- POST /{id}/client-event -------------------------------------------------------------------------------------
    def client_event(self, user_id: UUID, verification_id: UUID, event: str, session_id: Optional[str]) -> Dict:
        """A hint from the widget. Moves the caregiver's own active attempt forward and remembers the session id; nothing else."""
        caregiver = self._caregiver(user_id)
        row = (self.db.query(IdentityVerification)
               .filter(IdentityVerification.id == verification_id, IdentityVerification.caregiver_id == caregiver.id)
               .with_for_update().first())
        if row is None:  # not yours and not there look the same
            raise KycError(404, "VERIFICATION_NOT_FOUND", "Verification not found")
        if row.status in VERIFICATION_ACTIVE_STATUSES and event in ("opened", "completed"):
            target = self._hint_target(row.status, event)
            if target and target != row.status:
                self._event(row, "client", row.status, target)
                row.status = target
                if target == VERIFICATION_STARTED and row.started_at is None:
                    row.started_at = datetime.utcnow()
            if session_id:
                self._remember_session_hint(row, session_id)
            row.updated_at = datetime.utcnow()
        self.db.commit()
        return self.state(caregiver)

    # --- POST /defer -------------------------------------------------------------------------------------------------
    def defer(self, user_id: UUID) -> Dict:
        """The outage path: let the caregiver carry on to consent without verification. Identity stays as it is; approval stays blocked."""
        caregiver = self._caregiver(user_id, lock=True)
        self._require_undecided(caregiver)
        self._require_step(caregiver)
        if self.availability.is_available():
            raise _conflict("PROVIDER_AVAILABLE", "Identity verification is available right now. Please complete it.")
        if caregiver.profile_completion_step < DEFER_STEP:
            caregiver.profile_completion_step = DEFER_STEP
            caregiver.updated_at = datetime.utcnow()
            logger.info("kyc deferred during an outage caregiver=%s", caregiver.id)
        self.db.commit()
        return self.state(caregiver)

    # --- helpers -----------------------------------------------------------------------------------------------------
    def _caregiver(self, user_id: UUID, lock: bool = False) -> Caregiver:
        query = self.db.query(Caregiver).filter(Caregiver.user_id == user_id)
        if lock:  # serialises two taps on "Start" (the partial unique index is the backstop)
            query = query.with_for_update()
        caregiver = query.first()
        if caregiver is None:
            raise KycError(404, "CAREGIVER_NOT_FOUND", "Caregiver not found")
        return caregiver

    def _attempts(self, caregiver: Caregiver) -> List[IdentityVerification]:
        return (self.db.query(IdentityVerification).filter(IdentityVerification.caregiver_id == caregiver.id)
                .order_by(IdentityVerification.attempt_no.desc()).all())

    @staticmethod
    def _require_undecided(caregiver: Caregiver) -> None:
        status = caregiver.identity_verification_status
        if status == "verified":
            raise _conflict("ALREADY_VERIFIED", "Your identity is already verified.")
        if status == "needs_review":
            raise _conflict("UNDER_REVIEW", "Your identity check is with our team. You can continue.")
        if status == "failed":
            raise _conflict("IDENTITY_REJECTED", "Your identity could not be verified. Please contact support.")

    @staticmethod
    def _require_step(caregiver: Caregiver) -> None:
        if caregiver.profile_completion_step < MIN_STEP:
            raise _conflict("PROFILE_INCOMPLETE", "Complete the earlier profile steps before verifying your identity.")

    @staticmethod
    def _hint_target(current: str, event: str) -> Optional[str]:
        if event == "opened":
            return VERIFICATION_STARTED if current == VERIFICATION_CREATED else None
        return VERIFICATION_AWAITING  # completed: from created or started

    def _remember_session_hint(self, row: IdentityVerification, session_id: str) -> None:
        """Keep the widget's session id as a hint to speed up the pull. ``provider_session_id`` is unique, so an id already on any
        attempt (a clash, or someone squatting a known id) is quietly not stored: the binding never depends on it (design 16.1 point 5)."""
        if not _SESSION_ID.match(session_id) or row.provider_session_id == session_id:
            return
        try:
            with self.db.begin_nested():
                row.provider_session_id = session_id
                self.db.flush()
        except IntegrityError:
            logger.info("kyc session hint ignored: id already on another attempt verification=%s", row.id)

    def _event(self, row: IdentityVerification, actor: str, from_status: Optional[str], to_status: str) -> None:
        self.db.add(IdentityVerificationEvent(verification_id=row.id, caregiver_id=row.caregiver_id, actor=actor,
                                              from_status=from_status, to_status=to_status, reason_codes=[]))

    @staticmethod
    def _failure_hint(latest: Optional[IdentityVerification]) -> Optional[str]:
        if latest is None or latest.status != VERIFICATION_FAILED:
            return None
        for code in latest.reason_codes or []:
            if code in FAILURE_HINTS:
                return FAILURE_HINTS[code]
        return GENERIC_FAILURE_HINT
