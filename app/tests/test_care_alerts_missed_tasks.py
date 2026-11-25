"""Care alerts for missed care-plan tasks (slice 1c): the rule (pure functions) and the cron sweep.

Times are chosen in HOME_TIMEZONE (Africa/Lagos, UTC+1): Tuesday 2026-11-24, a visit from 08:00 to 12:00 local = 07:00 to 11:00 UTC."""

import json
from datetime import date, datetime, timedelta

import pytest

from app.core.security import decrypt
from app.db.models.booking import BookingStatus
from app.db.models.care_alert import CareAlert, CareAlertOccurrence
from app.db.models.care_plan import CarePlan, CarePlanTask
from app.services import care_alert_missed_tasks as missed
from app.services import care_alerts
from app.tests.care_alert_helpers import (  # noqa: F401  (outbox is a fixture)
    VISIT_DAY, VISIT_END_UTC, VISIT_START_UTC, add_family, add_task, give_consent, make_plan, outbox, patient_with_family,
    self_recipient, tick_task,
)
from app.tests.care_plan_helpers import relative_visit
from app.tests.factories import make_booking, make_caregiver, make_patient

AFTER = VISIT_END_UTC + timedelta(minutes=10)


def finished_visit(db, caregiver, patient, **over):
    fields = dict(status=BookingStatus.COMPLETED, scheduled_date=VISIT_START_UTC, scheduled_end_date=VISIT_END_UTC,
                  actual_start_time=VISIT_START_UTC + timedelta(minutes=5), actual_end_time=VISIT_END_UTC - timedelta(minutes=5),
                  booking_for="myself")
    fields.update(over)
    return make_booking(db, patient, caregiver, **fields)


def run(db, now=AFTER):
    return missed.sweep(db, now)


def alerts(db):
    db.expire_all()
    return db.query(CareAlert).all()


# --- the pure rule -------------------------------------------------------------------------------------------------------

def local(h, m=0):
    return datetime(2026, 11, 24, h, m)


@pytest.mark.parametrize("slot,start,end,expected", [
    ("morning", local(8), local(12), 240), ("morning", local(10), local(14), 120), ("afternoon", local(8), local(12), 0),
    ("afternoon", local(11), local(13), 60), ("evening", local(17), local(21), 240), ("night", local(20), local(23), 120),
    ("night", local(0), local(3), 180), ("night", local(22), datetime(2026, 11, 25, 2), 120),  # only the same date's bands count
    ("nonsense", local(8), local(12), 0)])
def test_slot_overlap_minutes(slot, start, end, expected):
    assert missed.slot_overlap_minutes(slot, VISIT_DAY, start, end) == expected


def plan_and_tasks(**task_over):
    plan = CarePlan(start_date=date(2026, 11, 1), duration_days=60)
    task = CarePlanTask(time_slot="morning", days_of_week=[1, 2, 3, 4, 5, 6, 7], is_active=True,
                        created_at=datetime(2026, 11, 1, 8), **task_over)
    return plan, task


def test_a_morning_task_is_due_in_a_morning_visit_and_not_in_an_evening_one():
    plan, task = plan_and_tasks()
    assert [d for _t, d in missed.due_tasks(plan, [task], VISIT_START_UTC, VISIT_END_UTC)] == [VISIT_DAY]
    assert missed.due_tasks(plan, [task], datetime(2026, 11, 24, 16), datetime(2026, 11, 24, 20)) == []  # 17:00-21:00 local


def test_a_brief_overlap_does_not_make_a_task_due():
    plan, task = plan_and_tasks()
    # 11:40-13:00 local touches the morning band for 20 minutes only
    assert missed.due_tasks(plan, [task], datetime(2026, 11, 24, 10, 40), datetime(2026, 11, 24, 12, 0)) == []


def test_weekday_and_plan_dates_decide_the_day():
    plan, task = plan_and_tasks()
    task.days_of_week = [1, 3]  # Monday and Wednesday: the visit is a Tuesday
    assert missed.due_tasks(plan, [task], VISIT_START_UTC, VISIT_END_UTC) == []
    task.days_of_week = [2]
    assert len(missed.due_tasks(plan, [task], VISIT_START_UTC, VISIT_END_UTC)) == 1
    plan.start_date = date(2026, 11, 25)  # the plan starts the day after
    assert missed.due_tasks(plan, [task], VISIT_START_UTC, VISIT_END_UTC) == []


