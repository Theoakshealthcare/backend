from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base

# The defaults the migration seeds, and what the service falls back to if the row is somehow missing.
POLICY_DEFAULTS = {
    "cancel_free_hours": 12,
    "cancel_late_hours": 2,
    "cancel_fee_percent": 20,
    "cancel_late_fee_percent": 50,
    "dropout_redispatch_min_lead_minutes": 120,
    "dropout_decision_lead_minutes": 60,
    "onmyway_compensation_window_minutes": 120,
    "caregiver_late_fee_share_percent": 80,  # stored now, paid out by the caregiver-compensation work (A3)
}


class BookingPolicy(Base):
    """The one row of cancellation and drop-out rules an admin edits (``/admin/booking-policy``).

    A single row (``id = 1``, enforced), kept apart from ``booking_settings`` (pricing, a history of rows): these are
    rules, not prices, they have their own permission and audit trail, and one current value is all anyone needs.
    The cancellation tiers are *copied onto each quote and booking* when it is created, so editing this row never
    changes terms a family has already agreed to.
    """
    __tablename__ = "booking_policies"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_booking_policies_singleton"),
        CheckConstraint("cancel_fee_percent BETWEEN 0 AND 100", name="ck_booking_policies_fee_percent"),
        CheckConstraint("cancel_late_fee_percent BETWEEN 0 AND 100", name="ck_booking_policies_late_fee_percent"),
        CheckConstraint("caregiver_late_fee_share_percent BETWEEN 0 AND 100", name="ck_booking_policies_share_percent"),
        CheckConstraint("cancel_free_hours BETWEEN 0 AND 168 AND cancel_late_hours BETWEEN 0 AND 168",
                        name="ck_booking_policies_hours"),
        CheckConstraint(
            "dropout_redispatch_min_lead_minutes BETWEEN 0 AND 10080 AND dropout_decision_lead_minutes BETWEEN 0 AND 10080 "
            "AND onmyway_compensation_window_minutes BETWEEN 0 AND 10080", name="ck_booking_policies_minutes"),
        CheckConstraint("cancel_fee_percent <= cancel_late_fee_percent", name="ck_booking_policies_late_fee_not_lower"),
        CheckConstraint("cancel_late_hours < cancel_free_hours", name="ck_booking_policies_late_before_free"),
        CheckConstraint("dropout_decision_lead_minutes < dropout_redispatch_min_lead_minutes",
                        name="ck_booking_policies_decision_before_redispatch"),
    )

    id = Column(Integer, primary_key=True, default=1)
    cancel_free_hours = Column(Integer, nullable=False, default=POLICY_DEFAULTS["cancel_free_hours"])
    cancel_late_hours = Column(Integer, nullable=False, default=POLICY_DEFAULTS["cancel_late_hours"])
    cancel_fee_percent = Column(Integer, nullable=False, default=POLICY_DEFAULTS["cancel_fee_percent"])
    cancel_late_fee_percent = Column(Integer, nullable=False, default=POLICY_DEFAULTS["cancel_late_fee_percent"])
    dropout_redispatch_min_lead_minutes = Column(
        Integer, nullable=False, default=POLICY_DEFAULTS["dropout_redispatch_min_lead_minutes"])
    dropout_decision_lead_minutes = Column(Integer, nullable=False, default=POLICY_DEFAULTS["dropout_decision_lead_minutes"])
    onmyway_compensation_window_minutes = Column(
        Integer, nullable=False, default=POLICY_DEFAULTS["onmyway_compensation_window_minutes"])
    caregiver_late_fee_share_percent = Column(
        Integer, nullable=False, default=POLICY_DEFAULTS["caregiver_late_fee_share_percent"])
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=True)
