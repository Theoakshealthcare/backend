"""Care alerts (slice 1c): an out-of-range vital reading or a missed care-plan task that somebody must act on.

Two tables. ``care_alerts`` is the queue the admin console works (open -> acknowledged -> resolved). ``care_alert_occurrences`` is
the ledger of what raised or fed each alert, one row per source (a reading and its severity, or a visit), unique on ``source_key``,
so re-evaluating the same reading or sweeping the same visit twice is a no-op and a burst of readings inside the debounce window
only bumps ``occurrence_count`` on one alert instead of making many (rules in ``app/services/care_alerts.py``).

Privacy: the plaintext columns carry ids, the type, the severity and a one-line generic ``summary``. Anything that describes the
person's health (the reason a reading was flagged, the names of the missed tasks, an admin's note) is Fernet-encrypted.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base

ALERT_TYPES = ("vital_out_of_range", "task_missed")
SEVERITIES = ("warning", "critical")
STATUSES = ("open", "acknowledged", "resolved")


def _in(column: str, values) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class CareAlert(Base):
    __tablename__ = "care_alerts"
    __table_args__ = (
        CheckConstraint(_in("type", ALERT_TYPES), name="ck_care_alerts_type"),
        CheckConstraint(_in("severity", SEVERITIES), name="ck_care_alerts_severity"),
        CheckConstraint(_in("status", STATUSES), name="ck_care_alerts_status"),
        CheckConstraint("occurrence_count >= 1", name="ck_care_alerts_occurrences"),
        Index("ix_care_alerts_queue", "status", "severity", text("last_occurred_at DESC")),
        Index("ix_care_alerts_debounce", "patient_id", "type", "severity", text("last_occurred_at DESC")),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type = Column(String, nullable=False)
    severity = Column(String, nullable=False)
    status = Column(String, nullable=False, default="open", server_default=text("'open'"))
    # The patient row on the booking or reading (for a visit booked for a relative: the booker); ``care_recipient_id`` is the client
    # when the visit was booked for somebody else.
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    care_recipient_id = Column(UUID(as_uuid=True), ForeignKey("care_recipients.id"), nullable=True)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=True)
    vital_id = Column(UUID(as_uuid=True), ForeignKey("patient_vitals.id", ondelete="SET NULL"), nullable=True)
    summary = Column(String, nullable=False)  # one generic line, no value and no condition
    detail_encrypted = Column(Text, nullable=True)  # encrypted JSON: the flag reason, or the missed task titles
    occurrence_count = Column(Integer, nullable=False, default=1, server_default=text("1"))
    last_occurred_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    family_notified_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    family_notified_at = Column(DateTime, nullable=True)
    admin_notified_at = Column(DateTime, nullable=True)
    acknowledged_by_admin_id = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)
    resolved_by_admin_id = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    note_encrypted = Column(Text, nullable=True)  # the admin's latest note (acknowledge or resolve), encrypted
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class CareAlertOccurrence(Base):
    """One source that raised or fed an alert. ``source_key`` is ``vital:<reading id>:<severity>`` or ``visit:<booking id>``."""
    __tablename__ = "care_alert_occurrences"
    __table_args__ = (UniqueConstraint("source_key", name="uq_care_alert_occurrences_source_key"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    alert_id = Column(UUID(as_uuid=True), ForeignKey("care_alerts.id", ondelete="CASCADE"), nullable=False, index=True)
    source_key = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
