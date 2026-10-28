"""Care plan tasks: edit the definitions, the day view, ticks and progress (slice B2).

Ticks are the one write here that several caregivers may make on the same row, so they are built to be repeated and to race:

- A tick is ``INSERT ... ON CONFLICT DO NOTHING`` on ``UNIQUE (task_id, for_date)``; an untick is a ``DELETE``. Repeating
  either changes nothing and answers the same.
- A tick takes ``FOR SHARE`` locks on the plan and on the booking it is made through, then re-reads their status. Plan
  editors take ``FOR UPDATE`` on the plan, so a tick can never land on a plan that was completed a moment earlier, and a visit
  that ends cannot be ticked into. Ticks do not block each other.
- "Today" is the Africa/Lagos date at the moment the request is handled (``care_plans.local_today``), not the client's.

Ticks and notes never bump the plan's ``version``: they overwrite nothing, and bumping it would make every tick a
``version_conflict`` for a nurse editing the same plan. Events carry ids and dates only, never health text.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.booking import Booking, BookingStatus
from app.db.models.care_plan import (
    CarePlan, CarePlanGoal, CarePlanObservation, CarePlanTask, CarePlanTaskCompletion,
)
from app.db.models.caregiver import Caregiver
from app.schemas.care_plan import MAX_TASKS, TaskCreate, TaskPatch
from app.services import care_plans as base
from app.services.care_plan_access import PlanAccess
from app.services.care_plans import (
    Actor, InvalidPlan, PlanNotEditable, TaskDateNotToday, TaskNotScheduled, TaskRemoved, VisitNotInProgress, actor_event,
    record_event, seal, unseal,
)

SLOT_ORDER = ("morning", "afternoon", "evening", "night")
RECENT_DAYS = 14


# --- when a task is scheduled -----------------------------------------------------------------------------------------

def _local_date(moment: datetime) -> date:
    """A naive-UTC timestamp as a date in HOME_TIMEZONE."""
    return moment.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.HOME_TIMEZONE)).date()


def plan_last_day(plan: CarePlan) -> date:
    return plan.start_date + timedelta(days=plan.duration_days - 1)


def scheduled_on(plan: CarePlan, task: CarePlanTask, day: date) -> bool:
    """A task is on a day when it is active, the day is inside the plan's dates, the task's weekdays include it, and the task
    existed that day (a task added today was never due last week; the day view and progress share this one rule)."""
    return (task.is_active and plan.start_date <= day <= plan_last_day(plan) and day.isoweekday() in task.days_of_week
            and _local_date(task.created_at) <= day)


# --- task definitions (nurses): routes 16-18 --------------------------------------------------------------------------

def _live_task_count(db: Session, plan_id: UUID) -> int:
    return db.query(CarePlanTask.id).filter(CarePlanTask.plan_id == plan_id, CarePlanTask.is_active.is_(True)).count()


def add_task(db: Session, actor: Actor, plan_id: UUID, payload: TaskCreate) -> Tuple[CarePlanTask, int]:
    try:
        plan = base._lock_editable(db, plan_id, payload.expected_version)
        if _live_task_count(db, plan.id) >= MAX_TASKS:
            raise InvalidPlan(f"a plan can have at most {MAX_TASKS} tasks")
        last = db.query(CarePlanTask.sort_order).filter(CarePlanTask.plan_id == plan.id).order_by(
            CarePlanTask.sort_order.desc()).first()
        task = CarePlanTask(plan_id=plan.id, title_encrypted=seal(payload.title), time_slot=payload.time_slot,
                            time_of_day=base._parse_time(payload.time_of_day), days_of_week=sorted(payload.days_of_week),
                            sort_order=(last[0] + 1) if last else 0)
        db.add(task)
        db.flush()
        actor_event(db, plan.id, actor, "task.add", {"task_id": str(task.id)})
        base._touch(plan)
        version = plan.version
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(task)
    return task, version


def _locked_task(db: Session, task_id: UUID) -> CarePlanTask:
    return db.query(CarePlanTask).filter(CarePlanTask.id == task_id).populate_existing().with_for_update().one()


def update_task(db: Session, actor: Actor, task: CarePlanTask, payload: TaskPatch) -> Tuple[CarePlanTask, int]:
    sent = payload.model_fields_set - {"expected_version"}
    try:
        plan = base._lock_editable(db, task.plan_id, payload.expected_version)
        task = _locked_task(db, task.id)
        if not task.is_active:
            raise TaskRemoved()
        changed: List[str] = []
        if "title" in sent:
            task.title_encrypted = seal(payload.title)
            changed.append("title")
        if "time_slot" in sent:
            task.time_slot = payload.time_slot
            changed.append("time_slot")
        if "time_of_day" in sent:
            task.time_of_day = base._parse_time(payload.time_of_day)
            changed.append("time_of_day")
        if "days_of_week" in sent:
            task.days_of_week = sorted(payload.days_of_week)
            changed.append("days_of_week")
        if changed:
            task.updated_at = datetime.utcnow()
            actor_event(db, plan.id, actor, "task.update", {"task_id": str(task.id), "fields": changed})
            base._touch(plan)
        version = plan.version
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(task)
    return task, version


def remove_task(db: Session, actor: Actor, task: CarePlanTask, expected_version: int) -> Tuple[CarePlanTask, int]:
    """Removing a task deactivates it. The row and every tick it has are kept; it simply stops being scheduled."""
    try:
        plan = base._lock_editable(db, task.plan_id, expected_version)
        task = _locked_task(db, task.id)
        if task.is_active:
            task.is_active = False
            task.updated_at = datetime.utcnow()
            actor_event(db, plan.id, actor, "task.remove", {"task_id": str(task.id)})
            base._touch(plan)
        version = plan.version
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(task)
    return task, version


def task_out(task: CarePlanTask) -> Dict[str, Any]:
    return base._task_out(task)


# --- the day view: route 19 -------------------------------------------------------------------------------------------

def _completions(db: Session, task_ids: List[UUID], first: date, last: date) -> Dict[Tuple[UUID, date], Tuple[Any, str]]:
    """(task, day) -> (the tick row, who ticked it)."""
    if not task_ids:
        return {}
    rows = (db.query(CarePlanTaskCompletion, Caregiver.full_name)
            .join(Caregiver, Caregiver.id == CarePlanTaskCompletion.completed_by_caregiver_id)
            .filter(CarePlanTaskCompletion.task_id.in_(task_ids), CarePlanTaskCompletion.for_date >= first,
                    CarePlanTaskCompletion.for_date <= last).all())
    return {(c.task_id, c.for_date): (c, name) for c, name in rows}


def day_view(db: Session, access: PlanAccess, plan: CarePlan, day: Optional[date]) -> Dict[str, Any]:
    today = base.local_today()
    day = day or today
    tasks = [t for t in base._tasks(db, plan.id) if scheduled_on(plan, t, day)]
    ticks = _completions(db, [t.id for t in tasks], day, day)
    grouped: Dict[str, List[Dict[str, Any]]] = {slot: [] for slot in SLOT_ORDER}
    for task in sorted(tasks, key=lambda t: (t.time_of_day is None, t.time_of_day or datetime.min.time(), t.sort_order)):
        tick = ticks.get((task.id, day))
        grouped[task.time_slot].append({
            "id": task.id, "title": unseal(task.title_encrypted, plan.id, "care_plan_tasks.title_encrypted") or "",
            "time_of_day": base._hhmm(task.time_of_day), "done": tick is not None,
            "done_by": tick[1] if tick else None, "done_at": tick[0].completed_at if tick else None,
        })
    slots = []
    for slot in SLOT_ORDER:
        if not grouped[slot]:
            continue
        times = [t["time_of_day"] for t in grouped[slot] if t["time_of_day"]]
        slots.append({"slot": slot, "time_of_day": min(times) if times else None, "tasks": grouped[slot]})
    return {"date": day, "is_today": day == today, "slots": slots,
            "can_tick": day == today and base.can_record_visit(access, plan)}


# --- ticking: route 20 ------------------------------------------------------------------------------------------------

def require_live_visit(db: Session, caregiver: Caregiver, booking_id: UUID) -> Booking:
    """Re-read the caller's booking under a share lock: a visit that is no longer in progress (the caregiver clocked out, an
    admin changed it) refuses the write even if access was resolved a moment ago."""
    booking = (db.query(Booking).filter(Booking.id == booking_id).populate_existing().with_for_update(read=True).first())
    if booking is None or booking.caregiver_id != caregiver.id or booking.status != BookingStatus.IN_PROGRESS:
        raise VisitNotInProgress()
    return booking


def lock_active_plan(db: Session, plan_id: UUID) -> CarePlan:
    """Share-lock the plan and refuse a completed or archived one. Editors hold ``FOR UPDATE``, so the status read here cannot
    change before the write that follows commits."""
    plan = db.query(CarePlan).filter(CarePlan.id == plan_id).populate_existing().with_for_update(read=True).one()
    if plan.status != "active":
        raise PlanNotEditable()
    return plan


def _completion_state(db: Session, task_id: UUID, day: date) -> Dict[str, Any]:
    row = (db.query(CarePlanTaskCompletion, Caregiver.full_name)
           .join(Caregiver, Caregiver.id == CarePlanTaskCompletion.completed_by_caregiver_id)
           .filter(CarePlanTaskCompletion.task_id == task_id, CarePlanTaskCompletion.for_date == day).first())
    if row is None:
        return {"task_id": task_id, "date": day, "done": False, "done_by": None, "done_at": None}
    return {"task_id": task_id, "date": day, "done": True, "done_by": row[1], "done_at": row[0].completed_at}


def set_completion(db: Session, caregiver: Caregiver, access: PlanAccess, task: CarePlanTask, day: date,
                   done: bool) -> Dict[str, Any]:
    """Tick or untick one task for today during the caller's in-progress visit. Idempotent either way."""
    try:
        plan = lock_active_plan(db, task.plan_id)
        booking = require_live_visit(db, caregiver, access.booking.id)
        if day != base.local_today():
            raise TaskDateNotToday()
        task = db.query(CarePlanTask).filter(CarePlanTask.id == task.id).populate_existing().one()
        if not task.is_active:
            raise TaskRemoved()
        if not scheduled_on(plan, task, day):
            raise TaskNotScheduled()
        if done:
            db.execute(insert(CarePlanTaskCompletion).values(
                task_id=task.id, for_date=day, completed_by_caregiver_id=caregiver.id, booking_id=booking.id,
                completed_at=datetime.utcnow(),
            ).on_conflict_do_nothing(constraint="uq_care_plan_task_completions_task_date"))
        else:
            removed = db.execute(
                delete(CarePlanTaskCompletion).where(CarePlanTaskCompletion.task_id == task.id,
                                                     CarePlanTaskCompletion.for_date == day)
                .returning(CarePlanTaskCompletion.completed_by_caregiver_id)).first()
            if removed is not None:
                record_event(db, plan.id, "caregiver", caregiver.id, "task.untick",
                             {"task_id": str(task.id), "for_date": day.isoformat(), "ticked_by": str(removed[0])})
        db.commit()
    except Exception:
        db.rollback()
        raise
    return _completion_state(db, task.id, day)


