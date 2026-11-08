"""The service catalogue: which services are on offer, and the one rule for "bookable".

A service is bookable only if it is **enabled** in ``service_catalog`` AND **priced**: the newest ``booking_settings`` row
has a usable multiplier for it (``pricing.is_priced``; the nurse also counts as priced at 1.0 while no multiplier is set,
which is how the app has always sold it). The catalogue stores no prices. Everything that offers a service (the app's
service list, preview and create, the WhatsApp Flow) goes through ``bookable_services`` / ``assert_bookable``.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session

from app.db.models.booking_settings import BookingSettings
from app.db.models.service_catalog import ServiceCatalog
from app.services.audit import AuditService
from app.services.pricing import is_priced

logger = logging.getLogger(__name__)

EDITABLE_FIELDS = ("enabled", "display_name", "mode", "min_minutes", "step_minutes", "max_minutes", "sort_order")


class ServiceUnavailable(Exception):
    """The service is not on offer (disabled, no price, or unknown). Answered as 422 ``service_unavailable``."""

    def __init__(self, service_type: str):
        super().__init__(service_type)
        self.service_type = service_type


class ServiceHasNoPrice(Exception):
    """An admin tried to enable a service that has no multiplier in the pricing. Answered as 422 ``service_has_no_price``."""

    def __init__(self, service_type: str):
        super().__init__(service_type)
        self.service_type = service_type


class ServiceNotFound(Exception):
    pass


def latest_settings(db: Session) -> Optional[BookingSettings]:
    return db.query(BookingSettings).order_by(BookingSettings.created_at.desc(), BookingSettings.id.desc()).first()


def all_rows(db: Session) -> List[ServiceCatalog]:
    return db.query(ServiceCatalog).order_by(ServiceCatalog.sort_order, ServiceCatalog.service_type).all()


def is_bookable(row: ServiceCatalog, settings: Optional[BookingSettings]) -> bool:
    return bool(row.enabled) and is_priced(settings, row.service_type)


def bookable_services(db: Session, settings: Optional[BookingSettings] = None) -> List[ServiceCatalog]:
    """The services a person may book now, in display order. ``settings`` is the newest pricing row (fetched when omitted)."""
    settings = settings if settings is not None else latest_settings(db)
    return [row for row in all_rows(db) if is_bookable(row, settings)]


def bookable_types(db: Session, settings: Optional[BookingSettings] = None) -> List[str]:
    return [row.service_type for row in bookable_services(db, settings)]


def get_bookable(db: Session, service_type: str, settings: Optional[BookingSettings]) -> ServiceCatalog:
    """The catalogue row for ``service_type`` if it is bookable, else ``ServiceUnavailable``."""
    row = db.query(ServiceCatalog).filter(ServiceCatalog.service_type == service_type).first()
    if row is None or not is_bookable(row, settings):
        raise ServiceUnavailable(service_type)
    return row


def display_name(db: Session, service_type: str) -> str:
    row = db.query(ServiceCatalog).filter(ServiceCatalog.service_type == service_type).first()
    return row.display_name if row is not None else service_type.capitalize()


def snapshot(row: ServiceCatalog) -> Dict[str, Any]:
    return {field: getattr(row, field) for field in EDITABLE_FIELDS}


def update(db: Session, *, admin_id: UUID, service_type: str, changes: Dict[str, Any], reason: Optional[str] = None,
           request: Optional[Request] = None) -> ServiceCatalog:
    """Replace the editable fields of one service. Commits.

    Idempotent: the same body twice leaves the same row and writes no second audit row (a retry after a timeout is safe).
    Turning a service ON that has no price raises ``ServiceHasNoPrice`` (an already-enabled one may still be edited).
    The row is locked so two admins cannot interleave.
    """
    row = (db.query(ServiceCatalog).filter(ServiceCatalog.service_type == service_type).with_for_update().first())
    if row is None:
        raise ServiceNotFound()
    before = snapshot(row)
    if changes.get("enabled") and not row.enabled and not is_priced(latest_settings(db), service_type):
        raise ServiceHasNoPrice(service_type)
    for field, value in changes.items():
        setattr(row, field, value)
    after = snapshot(row)
    if after == before:
        db.rollback()  # release the row lock; nothing to write
        return db.query(ServiceCatalog).filter(ServiceCatalog.service_type == service_type).one()
    row.updated_at = datetime.utcnow()
    row.updated_by = admin_id
    AuditService(db).record(
        admin_user_id=admin_id, action="service_catalog.update", entity_type="service_catalog", entity_id=service_type,
        before=before, after=after, reason=reason, request=request)
    db.commit()
    db.refresh(row)
    logger.info("service catalogue: %s updated by admin %s", service_type, admin_id)
    return row