def test_a_task_added_after_the_visit_began_is_never_due_in_it():
    plan, task = plan_and_tasks()
    task.created_at = VISIT_START_UTC + timedelta(minutes=20)
    assert missed.due_tasks(plan, [task], VISIT_START_UTC, VISIT_END_UTC) == []


def test_an_inactive_task_is_never_due():
    plan, task = plan_and_tasks()
    task.is_active = False
    assert missed.due_tasks(plan, [task], VISIT_START_UTC, VISIT_END_UTC) == []


def test_a_night_visit_across_midnight_covers_both_dates():
    plan, task = plan_and_tasks()
    task.time_slot = "night"
    # 22:00 local on the 24th to 04:00 local on the 25th = 21:00 UTC to 03:00 UTC
    due = missed.due_tasks(plan, [task], datetime(2026, 11, 24, 21), datetime(2026, 11, 25, 3))
    assert [d for _t, d in due] == [date(2026, 11, 24), date(2026, 11, 25)]


# --- the sweep ------------------------------------------------------------------------------------------------------------

def one_visit(db, *, tasks=(("Placeholder task one", "morning"),), family=False, **kw):
    nurse = make_caregiver(db)
    patient, recipient, fam = patient_with_family(db) if family else (make_patient(db), None, None)
    plan = make_plan(db, nurse, patient=patient)
    made = [add_task(db, plan, title, slot) for title, slot in tasks]
    booking = finished_visit(db, nurse, patient, **kw)
    return nurse, patient, plan, made, booking, fam


def test_an_unticked_due_task_raises_one_warning_alert(db, outbox):
    _n, patient, _p, [task], booking, _f = one_visit(db)
    created = run(db)
    [alert] = alerts(db)
    assert created == [alert.id]
    assert (alert.type, alert.severity, alert.status) == ("task_missed", "warning", "open")
    assert alert.patient_id == patient.id and alert.booking_id == booking.id and alert.care_recipient_id is None
    assert alert.summary == "1 care task not marked done by the end of the visit."
    detail = json.loads(decrypt(alert.detail_encrypted))
    assert detail == {"tasks": ["Placeholder task one"], "days": ["2026-11-24"]}
    assert db.query(CareAlertOccurrence).one().source_key == f"visit:{booking.id}"


def test_a_ticked_task_is_not_missed(db, outbox):
    nurse, _pt, _p, [task], booking, _f = one_visit(db)
    tick_task(db, task, VISIT_DAY, nurse, booking)
    assert run(db) == [] and alerts(db) == []


def test_a_tick_by_another_visit_the_same_day_counts(db, outbox):
    nurse, patient, _p, [task], booking, _f = one_visit(db)
    other = make_caregiver(db)
    other_booking = finished_visit(db, other, patient, scheduled_date=VISIT_START_UTC + timedelta(hours=9),
                                   scheduled_end_date=VISIT_END_UTC + timedelta(hours=9),
                                   actual_start_time=VISIT_START_UTC + timedelta(hours=9),
                                   actual_end_time=VISIT_END_UTC + timedelta(hours=9))
    tick_task(db, task, VISIT_DAY, other, other_booking)
    run(db, now=VISIT_END_UTC + timedelta(hours=10))
    assert alerts(db) == []


def test_only_the_unticked_tasks_are_counted(db, outbox):
    nurse, _pt, _p, [a, b, c], booking, _f = one_visit(
        db, tasks=(("Placeholder A", "morning"), ("Placeholder B", "morning"), ("Placeholder C", "evening")))
    tick_task(db, a, VISIT_DAY, nurse, booking)
    run(db)
    [alert] = alerts(db)
    assert json.loads(decrypt(alert.detail_encrypted))["tasks"] == ["Placeholder B"]  # C is an evening task: not due in this visit


def test_the_sweep_is_idempotent_and_judges_a_visit_once(db, outbox):
    one_visit(db)
    assert len(run(db)) == 1
    assert run(db) == [] and run(db, now=AFTER + timedelta(minutes=30)) == []
    assert [a.occurrence_count for a in alerts(db)] == [1]


@pytest.mark.parametrize("status", [BookingStatus.IN_PROGRESS, BookingStatus.ACCEPTED, BookingStatus.CANCELLED,
                                    BookingStatus.DECLINED, BookingStatus.PENDING])
def test_only_a_completed_visit_is_judged(db, outbox, status):
    one_visit(db, status=status)
    assert run(db) == []


