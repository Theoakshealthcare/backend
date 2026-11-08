import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base

SERVICE_MODES = ("home", "video", "both")


class ServiceCatalog(Base):
    """One row per service type: whether it is on offer, what it is called and how long a visit may be.

    The catalogue holds NO prices. A price is the base fee (``booking_settings``) times the service's multiplier
    (``booking_settings.multipliers``); a service is bookable only when it is enabled here AND has a multiplier there
    (``app/services/service_catalog.py``). Durations are stored in minutes; until the booking-minutes slice they are
    validated as whole hours (step 60).
    """
    __tablename__ = "service_catalog"
    __table_args__ = (
        CheckConstraint(
            "service_type IN ('nurse','doctor','physiotherapist','caregiver','nutritionist')",
            name="ck_service_catalog_service_type",
        ),
        CheckConstraint("mode IN ('home','video','both')", name="ck_service_catalog_mode"),
        CheckConstraint(
            "min_minutes > 0 AND step_minutes > 0 AND max_minutes >= min_minutes", name="ck_service_catalog_minutes"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_type = Column(String, nullable=False, unique=True)
    enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    display_name = Column(String, nullable=False)
    mode = Column(String, nullable=False, default="home", server_default="home")
    min_minutes = Column(Integer, nullable=False, default=60, server_default="60")
    step_minutes = Column(Integer, nullable=False, default=60, server_default="60")
    max_minutes = Column(Integer, nullable=False, default=1440, server_default="1440")
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True)
