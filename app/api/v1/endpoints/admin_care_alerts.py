"""Admin: the care-alert queue (slice 1c). Mounted under /admin.

An out-of-range reading or a missed care-plan task raises an alert (``app/services/care_alerts.py``); this is where a person works
it: list, acknowledge, resolve, each with an optional note. Behind ``support.inbox`` (the permission of the urgent-ticket inbox
these alerts belong with: Operations, Support, Clinical Reviewer and Super Admin hold it). The clinical detail and the note need
``clinical.read`` and come only from the one-alert routes (the list is summary-only), the full name and phone ``people.pii``, as everywhere else in the console. Every acknowledge and resolve is an
``admin_audit_log`` row in the same transaction (ids and statuses only: the note is health text and stays in the encrypted column),
and so is opening a detail the admin may read. Health data: every response is ``Cache-Control: no-store``.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permission
from app.api.v1.endpoints.caregiver_care_plans import ScrubbedValidationRoute
from app.core.phone import mask_phone
from app.db.models.admin import AdminUser
from app.db.models.booking import Booking
from app.db.models.care import CareRecipient
from app.db.models.care_alert import CareAlert
from app.db.models.patient import Patient
from app.schemas.admin_care_alert import (
    AdminRef, AlertActionIn, AlertDetail, AlertErrorBody, AlertStatus, AlertType, CareAlertCounts, CareAlertList, CareAlertOut,
    AlertPartyOut, Severity, StatusFilter,
)
from app.services import care_alerts
from app.services.admin_bookings import mask_name
from app.services.audit import AuditService

NOT_FOUND = "Care alert not found"


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(dependencies=[Depends(_no_store)], route_class=ScrubbedValidationRoute)
PERMISSION = require_permission("support.inbox")
CONFLICT = {409: {"model": AlertErrorBody, "description": "`already_acknowledged` or `already_resolved`."}}


def _admin_ref(db: Session, admin_id: Optional[UUID]) -> Optional[AdminRef]:
    if admin_id is None:
        return None
    admin = db.get(AdminUser, admin_id)
    return AdminRef(id=admin.id, email=admin.email) if admin else None


def _out(db: Session, alert: CareAlert, admin: AdminUser, *, with_detail: bool) -> CareAlertOut:
    """``with_detail`` False (the list) leaves ``detail`` and ``note`` null even for an admin who may read them: the clinical content is
    read, and audited, one alert at a time (``GET /care-alerts/{id}`` and the two actions)."""
    held = admin.permissions
    unmasked, clinical = "people.pii" in held, "clinical.read" in held
    patient = db.get(Patient, alert.patient_id)
    name = patient.full_name if patient else "-"
    phone = patient.phone_number if patient else None
    party = AlertPartyOut(full_name=name, phone=phone, masked=False) if unmasked else AlertPartyOut(
        full_name=mask_name(name), phone=mask_phone(phone) if phone else None, masked=True)
    recipient = db.get(CareRecipient, alert.care_recipient_id) if alert.care_recipient_id else None
    recipient_name = None if recipient is None else (recipient.full_name if unmasked else mask_name(recipient.full_name))
    booking = db.get(Booking, alert.booking_id) if alert.booking_id else None
    detail: Optional[Dict[str, Any]] = care_alerts.open_detail(alert) if clinical and with_detail else None
    return CareAlertOut(
        id=alert.id, type=alert.type, severity=alert.severity, status=alert.status, summary=alert.summary,
        created_at=alert.created_at, last_occurred_at=alert.last_occurred_at, occurrence_count=alert.occurrence_count,
        patient=party, care_recipient_name=recipient_name, booking_id=alert.booking_id,
        booking_code=booking.booking_code if booking else None, vital_id=alert.vital_id,
        family_notified_count=alert.family_notified_count, family_notified_at=alert.family_notified_at,
        admin_notified_at=alert.admin_notified_at, acknowledged_by=_admin_ref(db, alert.acknowledged_by_admin_id),
        acknowledged_at=alert.acknowledged_at, resolved_by=_admin_ref(db, alert.resolved_by_admin_id),
        resolved_at=alert.resolved_at, note=care_alerts.open_note(alert) if clinical and with_detail else None,
        detail=AlertDetail(**{k: v for k, v in detail.items() if k in AlertDetail.model_fields}) if detail else None,
        detail_masked=not clinical)


def _get(db: Session, alert_id: UUID) -> CareAlert:
    alert = db.get(CareAlert, alert_id)
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    return alert


def _counts(db: Session) -> CareAlertCounts:
    by_status = dict(db.execute(select(CareAlert.status, func.count()).group_by(CareAlert.status)).all())
    critical = db.scalar(select(func.count()).where(CareAlert.severity == care_alerts.CRITICAL,
                                                     CareAlert.status.in_(care_alerts.ACTIVE)))
    return CareAlertCounts(open=by_status.get("open", 0), acknowledged=by_status.get("acknowledged", 0),
                           resolved=by_status.get("resolved", 0), critical_active=critical or 0)


@router.get("/care-alerts", response_model=CareAlertList)
def list_care_alerts(
    status_filter: StatusFilter = Query("active", alias="status", description="`active` = open + acknowledged"),
    severity: Optional[Severity] = None,
    type: Optional[AlertType] = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    admin: AdminUser = Depends(PERMISSION),
    db: Session = Depends(get_db),
) -> Any:
    """Critical first, then open before acknowledged, then the most recently fed. Summary only: ``detail`` and ``note`` are ``null`` here
    (``detail_masked`` still says whether this admin may open them with the detail route)."""
    query = select(CareAlert)
    if status_filter == "active":
        query = query.where(CareAlert.status.in_(care_alerts.ACTIVE))
    elif status_filter != "all":
        query = query.where(CareAlert.status == status_filter)
    if severity:
        query = query.where(CareAlert.severity == severity)
    if type:
        query = query.where(CareAlert.type == type)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows: List[CareAlert] = db.scalars(query.order_by(
        case((CareAlert.status == "resolved", 1), else_=0),
        case((CareAlert.severity == "critical", 0), else_=1),
        case((CareAlert.status == "open", 0), else_=1),
        CareAlert.last_occurred_at.desc(), CareAlert.id).limit(limit).offset(offset)).all()
    return CareAlertList(items=[_out(db, a, admin, with_detail=False) for a in rows], total=total or 0, counts=_counts(db))


@router.get("/care-alerts/{alert_id}", response_model=CareAlertOut, responses={404: {"description": NOT_FOUND}})
def get_care_alert(alert_id: UUID, request: Request, admin: AdminUser = Depends(PERMISSION), db: Session = Depends(get_db)) -> Any:
    alert = _get(db, alert_id)
    out = _out(db, alert, admin, with_detail=True)
    if not out.detail_masked:  # health detail was opened: leave a trace, like opening a plan or a voice note
        AuditService(db).record(admin_user_id=admin.id, action="care_alert.read", entity_type="care_alert", entity_id=alert.id,
                                after={"alert_id": str(alert.id)}, request=request)
        db.commit()
    return out


def _act(db: Session, request: Request, admin: AdminUser, alert_id: UUID, body: AlertActionIn, action: str) -> CareAlertOut:
    alert = _get(db, alert_id)
    now = datetime.utcnow()
    try:
        before = (care_alerts.acknowledge if action == "acknowledge" else care_alerts.resolve)(db, alert, admin, body.note, now)
    except care_alerts.AlertError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": exc.message})
    db.refresh(alert)
    AuditService(db).record(
        admin_user_id=admin.id, action=f"care_alert.{action}", entity_type="care_alert", entity_id=alert.id,
        before={"status": before}, after={"status": alert.status, "severity": alert.severity, "type": alert.type,
                                           "note_present": bool(body.note)}, request=request)
    db.commit()
    db.refresh(alert)
    return _out(db, alert, admin, with_detail=True)


@router.post("/care-alerts/{alert_id}/acknowledge", response_model=CareAlertOut, responses=CONFLICT)
def acknowledge_care_alert(alert_id: UUID, body: AlertActionIn, request: Request, admin: AdminUser = Depends(PERMISSION),
                           db: Session = Depends(get_db)) -> Any:
    """open -> acknowledged ("I am on it")."""
    return _act(db, request, admin, alert_id, body, "acknowledge")


@router.post("/care-alerts/{alert_id}/resolve", response_model=CareAlertOut, responses=CONFLICT)
def resolve_care_alert(alert_id: UUID, body: AlertActionIn, request: Request, admin: AdminUser = Depends(PERMISSION),
                       db: Session = Depends(get_db)) -> Any:
    """open or acknowledged -> resolved. A later occurrence for the same patient starts a new alert."""
    return _act(db, request, admin, alert_id, body, "resolve")