def test_a_visit_that_never_started_is_not_judged(db, outbox):
    one_visit(db, actual_start_time=None)
    assert run(db) == []


def test_a_visit_is_judged_only_after_its_end_and_within_the_lookback(db, outbox):
    one_visit(db)
    assert run(db, now=VISIT_END_UTC - timedelta(minutes=10)) == []  # not over yet (a corrected status, or a clock quirk)
    assert run(db, now=VISIT_END_UTC + timedelta(hours=7)) == []  # older than the 6 hour lookback
    assert len(run(db, now=VISIT_END_UTC + timedelta(hours=5))) == 1


def test_no_plan_no_alert(db, outbox):
    nurse = make_caregiver(db)
    patient = make_patient(db)
    finished_visit(db, nurse, patient)
    assert run(db) == []


def test_only_an_active_plan_counts(db, outbox):
    _n, _pt, plan, _t, _b, _f = one_visit(db)
    plan.status = "archived"
    db.commit()
    assert run(db) == []


def test_a_visit_booked_for_a_relative_uses_the_recipients_plan(db, outbox):
    nurse = make_caregiver(db)
    booker, recipient, booking = relative_visit(db, nurse, status=BookingStatus.COMPLETED, scheduled_date=VISIT_START_UTC,
                                                scheduled_end_date=VISIT_END_UTC,
                                                actual_start_time=VISIT_START_UTC, actual_end_time=VISIT_END_UTC)
    plan = make_plan(db, nurse, recipient=recipient)
    add_task(db, plan, "Placeholder relative task")
    run(db)
    [alert] = alerts(db)
    assert alert.patient_id == booker.id and alert.care_recipient_id == recipient.id and alert.booking_id == booking.id


def test_the_family_hears_about_a_missed_task_without_the_task_name(db, outbox):
    _n, _pt, _p, _t, _b, family = one_visit(db, family=True)
    from app.services import care_alert_delivery
    care_alert_delivery.deliver_all(run(db))
    assert outbox.to() == [family.email]
    body = outbox.body(family.email)
    assert "A care task planned for Ada's visit was not marked as done." in body
    assert "Placeholder task one" not in body and "Okafor" not in body
    assert alerts(db)[0].family_notified_count == 1


def test_several_missed_tasks_read_in_the_plural(db, outbox):
    _n, _pt, _p, _t, _b, family = one_visit(db, family=True, tasks=(("Placeholder A", "morning"), ("Placeholder B", "morning")))
    from app.services import care_alert_delivery
    care_alert_delivery.deliver_all(run(db))
    assert "Some care tasks planned for Ada's visit were not marked as done." in outbox.body(family.email)


def test_the_family_of_a_relatives_visit_is_the_recipients_family(db, outbox):
    nurse = make_caregiver(db)
    booker, recipient, booking = relative_visit(db, nurse, status=BookingStatus.COMPLETED, scheduled_date=VISIT_START_UTC,
                                                scheduled_end_date=VISIT_END_UTC,
                                                actual_start_time=VISIT_START_UTC, actual_end_time=VISIT_END_UTC)
    give_consent(db, recipient)
    family = add_family(db, recipient)
    unrelated = add_family(db, self_recipient(db, make_patient(db)))
    add_task(db, make_plan(db, nurse, recipient=recipient), "Placeholder relative task")
    from app.services import care_alert_delivery
    care_alert_delivery.deliver_all(run(db))
    assert outbox.to() == [family.email]
    assert "Placeholder Relative's visit".split()[0] in outbox.body(family.email)  # the recipient's first name, not the booker's


def test_a_second_visit_the_same_day_is_absorbed_by_the_open_alert(db, outbox):
    nurse, patient, plan, _t, _b, _f = one_visit(db)
    run(db)
    later = finished_visit(db, make_caregiver(db), patient, scheduled_date=VISIT_START_UTC + timedelta(hours=2),
                           scheduled_end_date=VISIT_END_UTC + timedelta(hours=2),
                           actual_start_time=VISIT_START_UTC + timedelta(hours=2),
                           actual_end_time=VISIT_END_UTC + timedelta(hours=2))
    assert run(db, now=VISIT_END_UTC + timedelta(hours=3)) == []  # nothing new to deliver
    [alert] = alerts(db)
    assert alert.occurrence_count == 2 and alert.booking_id == later.id
