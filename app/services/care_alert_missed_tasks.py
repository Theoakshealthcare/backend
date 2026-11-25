"""Missed care-plan tasks (slice 1c): when does a task count as MISSED, and the cron sweep that raises the alert.

**The rule.** A task is missed when it was due in a visit and nobody had ticked it for that day when the visit ended. Everything
comes from data that already exists; no schedule is invented:

* *Which visits.* A booking that is ``completed`` and was actually started (``actual_start_time`` set), whose end
  (``visit_windows.ended_at``) has passed, and which ended within ``CARE_ALERT_SWEEP_LOOKBACK_HOURS``. A tick can only be made while
  the visit is in progress (care_plan_tasks), so the moment a visit is completed its ticks are final. A no-show, a cancelled or a
  declined visit never started, so nothing was missed. A visit left in progress is judged when it is finally completed.
* *Which plan.* The subject's ``active`` plan: a visit booked for oneself uses the patient's plan, a visit booked for a relative
  the care recipient's plan (the same keying as care_plan_access).
* *Which tasks are due.* ``care_plan_tasks.scheduled_on`` (active, inside the plan's dates, the task's weekdays) on each local day
  (``HOME_TIMEZONE``) the visit's scheduled window touches, AND the task's own ``time_slot`` overlaps that window by at least
  ``CARE_ALERT_MIN_SLOT_OVERLAP_MINUTES``. A morning task is not due in an evening visit. The slot bands are the one constant below
  (the task only stores the word "morning"; the clinical lead has not given hours, so they are a question in the SOP). A task that
  was added after the visit's scheduled start is never due in it (this also stops a plan created during its first visit from raising
  an alert about itself).
* *Missed* = due and no ``care_plan_task_completions`` row for (task, that day), whoever ticked it, in whichever visit.

The alert is a ``warning`` ``task_missed`` for the booking's patient (the booker, for a relative's visit) and the care recipient. The
source key is ``visit:<booking id>``, so re-running the sweep never doubles it. The family message names no task (task text is health
text); the admin queue's encrypted detail lists the titles.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import String, cast, func, literal, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decrypt
from app.db.models.booking import Booking, BookingStatus
from app.db.models.care_alert import CareAlertOccurrence
from app.db.models.care_plan import CarePlan, CarePlanTask, CarePlanTaskCompletion
from app.services import care_alerts
from app.services.care_plan_access import is_self_booking
from app.services.care_plan_tasks import scheduled_on
from app.services.visit_windows import ended_at

logger = logging.getLogger(__name__)

# Local clock hours (start inclusive, end exclusive) of each time slot. "night" is the evening and the small hours of the SAME date
# (a tick is always dated the day it is made, so a 02:00 tick for a night task counts for the new date).
SLOT_BANDS: Dict[str, Tuple[Tuple[int, int], ...]] = {
    "morning": ((5, 12),), "afternoon": ((12, 17),), "evening": ((17, 21),), "night": ((0, 5), (21, 24)),
}
BATCH_LIMIT = 200


def to_local(moment: datetime) -> datetime:
    """A naive-UTC timestamp as a naive local (HOME_TIMEZONE) datetime."""
    return moment.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.HOME_TIMEZONE)).replace(tzinfo=None)


def slot_overlap_minutes(slot: str, day: date, start_local: datetime, end_local: datetime) -> float:
    """Minutes the local window [start, end] spends inside ``slot``'s band(s) on ``day``."""
    total = 0.0
    midnight = datetime(day.year, day.month, day.day)
    for from_hour, to_hour in SLOT_BANDS.get(slot, ()):
        band_start, band_end = midnight + timedelta(hours=from_hour), midnight + timedelta(hours=to_hour)
        overlap = (min(end_local, band_end) - max(start_local, band_start)).total_seconds() / 60
        total += max(overlap, 0.0)
    return total