# --- progress: route 23 -----------------------------------------------------------------------------------------------

MAX_VISIT_SPAN = timedelta(hours=24)


def visit_days(db: Session, plan: CarePlan, first: date, last: date) -> List[date]:
    """Dates on which a visit for this plan's client was under way (any caregiver): a booking for the same subject that reached
    ``in_progress`` or ``completed``, from ``actual_start_time`` to its end in HOME_TIMEZONE (the scheduled date if it was never
    stamped). A visit that runs past midnight makes both days visit days, since ticks are allowed on both; a visit with no end
    (still in progress, or never closed) is counted for at most 24 hours."""
    query = db.query(func.coalesce(Booking.actual_start_time, Booking.scheduled_date), Booking.actual_end_time,
                     Booking.status).filter(Booking.status.in_((BookingStatus.IN_PROGRESS, BookingStatus.COMPLETED)))
    myself = func.lower(func.trim(Booking.booking_for)) == "myself"
    if plan.patient_id is not None:
        query = query.filter(Booking.patient_id == plan.patient_id, myself)
    else:
        query = query.filter(Booking.care_recipient_id == plan.care_recipient_id, ~myself)
    days: Set[date] = set()
    for started, ended, status in query.all():
        stop = ended if status == BookingStatus.COMPLETED and ended is not None and ended >= started else (
            datetime.utcnow() if status == BookingStatus.IN_PROGRESS else started)
        stop = min(max(stop, started), started + MAX_VISIT_SPAN)
        day = _local_date(started)
        while day <= _local_date(stop):
            days.add(day)
            day += timedelta(days=1)
    return sorted(d for d in days if first <= d <= last)


