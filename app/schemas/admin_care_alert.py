"""Admin API shapes for the care-alert queue (slice 1c). The patient's name and phone follow the bookings rule (full only with
``people.pii``, masked otherwise); the clinical ``detail`` (the flag reason, the reading, the missed task titles) needs ``clinical.read``
and is ``null`` with ``detail_masked: true`` otherwise."""

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

AlertType = Literal["vital_out_of_range", "task_missed"]
Severity = Literal["warning", "critical"]
AlertStatus = Literal["open", "acknowledged", "resolved"]
StatusFilter = Literal["active", "open", "acknowledged", "resolved", "all"]


class PartyOut(BaseModel):
    full_name: str
    phone: Optional[str] = None
    masked: bool


class AlertDetail(BaseModel):
    # vital_out_of_range
    vital_type: Optional[str] = None
    status: Optional[str] = None  # high | low | critical
    reason: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    # task_missed
    tasks: Optional[List[str]] = None
    days: Optional[List[str]] = None


class AdminRef(BaseModel):
    id: UUID
    email: str


class CareAlertOut(BaseModel):
    id: UUID
    type: AlertType
    severity: Severity
    status: AlertStatus
    summary: str
    created_at: datetime
    last_occurred_at: datetime
    occurrence_count: int
    patient: PartyOut
    care_recipient_name: Optional[str] = None  # the client, when the visit was booked for a relative
    booking_id: Optional[UUID] = None
    booking_code: Optional[str] = None
    vital_id: Optional[UUID] = None
    family_notified_count: int
    family_notified_at: Optional[datetime] = None
    admin_notified_at: Optional[datetime] = None
    acknowledged_by: Optional[AdminRef] = None
    acknowledged_at: Optional[datetime] = None
    resolved_by: Optional[AdminRef] = None
    resolved_at: Optional[datetime] = None
    note: Optional[str] = None  # the latest admin note; needs clinical.read like the detail
    detail: Optional[AlertDetail] = None
    detail_masked: bool


class CareAlertCounts(BaseModel):
    open: int
    acknowledged: int
    resolved: int
    critical_active: int  # open or acknowledged alerts of severity critical


class CareAlertList(BaseModel):
    items: List[CareAlertOut]
    total: int
    counts: CareAlertCounts


class AlertActionIn(BaseModel):
    note: Optional[str] = Field(default=None, max_length=1000)


class AlertErrorBody(BaseModel):
    code: str
    message: str