def due_tasks(plan: CarePlan, tasks: Sequence[CarePlanTask], window_start: datetime, window_end: datetime) -> List[Tuple[CarePlanTask, date]]:
    """Every (task, local day) that was due in the visit window (naive UTC). Pure."""
    start_local, end_local = to_local(window_start), to_local(window_end)
    days: List[date] = []
    day = start_local.date()
    while day <= end_local.date():
        days.append(day)
        day += timedelta(days=1)
    minimum = settings.CARE_ALERT_MIN_SLOT_OVERLAP_MINUTES
    due = []
    for task in tasks:
        if task.created_at > window_start:
            continue  # the task did not exist when the visit began
        for day in days:
            if scheduled_on(plan, task, day) and slot_overlap_minutes(task.time_slot, day, start_local, end_local) >= minimum:
                due.append((task, day))
    return due


@dataclass
class Missed:
    titles: List[str]
    days: List[date]


def missed_for_visit(db: Session, booking: Booking) -> Optional[Missed]:
    """The tasks due in this visit that were never ticked, or None when there is no plan or nothing was missed."""
    if is_self_booking(booking):
        plan_filter = CarePlan.patient_id == booking.patient_id
    elif booking.care_recipient_id is not None:
        plan_filter = CarePlan.care_recipient_id == booking.care_recipient_id
    else:
        return None  # an app booking for someone else: no identifiable client, so no plan
    plan = db.scalars(select(CarePlan).where(plan_filter, CarePlan.status == "active")).first()
    if plan is None:
        return None
    tasks = db.scalars(select(CarePlanTask).where(CarePlanTask.plan_id == plan.id, CarePlanTask.is_active.is_(True))
                       .order_by(CarePlanTask.sort_order)).all()
    window_start, window_end = booking.scheduled_date, max(booking.scheduled_end_date, ended_at(booking))
    due = due_tasks(plan, tasks, window_start, window_end)
    if not due:
        return None
    ticked = {(task_id, day) for task_id, day in db.execute(
        select(CarePlanTaskCompletion.task_id, CarePlanTaskCompletion.for_date)
        .where(CarePlanTaskCompletion.task_id.in_({t.id for t, _ in due}),
               CarePlanTaskCompletion.for_date.in_({d for _, d in due})))}
    missed = [(task, day) for task, day in due if (task.id, day) not in ticked]
    if not missed:
        return None
    titles = []
    for task, _day in missed:
        try:
            titles.append(decrypt(task.title_encrypted) if task.title_encrypted else "A care task")
        except Exception:  # noqa: BLE001 - an unreadable title must not stop the alert
            titles.append("A care task")
    return Missed(titles, sorted({day for _, day in missed}))


def task_summary(count: int) -> str:
    return f"{count} care task{'s' if count != 1 else ''} not marked done by the end of the visit."


def sweep(db: Session, now: Optional[datetime] = None) -> List[UUID]:
    """Judge every recently completed visit not judged yet; returns the ids of the alerts CREATED (new, so they need delivering).
    Commits per visit, so one bad visit cannot lose the others' alerts."""
    now = now or datetime.utcnow()
    since = now - timedelta(hours=settings.CARE_ALERT_SWEEP_LOOKBACK_HOURS)
    ended = func.coalesce(Booking.actual_end_time, Booking.scheduled_end_date)
    judged = select(CareAlertOccurrence.id).where(CareAlertOccurrence.source_key == literal("visit:", String) + cast(Booking.id, String))
    ids = db.scalars(select(Booking.id).where(
        Booking.status == BookingStatus.COMPLETED, Booking.actual_start_time.is_not(None), ended <= now, ended >= since,
        ~judged.exists()).order_by(ended).limit(BATCH_LIMIT)).all()
    created: List[UUID] = []
    for booking_id in ids:
        try:
            booking = db.get(Booking, booking_id)
            missed = missed_for_visit(db, booking)
            if missed is not None:
                raised = care_alerts.raise_alert(
                    db, alert_type=care_alerts.TASK_ALERT, severity=care_alerts.WARNING, patient_id=booking.patient_id,
                    care_recipient_id=None if is_self_booking(booking) else booking.care_recipient_id, booking_id=booking.id,
                    source_key=f"visit:{booking.id}", summary=task_summary(len(missed.titles)), now=now,
                    detail={"tasks": missed.titles, "days": [d.isoformat() for d in missed.days]})
                if raised is not None and raised.created:
                    created.append(raised.alert_id)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.warning("missed-task sweep failed booking=%s", booking_id, exc_info=True)
    return created