def progress(db: Session, plan: CarePlan) -> Dict[str, Any]:
    """Ticked / scheduled task occurrences, counted only over days that had a visit (a nurse who visits Monday, Wednesday and
    Friday cannot tick Tuesday, so counting Tuesday would make every plan look half done). Removed tasks drop out of both
    sides. With nothing to measure yet, ``overall_percent`` is null."""
    today = base.local_today()
    window_end = min(today, plan_last_day(plan))
    closed_at = min((m for m in (plan.completed_at, plan.archived_at) if m is not None), default=None)
    if closed_at is not None:  # a finished plan stops counting the day it was closed
        window_end = min(window_end, _local_date(closed_at))
    tasks = base._tasks(db, plan.id)
    days = visit_days(db, plan, plan.start_date, window_end) if window_end >= plan.start_date else []
    ticks: Set[Tuple[UUID, date]] = set()
    if days and tasks:
        ticks = set(_completions(db, [t.id for t in tasks], days[0], days[-1]))
    per_day = []
    for day in days:
        due = [t for t in tasks if scheduled_on(plan, t, day)]
        per_day.append({"date": day, "scheduled": len(due), "completed": sum(1 for t in due if (t.id, day) in ticks)})
    scheduled = sum(d["scheduled"] for d in per_day)
    completed = sum(d["completed"] for d in per_day)
    goal_counts = dict(db.query(CarePlanGoal.status, func.count()).filter(CarePlanGoal.plan_id == plan.id)
                       .group_by(CarePlanGoal.status).all())
    last_note = db.query(func.max(CarePlanObservation.created_at)).filter(CarePlanObservation.plan_id == plan.id).scalar()
    return {
        "overall_percent": round(100 * completed / scheduled) if scheduled else None,
        "scheduled": scheduled, "completed": completed, "visit_days": len(days),
        "day": max(0, min((today - plan.start_date).days + 1, plan.duration_days)), "of_days": plan.duration_days,
        "review_due_on": base._review_due(plan), "by_day": per_day[-RECENT_DAYS:],
        "goals": {name: goal_counts.get(name, 0) for name in ("active", "achieved", "discontinued")},
        "last_observation_at": last_note,
    }


def add_progress_percent(db: Session, items: List[Dict[str, Any]]) -> None:
    """Fill ``progress_percent`` on the assigned list's items (the bar on each card)."""
    plans = {p.id: p for p in db.query(CarePlan).filter(CarePlan.id.in_([i["id"] for i in items]))} if items else {}
    for item in items:
        item["progress_percent"] = progress(db, plans[item["id"]])["overall_percent"]
