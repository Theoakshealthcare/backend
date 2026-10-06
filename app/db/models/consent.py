import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, String, event
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base

CONSENT_TYPES = ("terms", "health_processing", "marketing", "share_with_family")


class Consent(Base):
    """One consent decision. Append-only: a withdrawal is a new row with ``decision='withdrawn'``, never
    an edit of the grant, so the history that proves what the patient agreed to cannot be rewritten.

    This replaces the PRD's ``granted_at`` / ``withdrawn_at`` pair on a single row (which needs an
    UPDATE) with ``decision`` + ``created_at``. The current state is the latest row per subject and type.
    The ORM refuses UPDATE/DELETE below, and the migration adds a trigger that refuses them for any client.
    """
    __tablename__ = "consents"
    __table_args__ = (
        CheckConstraint(
            "type IN (" + ", ".join(f"'{t}'" for t in CONSENT_TYPES) + ")", name="ck_consents_type"
        ),
        CheckConstraint("decision IN ('granted', 'withdrawn')", name="ck_consents_decision"),
        CheckConstraint(
            "contact_id IS NOT NULL OR subject_user_id IS NOT NULL OR subject_recipient_id IS NOT NULL",
            name="ck_consents_has_subject",
        ),
        Index("ix_consents_contact_type_created", "contact_id", "type", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id = Column(UUID(as_uuid=True), ForeignKey("whatsapp_contacts.id"), nullable=True)
    subject_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)
    subject_recipient_id = Column(UUID(as_uuid=True), ForeignKey("care_recipients.id"), nullable=True,
                                  index=True)
    granted_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    type = Column(String, nullable=False)
    decision = Column(String, nullable=False)
    policy_version = Column(String, nullable=False)
    channel = Column(String, nullable=False)  # whatsapp_button now; whatsapp_flow from W2
    evidence_ref = Column(String, nullable=True)  # the Meta message id of the tap or submission
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


@event.listens_for(Consent, "before_update")
def _refuse_update(mapper, connection, target):
    raise RuntimeError("consents is append-only")


@event.listens_for(Consent, "before_delete")
def _refuse_delete(mapper, connection, target):
    raise RuntimeError("consents is append-only")
