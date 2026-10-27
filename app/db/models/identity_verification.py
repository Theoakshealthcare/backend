"""Caregiver KYC (Prembly): one row per verification attempt, the webhook ledger, and the system-decision trail.

See docs/plans/2026-10-27-caregiver-kyc-design.md sections 7, 8 and 9.

Nothing here stores an ID or face image, a NIN/BVN, or anything the provider returned about the person (name, DOB, address,
photo). An attempt keeps the provider's reference, scores, yes/no match results and reason codes; ``id_fingerprint`` is a keyed
hash used only to spot one identity on two accounts. ``kyc_webhook_events`` keeps a hash of the body, never the body.

Attempt statuses (doc 7.1): ACTIVE ones (``created``, ``started``, ``awaiting_result``) are limited to one per caregiver by a
partial unique index; the rest are terminal.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, event, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from app.db.base import Base

VERIFICATION_CREATED, VERIFICATION_STARTED, VERIFICATION_AWAITING = "created", "started", "awaiting_result"
VERIFICATION_PASSED, VERIFICATION_FAILED, VERIFICATION_NEEDS_REVIEW = "passed", "failed", "needs_review"
VERIFICATION_EXPIRED, VERIFICATION_ERROR = "expired", "error"
VERIFICATION_ACTIVE_STATUSES = (VERIFICATION_CREATED, VERIFICATION_STARTED, VERIFICATION_AWAITING)
VERIFICATION_TERMINAL_STATUSES = (VERIFICATION_PASSED, VERIFICATION_FAILED, VERIFICATION_NEEDS_REVIEW, VERIFICATION_EXPIRED,
                                  VERIFICATION_ERROR)
VERIFICATION_STATUSES = VERIFICATION_ACTIVE_STATUSES + VERIFICATION_TERMINAL_STATUSES

# The values of ``caregivers.identity_verification_status`` (doc 7.2); the CHECK lives on the Caregiver model.
IDENTITY_STATUSES = ("pending", "in_progress", "needs_review", "verified", "failed")

ID_TYPES = ("nin", "bvn", "drivers_licence", "voters_card", "passport")
DECIDED_VIA = ("provider", "admin")
# Not enforced by a CHECK (K3/K4 have no migration and may need another code); the set the design lists (doc 9.1).
REASON_CODES = ("face_mismatch", "liveness_failed", "face_grey_band", "name_mismatch", "dob_mismatch", "duplicate_identity",
                "watchlisted", "record_not_found", "document_unreadable", "provider_disagreement", "provider_failed",
                "attempts_exhausted")

WEBHOOK_OUTCOMES = ("applied", "duplicate", "orphan", "ignored_terminal", "disagreement", "error")
EVENT_ACTORS = ("provider", "reconcile", "client")  # or ``admin:<admin user id>``


def _in(column: str, values) -> str:
    return f"{column} IN (" + ",".join(f"'{v}'" for v in values) + ")"


class IdentityVerification(Base):
    __tablename__ = "identity_verifications"
    __table_args__ = (
        CheckConstraint(_in("status", VERIFICATION_STATUSES), name="ck_identity_verifications_status"),
        CheckConstraint("attempt_no >= 1", name="ck_identity_verifications_attempt_no"),
        CheckConstraint("id_type IS NULL OR " + _in("id_type", ID_TYPES), name="ck_identity_verifications_id_type"),
        CheckConstraint("decided_via IS NULL OR " + _in("decided_via", DECIDED_VIA), name="ck_identity_verifications_decided_via"),
        CheckConstraint("face_confidence IS NULL OR (face_confidence >= 0 AND face_confidence <= 100)",
                        name="ck_identity_verifications_face_confidence"),
        CheckConstraint("liveness_confidence IS NULL OR (liveness_confidence >= 0 AND liveness_confidence <= 100)",
                        name="ck_identity_verifications_liveness_confidence"),
        CheckConstraint("face_threshold IS NULL OR (face_threshold >= 0 AND face_threshold <= 100)",
                        name="ck_identity_verifications_face_threshold"),
        CheckConstraint("provider_cost IS NULL OR provider_cost >= 0", name="ck_identity_verifications_provider_cost"),
        UniqueConstraint("caregiver_id", "attempt_no", name="uq_identity_verifications_caregiver_attempt"),
        # One active attempt per caregiver; POST .../sessions returns it instead of creating a second.
        Index("uq_identity_verifications_one_active", "caregiver_id", unique=True,
              postgresql_where=text(_in("status", VERIFICATION_ACTIVE_STATUSES))),
        Index("ix_identity_verifications_caregiver", "caregiver_id"),
        Index("ix_identity_verifications_status", "status", "created_at"),
        Index("ix_identity_verifications_fingerprint", "id_fingerprint"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=False)
    attempt_no = Column(Integer, nullable=False)  # 1-based per caregiver; counted for the cap
    # Opaque; sent to the provider as ``metadata.oakly_ref``. The binding key from the signed webhook body.
    reference = Column(UUID(as_uuid=True), nullable=False, unique=True, default=uuid.uuid4)
    provider = Column(String, nullable=False, default="prembly", server_default="prembly")
    provider_session_id = Column(String, nullable=True, unique=True)  # a client hint first, confirmed from the pull
    status = Column(String, nullable=False, default=VERIFICATION_CREATED)
    reason_codes = Column(ARRAY(String), nullable=False, default=list, server_default=text("'{}'::varchar[]"))
    id_type = Column(String, nullable=True)
    face_confidence = Column(Numeric(5, 2), nullable=True)  # normalised to 0..100
    liveness_confidence = Column(Numeric(5, 2), nullable=True)
    face_threshold = Column(Integer, nullable=True)  # the threshold applied
    name_match = Column(Boolean, nullable=True)  # results only: the provider's name and DOB are NOT stored
    dob_match = Column(Boolean, nullable=True)
    # HMAC-SHA256(KYC_FINGERPRINT_KEY, type + ":" + normalised number); NULL if the payload carried no number. Nulled on erasure.
    id_fingerprint = Column(String, nullable=True)
    provider_reference = Column(String, nullable=True)  # Prembly's ``verification.reference``; nulled on erasure
    provider_cost = Column(Numeric(12, 2), nullable=True)
    decided_via = Column(String, nullable=True)  # provider | admin, once decided
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class KycWebhookEvent(Base):
    """The ledger of what Prembly sent: the dedupe key and the outcome. The BODY IS NOT STORED (it carries the registry record)."""
    __tablename__ = "kyc_webhook_events"
    __table_args__ = (
        CheckConstraint("outcome IS NULL OR " + _in("outcome", WEBHOOK_OUTCOMES), name="ck_kyc_webhook_events_outcome"),
        Index("ix_kyc_webhook_events_session", "provider_session_id"),
        Index("ix_kyc_webhook_events_verification", "verification_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token = Column(String, nullable=False, unique=True)  # Prembly's per-webhook ``token`` header: the idempotency key
    provider_session_id = Column(String, nullable=True)  # nulled on erasure
    event_type = Column(String, nullable=True)
    signature_ok = Column(Boolean, nullable=False)
    body_sha256 = Column(String, nullable=True)
    received_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)
    outcome = Column(String, nullable=True)  # null until processed
    verification_id = Column(UUID(as_uuid=True), ForeignKey("identity_verifications.id"), nullable=True)


class IdentityVerificationEvent(Base):
    """Append-only trail of system decisions (``admin_audit_log.admin_user_id`` is NOT NULL, so it cannot hold them).

    Like ``consents``: the ORM refuses UPDATE/DELETE below and the migration adds a trigger that refuses them for any client.
    ``from_status`` / ``to_status`` are the ATTEMPT's statuses. ``actor`` is provider | reconcile | client | admin:<id>.
    """
    __tablename__ = "identity_verification_events"
    __table_args__ = (
        CheckConstraint("actor IN ('provider','reconcile','client') OR actor LIKE 'admin:%'",
                        name="ck_identity_verification_events_actor"),
        CheckConstraint("from_status IS NULL OR " + _in("from_status", VERIFICATION_STATUSES),
                        name="ck_identity_verification_events_from_status"),
        CheckConstraint(_in("to_status", VERIFICATION_STATUSES), name="ck_identity_verification_events_to_status"),
        Index("ix_identity_verification_events_verification", "verification_id", "at"),
        Index("ix_identity_verification_events_caregiver", "caregiver_id", "at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    verification_id = Column(UUID(as_uuid=True), ForeignKey("identity_verifications.id"), nullable=False)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=False)
    at = Column(DateTime, nullable=False, default=datetime.utcnow)
    actor = Column(String, nullable=False)
    from_status = Column(String, nullable=True)
    to_status = Column(String, nullable=False)
    reason_codes = Column(ARRAY(String), nullable=False, default=list, server_default=text("'{}'::varchar[]"))


@event.listens_for(IdentityVerificationEvent, "before_update")
def _refuse_update(mapper, connection, target):
    raise RuntimeError("identity_verification_events is append-only")


@event.listens_for(IdentityVerificationEvent, "before_delete")
def _refuse_delete(mapper, connection, target):
    raise RuntimeError("identity_verification_events is append-only")
