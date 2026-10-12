"""A3 slice 1: caregivers are credited for completed visits (and compensated for compensable late cancellations),
the money is held for a dispute window, then released to the withdrawable balance by a job. Admin can hold or void."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from app.db.models.audit import AdminAuditLog
from app.db.models.booking import Booking, BookingStatus
from app.db.models.caregiver import CaregiverTransaction, CaregiverWallet
from app.db.session import SessionLocal
from app.db.session import engine as suite_engine
from app.main import app as fastapi_app
from app.schemas.booking import BookingCreate
from app.db.models.booking_settings import BookingSettings
from app.services import booking_cancellation as cancellation
from app.services import earnings
from app.services.admin_bookings import AdminBookingService
from app.services.booking import BookingService
from app.tests.factories import auth_headers, make_admin, make_booking, make_caregiver, make_caregiver_wallet, make_patient
from app.tests.policy_helpers import set_policy
from app.tests.test_cancellation_tiers import start_moving
from app.tests.whatsapp_notify_helpers import Chat, accepted_booking, fake_meta, onboarded  # noqa: F401

ACTION = "/api/v1/booking/caregiver/action"
WALLET = "/api/v1/caregiver/wallet"
ADMIN_EARNINGS = "/api/v1/admin/earnings"
SERVICE_FEE = 4_000_000  # kobo; a total of 4_400_000 with a 10% platform fee


def in_progress(db, cg=None, patient=None, service_fee=SERVICE_FEE, **kw):
    cg = cg or make_caregiver(db)
    kw.setdefault("platform_fee_kobo", service_fee // 10)
    kw.setdefault("total_amount_kobo", service_fee + kw["platform_fee_kobo"])
    return cg, make_booking(db, patient or make_patient(db), cg, status=BookingStatus.IN_PROGRESS,
                            service_fee_kobo=service_fee, **kw)


def complete(client, booking, cg):
    return client.post(f"{ACTION}/{booking.id}", json={"action": "complete"}, headers=auth_headers(cg.user))


def txs(db, caregiver=None, **filters):
    db.expire_all()
    q = db.query(CaregiverTransaction)
    if caregiver is not None:
        q = q.filter_by(caregiver_id=caregiver.id)
    return q.filter_by(**filters).order_by(CaregiverTransaction.created_at).all()


def balance(db, caregiver):
    db.expire_all()
    wallet = db.query(CaregiverWallet).filter_by(caregiver_id=caregiver.id).first()
    return wallet.balance if wallet else 0


def completed_earning(db, client, **kw):
    cg, booking = in_progress(db, **kw)
    assert complete(client, booking, cg).status_code == 200
    return cg, booking, txs(db, cg, type="earning")[0]


# --- the credit on completion -------------------------------------------------------------------------------------


def test_completing_a_visit_credits_one_pending_earning_of_80_percent_of_the_service_fee(db, client):
    cg, booking = in_progress(db)
    before = datetime.utcnow()
    r = complete(client, booking, cg)
    assert r.status_code == 200
    [tx] = txs(db, cg)
    assert (tx.type, tx.status, tx.amount) == ("earning", "pending_clearance", 3_200_000)
    assert (tx.booking_id, tx.base_amount_kobo, tx.share_percent) == (booking.id, SERVICE_FEE, 80)
    db.expire_all()
    done = db.get(Booking, booking.id)
    assert tx.available_at == done.actual_end_time + timedelta(hours=24)
    assert before <= done.actual_end_time <= datetime.utcnow()
    assert balance(db, cg) == 0  # nothing is withdrawable yet


def test_the_share_is_of_the_service_fee_not_the_total_nor_the_platform_fee(db, client):
    cg, booking = in_progress(db, service_fee=1_000_000, platform_fee_kobo=900_000, total_amount_kobo=1_900_000)
    complete(client, booking, cg)
    assert txs(db, cg)[0].amount == 800_000


@pytest.mark.parametrize("fee,percent,expected", [
    (1, 80, 1), (2, 80, 2), (3, 80, 2), (4, 80, 3), (5, 80, 4),       # 0.8 -> 1, 1.6 -> 2, 2.4 -> 2, 3.2 -> 3
    (5, 50, 3), (7, 50, 4), (1, 50, 1), (3, 50, 2),                   # a tie rounds up
    (1_234_567, 80, 987_654), (1_234_569, 80, 987_655),               # .6 and .2 fractions
    (100, 0, 0), (100, 100, 100),
])
def test_the_share_is_integer_kobo_rounded_half_up(fee, percent, expected):
    assert earnings.share(fee, percent) == expected


def test_the_credit_uses_the_rounding_end_to_end(db, client):
    cg, booking = in_progress(db, service_fee=1_234_569,
                              earnings_terms={"visit_share_percent": 80, "hold_hours": 24, "late_fee_share_percent": 80})
    complete(client, booking, cg)
    assert txs(db, cg)[0].amount == 987_655 and isinstance(txs(db, cg)[0].amount, int)


def test_a_booking_with_no_caregiver_credits_nobody(db):
    booking = make_booking(db, make_patient(db), None, status=BookingStatus.COMPLETED)
    assert earnings.credit_visit(db, booking) is None
    assert txs(db) == []


# --- snapshot ------------------------------------------------------------------------------------------------------


def _create_body():
    return dict(service_type="nurse", assistance_type="x", duration_hours=3, scheduled_date="2030-01-01T10:00:00",
                patient_address="1 Test Close", patient_latitude=6.4, patient_longitude=3.4, patient_city="Lagos",
                patient_state="Lagos", booking_for="myself")


def test_the_terms_are_copied_onto_the_booking_when_it_is_created_and_a_later_edit_does_not_change_the_earning(db, client):
    db.add(BookingSettings(base_fee_kobo=500_000, platform_fee_percent=10, multipliers={"nurse": 1.0}))
    db.commit()
    set_policy(db, caregiver_visit_share_percent=70, earning_hold_hours=48)
    patient = make_patient(db)
    booking = BookingService(db).insert_booking(patient.id, BookingCreate(**_create_body()))
    assert booking.earnings_terms == {"visit_share_percent": 70, "hold_hours": 48, "late_fee_share_percent": 80}

    set_policy(db, caregiver_visit_share_percent=90, earning_hold_hours=1)  # a later change
    cg = make_caregiver(db)
    booking.caregiver_id, booking.status = cg.id, BookingStatus.IN_PROGRESS
    db.commit()
    assert complete(client, booking, cg).status_code == 200
    tx = txs(db, cg)[0]
    assert tx.share_percent == 70 and tx.amount == earnings.share(booking.service_fee_kobo, 70)
    db.expire_all()
    assert tx.available_at == db.get(Booking, booking.id).actual_end_time + timedelta(hours=48)


def test_a_booking_without_a_snapshot_uses_the_live_policy(db, client):
    set_policy(db, caregiver_visit_share_percent=60, earning_hold_hours=2)
    cg, booking = in_progress(db)
    complete(client, booking, cg)
    tx = txs(db, cg)[0]
    assert tx.share_percent == 60 and tx.amount == 2_400_000


def test_a_damaged_snapshot_falls_back_to_the_live_policy(db, client):
    cg, booking = in_progress(db, earnings_terms={"visit_share_percent": "lots"})
    complete(client, booking, cg)
    assert txs(db, cg)[0].share_percent == 80


# --- idempotency and the lock --------------------------------------------------------------------------------------


def test_completing_twice_credits_once(db, client):
    cg, booking = in_progress(db)
    assert complete(client, booking, cg).status_code == 200
    assert complete(client, booking, cg).status_code == 409
    assert len(txs(db, cg)) == 1


def test_the_service_credit_is_idempotent(db):
    cg, booking = in_progress(db)
    first = earnings.credit_visit(db, booking)
    db.commit()
    second = earnings.credit_visit(db, booking)
    db.commit()
    assert first.id == second.id and len(txs(db, cg)) == 1


def test_the_database_refuses_a_second_earning_for_a_booking(db):
    cg, booking = in_progress(db)
    for _ in range(2):
        db.add(CaregiverTransaction(caregiver_id=cg.id, booking_id=booking.id, type="earning", amount=1,
                                    status="pending_clearance"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_withdrawals_are_not_limited_by_the_booking_uniqueness(db):
    cg = make_caregiver(db)
    for _ in range(2):
        db.add(CaregiverTransaction(caregiver_id=cg.id, type="withdrawal", amount=1, status="pending"))
    db.commit()
    assert len(txs(db, cg)) == 2


def test_two_caregiver_taps_at_once_credit_exactly_once(db):
    cg, booking = in_progress(db)
    headers = auth_headers(cg.user)
    barrier = threading.Barrier(4)

    def tap(_):
        with TestClient(fastapi_app, raise_server_exceptions=False) as c:
            barrier.wait()
            return c.post(f"{ACTION}/{booking.id}", json={"action": "complete"}, headers=headers).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = sorted(pool.map(tap, range(4)))
    assert codes == [200, 409, 409, 409], codes
    assert len(txs(db, cg)) == 1


def test_an_admin_completion_racing_the_caregiver_credits_once(db):
    cg, booking = in_progress(db)
    admin = make_admin(db, ["super_admin"], password=None)
    headers, cg_headers = auth_headers(admin), auth_headers(cg.user)
    barrier = threading.Barrier(2)

    def go(which):
        with TestClient(fastapi_app, raise_server_exceptions=False) as c:
            barrier.wait()
            if which == "caregiver":
                return c.post(f"{ACTION}/{booking.id}", json={"action": "complete"}, headers=cg_headers).status_code
            return c.post(f"/api/v1/admin/bookings/{booking.id}/correct-status",
                          json={"status": "completed", "reason": "forgot to check out"}, headers=headers).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(go, ["caregiver", "admin"]))
    assert 200 in codes and all(c in (200, 409) for c in codes), codes
    assert len(txs(db, cg)) == 1


def test_an_admin_status_correction_to_completed_credits_the_earning(db, client):
    cg, booking = in_progress(db)
    r = client.post(f"/api/v1/admin/bookings/{booking.id}/correct-status",
                    json={"status": "completed", "reason": "forgot to check out"},
                    headers=auth_headers(make_admin(db, ["super_admin"], password=None)))
    assert r.status_code == 200, r.text
    assert [(t.type, t.amount, t.status) for t in txs(db, cg)] == [("earning", 3_200_000, "pending_clearance")]


# --- the release job -----------------------------------------------------------------------------------------------


def test_the_job_releases_only_what_is_due_and_moves_it_to_the_available_balance(db, client):
    cg, booking, tx = completed_earning(db, client)
    assert earnings.release_due(db, now=tx.available_at - timedelta(seconds=1)) == 0
    assert txs(db, cg)[0].status == "pending_clearance" and balance(db, cg) == 0

    assert earnings.release_due(db, now=tx.available_at) == 1
    [done] = txs(db, cg)
    assert done.status == "successful" and done.released_at == tx.available_at
    assert balance(db, cg) == 3_200_000


def test_the_job_is_idempotent(db, client):
    cg, booking, tx = completed_earning(db, client)
    later = tx.available_at + timedelta(hours=1)
    assert earnings.release_due(db, now=later) == 1
    assert earnings.release_due(db, now=later) == 0
    assert balance(db, cg) == 3_200_000


def test_the_job_adds_to_an_existing_balance_and_batches(db, client):
    cg = make_caregiver(db)
    make_caregiver_wallet(db, cg, balance_kobo=1_000)
    for _ in range(5):
        _, b = in_progress(db, cg=cg)
        assert complete(client, b, cg).status_code == 200
    later = datetime.utcnow() + timedelta(days=2)
    assert earnings.release_due(db, now=later, limit=2) == 5  # three batches
    assert balance(db, cg) == 1_000 + 5 * 3_200_000


def test_the_job_never_touches_held_or_voided_rows(db, client):
    cg, booking, tx = completed_earning(db, client)
    cg2, b2, tx2 = completed_earning(db, client)
    admin = make_admin(db, ["super_admin"], password=None)
    earnings.hold(db, tx.id, admin_id=admin.id, reason="dispute")
    earnings.void(db, tx2.id, admin_id=admin.id, reason="no-show")
    assert earnings.release_due(db, now=datetime.utcnow() + timedelta(days=30)) == 0
    assert (txs(db, cg)[0].status, txs(db, cg2)[0].status) == ("held", "voided")
    assert balance(db, cg) == balance(db, cg2) == 0


def test_the_job_skips_a_row_someone_else_has_locked(db, client):
    cg, booking, tx = completed_earning(db, client)
    cg2, b2, tx2 = completed_earning(db, client)
    other = SessionLocal()
    try:
        other.query(CaregiverTransaction).filter_by(id=tx.id).with_for_update().one()  # e.g. an admin mid-hold
        assert earnings.release_due(db, now=datetime.utcnow() + timedelta(days=2)) == 1
    finally:
        other.rollback()
        other.close()
    assert txs(db, cg)[0].status == "pending_clearance" and txs(db, cg2)[0].status == "successful"


def test_two_workers_releasing_at_once_pay_each_row_once(db, client):
    cg = make_caregiver(db)
    for _ in range(6):
        _, b = in_progress(db, cg=cg)
        complete(client, b, cg)
    later = datetime.utcnow() + timedelta(days=2)
    barrier = threading.Barrier(3)

    def worker(_):
        s = SessionLocal()
        try:
            barrier.wait()
            return earnings.release_due(s, now=later)
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=3) as pool:
        counts = list(pool.map(worker, range(3)))
    assert sum(counts) == 6, counts
    assert balance(db, cg) == 6 * 3_200_000


def test_the_job_is_registered_in_the_worker():
    from app import worker

    assert worker.earnings_release_job in worker.WorkerSettings.functions
    assert "earnings_release_job" in {c.coroutine.__name__ for c in worker.WorkerSettings.cron_jobs}


# --- admin: hold, release the hold, void ---------------------------------------------------------------------------


def audit(db, tx, action):
    db.expire_all()
    return db.query(AdminAuditLog).filter_by(entity_type="caregiver_transaction", entity_id=str(tx.id), action=action).all()


def test_finance_can_hold_and_release_a_hold_and_each_is_audited(db, client):
    cg, booking, tx = completed_earning(db, client)
    finance = auth_headers(make_admin(db, ["finance"], password=None))
    r = client.post(f"{ADMIN_EARNINGS}/{tx.id}/hold", json={"reason": "Family disputes the visit"}, headers=finance)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "held" and r.json()["status_reason"] == "Family disputes the visit"
    [row] = audit(db, tx, "earning.hold")
    assert row.before["status"] == "pending_clearance" and row.after["status"] == "held"
    assert row.reason == "Family disputes the visit"

    later = datetime.utcnow() + timedelta(days=2)
    assert earnings.release_due(db, now=later) == 0

    r = client.post(f"{ADMIN_EARNINGS}/{tx.id}/release-hold", json={"reason": "Cleared"}, headers=finance)
    assert r.status_code == 200 and r.json()["status"] == "pending_clearance"
    assert len(audit(db, tx, "earning.release_hold")) == 1
    assert earnings.release_due(db, now=later) == 1  # the original available_at has passed: it clears
    assert balance(db, cg) == 3_200_000


def test_voiding_stops_the_money_for_good(db, client):
    cg, booking, tx = completed_earning(db, client)
    boss = auth_headers(make_admin(db, ["super_admin"], password=None))
    r = client.post(f"{ADMIN_EARNINGS}/{tx.id}/void", json={"reason": "Caregiver no-show"}, headers=boss)
    assert r.status_code == 200 and r.json()["status"] == "voided"
    assert len(audit(db, tx, "earning.void")) == 1
    assert earnings.release_due(db, now=datetime.utcnow() + timedelta(days=30)) == 0
    assert balance(db, cg) == 0
    for action in ("hold", "release-hold", "void"):
        assert client.post(f"{ADMIN_EARNINGS}/{tx.id}/{action}", json={"reason": "again"}, headers=boss).status_code == 409


def test_a_held_earning_can_be_voided(db, client):
    cg, booking, tx = completed_earning(db, client)
    earnings.hold(db, tx.id, admin_id=make_admin(db, ["finance"], password=None).id, reason="look into it")
    boss = auth_headers(make_admin(db, ["super_admin"], password=None))
    assert client.post(f"{ADMIN_EARNINGS}/{tx.id}/void", json={"reason": "confirmed no-show"}, headers=boss).status_code == 200


def test_a_released_earning_cannot_be_held_or_voided(db, client):
    cg, booking, tx = completed_earning(db, client)
    earnings.release_due(db, now=tx.available_at)
    boss = auth_headers(make_admin(db, ["super_admin"], password=None))
    for action in ("hold", "void"):
        r = client.post(f"{ADMIN_EARNINGS}/{tx.id}/{action}", json={"reason": "too late"}, headers=boss)
        assert r.status_code == 409 and "ledger adjustment" in r.json()["detail"]
    assert balance(db, cg) == 3_200_000 and not audit(db, tx, "earning.hold")


def test_an_earning_not_on_hold_cannot_have_a_hold_released_and_a_hold_cannot_be_doubled(db, client):
    cg, booking, tx = completed_earning(db, client)
    finance = auth_headers(make_admin(db, ["finance"], password=None))
    assert client.post(f"{ADMIN_EARNINGS}/{tx.id}/release-hold", json={"reason": "x"}, headers=finance).status_code == 409
    assert client.post(f"{ADMIN_EARNINGS}/{tx.id}/hold", json={"reason": "x"}, headers=finance).status_code == 200
    assert client.post(f"{ADMIN_EARNINGS}/{tx.id}/hold", json={"reason": "x"}, headers=finance).status_code == 409


def test_unknown_earning_and_bad_bodies(db, client):
    boss = auth_headers(make_admin(db, ["super_admin"], password=None))
    assert client.post(f"{ADMIN_EARNINGS}/00000000-0000-0000-0000-000000000000/hold", json={"reason": "x"},
                       headers=boss).status_code == 404
    cg, booking, tx = completed_earning(db, client)
    assert client.post(f"{ADMIN_EARNINGS}/{tx.id}/hold", json={"reason": "   "}, headers=boss).status_code == 422
    assert client.post(f"{ADMIN_EARNINGS}/{tx.id}/hold", json={}, headers=boss).status_code == 422
    assert client.post(f"{ADMIN_EARNINGS}/{tx.id}/hold", json={"reason": "x", "amount": 1}, headers=boss).status_code == 422
    assert client.post(f"{ADMIN_EARNINGS}/not-a-uuid/hold", json={"reason": "x"}, headers=boss).status_code == 422


def test_a_withdrawal_cannot_be_held_through_the_earnings_api(db, client):
    cg = make_caregiver(db)
    w = CaregiverTransaction(caregiver_id=cg.id, type="withdrawal", amount=1, status="pending")
    db.add(w)
    db.commit()
    boss = auth_headers(make_admin(db, ["super_admin"], password=None))
    assert client.post(f"{ADMIN_EARNINGS}/{w.id}/hold", json={"reason": "x"}, headers=boss).status_code == 404


# role -> (may list, may hold / release hold, may void)
ROLES = {
    "super_admin": (True, True, True), "finance": (True, True, False), "operations": (False, False, False),
    "support": (False, False, False), "verification": (False, False, False), "clinical_reviewer": (False, False, False),
    "read_only_analyst": (False, False, False),
}


@pytest.mark.parametrize("role", sorted(ROLES))
def test_role_matrix(db, client, role):
    may_list, may_hold, may_void = ROLES[role]
    cg, booking, tx = completed_earning(db, client)
    h = auth_headers(make_admin(db, [role], password=None))
    assert (client.get(ADMIN_EARNINGS, headers=h).status_code == 200) is may_list
    r = client.post(f"{ADMIN_EARNINGS}/{tx.id}/hold", json={"reason": "x"}, headers=h)
    assert (r.status_code == 200) is may_hold, r.text
    if not may_hold:
        assert r.status_code == 403
    r = client.post(f"{ADMIN_EARNINGS}/{tx.id}/release-hold", json={"reason": "x"}, headers=h)
    assert r.status_code == (200 if may_hold else 403)
    r = client.post(f"{ADMIN_EARNINGS}/{tx.id}/void", json={"reason": "x"}, headers=h)
    assert r.status_code == (200 if may_void else 403)


def test_the_earnings_api_needs_an_admin(db, client):
    cg, booking, tx = completed_earning(db, client)
    for headers in ({}, auth_headers(cg.user), auth_headers(make_patient(db).user)):
        assert client.get(ADMIN_EARNINGS, headers=headers).status_code in (401, 403)
        assert client.post(f"{ADMIN_EARNINGS}/{tx.id}/void", json={"reason": "x"}, headers=headers).status_code in (401, 403)
    assert txs(db, cg)[0].status == "pending_clearance"


def test_listing_filters_and_total(db, client):
    cg, booking, tx = completed_earning(db, client)
    cg2, b2, tx2 = completed_earning(db, client)
    earnings.hold(db, tx2.id, admin_id=make_admin(db, ["finance"], password=None).id, reason="x")
    h = auth_headers(make_admin(db, ["finance"], password=None))
    assert client.get(ADMIN_EARNINGS, headers=h).json()["total"] == 2
    held = client.get(ADMIN_EARNINGS, params={"status": "held"}, headers=h).json()
    assert held["total"] == 1 and held["items"][0]["id"] == str(tx2.id) and held["items"][0]["booking_code"] == b2.booking_code
    by_cg = client.get(ADMIN_EARNINGS, params={"caregiver_id": str(cg.id)}, headers=h).json()
    assert [i["id"] for i in by_cg["items"]] == [str(tx.id)]
    assert client.get(ADMIN_EARNINGS, params={"status": "bogus"}, headers=h).status_code == 422


# --- no-show -------------------------------------------------------------------------------------------------------


def test_flagging_a_no_show_holds_a_pending_earning_for_that_booking(db, client):
    cg, booking, tx = completed_earning(db, client)
    admin = make_admin(db, ["operations"], password=None)
    booking = db.get(Booking, booking.id)
    booking.status, booking.actual_start_time = BookingStatus.ACCEPTED, None  # the shape the flag guard accepts
    booking.scheduled_date = datetime.utcnow() - timedelta(hours=1)
    db.commit()
    AdminBookingService(db).flag_no_show(booking.id, admin, "never showed up", None)
    [held] = txs(db, cg)
    assert held.status == "held" and "No-show" in held.status_reason and held.status_changed_by == admin.id
    assert len(audit(db, held, "earning.hold")) == 1


def test_a_visit_completed_after_a_no_show_flag_is_credited_already_held(db, client):
    cg = make_caregiver(db)
    booking = make_booking(db, make_patient(db), cg, status=BookingStatus.ACCEPTED, visit_code="1234",
                           scheduled_date=datetime.utcnow() - timedelta(hours=1),
                           scheduled_end_date=datetime.utcnow() + timedelta(hours=3))
    admin = make_admin(db, ["operations"], password=None)
    AdminBookingService(db).flag_no_show(booking.id, admin, "not there at start time", None)
    assert client.post(f"{ACTION}/{booking.id}", json={"action": "start", "visit_code": "1234"},
                       headers=auth_headers(cg.user)).status_code == 200
    assert complete(client, booking, cg).status_code == 200
    [tx] = txs(db, cg)
    assert tx.status == "held" and tx.amount > 0 and tx.status_changed_by == admin.id
    assert earnings.release_due(db, now=datetime.utcnow() + timedelta(days=30)) == 0
    assert balance(db, cg) == 0


# --- late-cancel compensation ---------------------------------------------------------------------------------------


@pytest.fixture
def chat(db, fake_meta):
    c = Chat(db, fake_meta)
    onboarded(c)
    return c


def test_a_compensable_late_cancel_pays_the_caregiver_80_percent_of_the_fee_kept(db, chat):
    booking, cg = accepted_booking(db, chat, hours_ahead=1.5)
    start_moving(db, booking, cg, moved_m=600)
    cancellation.cancel_by_booker(db, booking.id)
    db.expire_all()
    b = db.get(Booking, booking.id)
    assert b.late_cancel_compensable is True
    fee = b.total_amount_kobo - b.refund_amount_kobo  # 50% of the service fee
    [tx] = txs(db, cg)
    assert (tx.type, tx.status, tx.base_amount_kobo, tx.share_percent) == ("late_cancel_compensation", "pending_clearance", fee, 80)
    assert tx.amount == earnings.share(fee, 80) and tx.amount > 0
    assert tx.available_at > datetime.utcnow() + timedelta(hours=23)
    assert balance(db, cg) == 0


def test_a_late_cancel_that_is_not_compensable_pays_nothing(db, chat):
    booking, cg = accepted_booking(db, chat, hours_ahead=1)  # inside the late window, but the caregiver never set out
    cancellation.cancel_by_booker(db, booking.id)
    db.expire_all()
    assert db.get(Booking, booking.id).cancel_fee_tier == "late" and txs(db, cg) == []


def test_a_standard_tier_cancel_pays_nothing_even_though_a_fee_is_kept(db, chat):
    booking, cg = accepted_booking(db, chat, hours_ahead=6)
    cancellation.cancel_by_booker(db, booking.id)
    assert txs(db, cg) == []


def test_a_compensable_late_cancel_with_no_fee_kept_pays_nothing(db, chat):
    set_policy(db, cancel_fee_percent=0, cancel_late_fee_percent=0)
    booking, cg = accepted_booking(db, chat, hours_ahead=1.5)
    start_moving(db, booking, cg, moved_m=600)
    cancellation.cancel_by_booker(db, booking.id)
    assert txs(db, cg) == []


def test_the_compensation_is_credited_once_and_uses_the_snapshot_share(db, chat):
    booking, cg = accepted_booking(db, chat, hours_ahead=1.5, earnings_terms={
        "visit_share_percent": 80, "hold_hours": 6, "late_fee_share_percent": 60})
    set_policy(db, caregiver_late_fee_share_percent=10)  # a later change: ignored
    start_moving(db, booking, cg, moved_m=600)
    assert cancellation.cancel_by_booker(db, booking.id).status == "cancelled"
    assert cancellation.cancel_by_booker(db, booking.id).status == "already_cancelled"
    [tx] = txs(db, cg)
    assert tx.share_percent == 60 and tx.available_at < datetime.utcnow() + timedelta(hours=7)


def test_a_booking_that_paid_out_a_visit_cannot_also_be_cancelled_compensated(db):
    # the two credits are different types, so the constraint allows one of each per booking, and a completed booking is
    # refused by the cancellation rules anyway
    cg, booking = in_progress(db)
    complete_booking = earnings.credit_visit(db, booking)
    db.commit()
    assert complete_booking is not None
    assert cancellation.terms_for(db, db.get(Booking, booking.id), datetime.utcnow()).allowed is False


# --- wallet balances -----------------------------------------------------------------------------------------------


def test_wallet_keeps_the_old_fields_and_adds_pending_and_available(db, client):
    cg, booking, tx = completed_earning(db, client)
    cg2_booking = in_progress(db, cg=cg)[1]
    complete(client, cg2_booking, cg)
    held = txs(db, cg, booking_id=cg2_booking.id)[0]
    earnings.hold(db, held.id, admin_id=make_admin(db, ["finance"], password=None).id, reason="x")
    earnings.release_due(db, now=tx.available_at)  # the first one clears
    third = in_progress(db, cg=cg)[1]
    complete(client, third, cg)

    r = client.get(WALLET, headers=auth_headers(cg.user))
    assert r.status_code == 200
    body = r.json()
    assert body["balance"] == body["available_balance"] == 3_200_000
    assert body["total_earned"] == 3_200_000 and body["total_withdrawn"] == 0 and body["currency"] == "NGN"
    assert body["pending_balance"] == 3_200_000 and body["held_balance"] == 3_200_000 and body["total_pending"] == 6_400_000
    pending = txs(db, cg, booking_id=third.id)[0]
    assert body["next_available_at"].startswith(pending.available_at.isoformat()[:19])


def test_the_wallet_of_a_caregiver_with_nothing_is_all_zero(db, client):
    cg = make_caregiver(db)
    body = client.get(WALLET, headers=auth_headers(cg.user)).json()
    assert (body["balance"], body["available_balance"], body["pending_balance"], body["held_balance"]) == (0, 0, 0, 0)
    assert body["next_available_at"] is None


def test_the_transaction_list_shows_status_and_available_at(db, client):
    cg, booking, tx = completed_earning(db, client)
    [row] = client.get(f"{WALLET}/transactions", headers=auth_headers(cg.user)).json()
    assert row["type"] == "earning" and row["status"] == "pending_clearance" and row["amount"] == 3_200_000
    assert row["available_at"].startswith(tx.available_at.isoformat()[:19]) and row["booking_id"] == str(booking.id)
    assert row["released_at"] is None and row["reference"] == booking.booking_code
    earnings.release_due(db, now=tx.available_at)
    [row] = client.get(f"{WALLET}/transactions", headers=auth_headers(cg.user)).json()
    assert row["status"] == "successful" and row["released_at"] is not None


def test_a_caregiver_sees_only_their_own_earnings(db, client):
    cg, booking, tx = completed_earning(db, client)
    other = make_caregiver(db)
    assert client.get(f"{WALLET}/transactions", headers=auth_headers(other.user)).json() == []
    assert client.get(WALLET, headers=auth_headers(other.user)).json()["pending_balance"] == 0


def test_the_payslip_counts_released_earnings_only(db, client):
    cg, booking, tx = completed_earning(db, client)
    month = datetime.utcnow().strftime("%Y-%m")
    url = f"{WALLET}/payslip?month={month}"
    assert client.get(url, headers=auth_headers(cg.user)).text.strip().count("\n") == 0  # header only
    earnings.release_due(db, now=tx.available_at)
    assert "32,000.00" in client.get(url, headers=auth_headers(cg.user)).text


# --- withdrawing ---------------------------------------------------------------------------------------------------


def test_withdraw_refuses_money_that_is_still_pending(db, client):
    cg, booking, tx = completed_earning(db, client)
    make_caregiver_wallet(db, cg, balance_kobo=0)
    r = client.post(f"{WALLET}/withdraw", json={"amount": 100_000}, headers=auth_headers(cg.user))
    assert r.status_code == 400 and "Insufficient" in r.json()["detail"]
    assert [t.type for t in txs(db, cg)] == ["earning"]  # no withdrawal row


def test_withdraw_takes_only_from_the_available_part_and_works_after_release(db, client):
    cg, booking, tx = completed_earning(db, client)
    db.add(CaregiverWallet(caregiver_id=cg.id, balance=500_000))
    from app.db.models.caregiver import CaregiverBankAccount
    db.add(CaregiverBankAccount(caregiver_id=cg.id, bank_name="Test Bank", account_number="0000000000", account_name="T"))
    db.commit()
    h = auth_headers(cg.user)
    assert client.post(f"{WALLET}/withdraw", json={"amount": 600_000}, headers=h).status_code == 400  # pending is not counted
    assert client.post(f"{WALLET}/withdraw", json={"amount": 500_000}, headers=h).status_code == 200
    assert balance(db, cg) == 0
    earnings.release_due(db, now=tx.available_at)
    assert client.post(f"{WALLET}/withdraw", json={"amount": 3_200_000}, headers=h).status_code == 200
    assert balance(db, cg) == 0


# --- account erasure -----------------------------------------------------------------------------------------------


def test_a_caregiver_with_earnings_still_clearing_cannot_delete_their_account(db, client):
    cg, booking, tx = completed_earning(db, client)
    r = client.delete("/api/v1/auth/profile", headers=auth_headers(cg.user))
    assert r.status_code == 409 and "still being cleared" in r.json()["detail"]
    db.expire_all()
    assert cg.user.is_active is True
    earnings.release_due(db, now=tx.available_at)  # released but not withdrawn: the balance blocks it instead
    assert "withdraw" in client.delete("/api/v1/auth/profile", headers=auth_headers(cg.user)).json()["detail"].lower()


# --- money-review fixes: flagged late-cancel, wallet race, release-hold permission, audit ------------------------------


def test_a_compensable_late_cancel_of_a_no_show_flagged_booking_is_credited_already_held_and_audited(db, chat):
    booking, cg = accepted_booking(db, chat, hours_ahead=1.5)
    start_moving(db, booking, cg, moved_m=600)
    admin = make_admin(db, ["operations"], password=None)
    b = db.get(Booking, booking.id)
    b.no_show_flagged_at, b.no_show_flagged_by = datetime.utcnow(), admin.id
    db.commit()
    cancellation.cancel_by_booker(db, booking.id)
    [tx] = txs(db, cg)
    assert tx.type == "late_cancel_compensation" and tx.amount > 0
    assert tx.status == "held" and "No-show" in tx.status_reason and tx.status_changed_by == admin.id
    assert earnings.release_due(db, now=datetime.utcnow() + timedelta(days=30)) == 0
    assert balance(db, cg) == 0
    [row] = audit(db, tx, "earning.hold")
    assert row.admin_user_id == admin.id and row.after["status"] == "held"


def test_a_visit_credited_already_held_for_a_no_show_flag_is_audited(db, client):
    cg = make_caregiver(db)
    admin = make_admin(db, ["operations"], password=None)
    cg, booking = in_progress(db, cg=cg, no_show_flagged_at=datetime.utcnow(), no_show_flagged_by=admin.id)
    assert complete(client, booking, cg).status_code == 200
    [tx] = txs(db, cg)
    assert tx.status == "held"
    [row] = audit(db, tx, "earning.hold")
    assert row.admin_user_id == admin.id and row.after["status"] == "held"


def test_a_visit_credited_for_an_unflagged_booking_writes_no_hold_audit(db, client):
    cg, booking, tx = completed_earning(db, client)
    assert audit(db, tx, "earning.hold") == []


def test_a_first_wallet_read_that_loses_the_insert_race_still_succeeds(db):
    # Another writer (the release job's _locked_wallet) creates the wallet between this request's SELECT and its INSERT.
    cg = make_caregiver(db)
    fired = []

    def rival(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO caregiver_wallets" in statement and not fired:
            fired.append(1)
            with suite_engine.begin() as other:
                other.execute(text("INSERT INTO caregiver_wallets (id, caregiver_id, balance) "
                                   "VALUES (gen_random_uuid(), :c, 0)"), {"c": cg.id})

    event.listen(suite_engine, "before_cursor_execute", rival)
    try:
        with TestClient(fastapi_app, raise_server_exceptions=False) as c:
            r = c.get(WALLET, headers=auth_headers(cg.user))
    finally:
        event.remove(suite_engine, "before_cursor_execute", rival)
    assert fired and r.status_code == 200, r.text
    assert db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).count() == 1


def _flagged_held_earning(db, client):
    cg, booking, tx = completed_earning(db, client)
    ops = make_admin(db, ["operations"], password=None)
    b = db.get(Booking, booking.id)
    b.status, b.actual_start_time = BookingStatus.ACCEPTED, None
    b.scheduled_date = datetime.utcnow() - timedelta(hours=1)
    db.commit()
    AdminBookingService(db).flag_no_show(booking.id, ops, "never showed up", None)
    return cg, booking, txs(db, cg)[0]


def test_releasing_a_no_show_hold_needs_ledger_adjust_not_just_the_adjust_request(db, client):
    cg, booking, tx = _flagged_held_earning(db, client)
    assert tx.status == "held"
    finance = auth_headers(make_admin(db, ["finance"], password=None))
    r = client.post(f"{ADMIN_EARNINGS}/{tx.id}/release-hold", json={"reason": "looks fine"}, headers=finance)
    assert r.status_code == 403, r.text
    assert "ledger.adjust" in r.json()["detail"] and "no-show" in r.json()["detail"].lower()
    assert txs(db, cg)[0].status == "held"
    boss = auth_headers(make_admin(db, ["super_admin"], password=None))
    r = client.post(f"{ADMIN_EARNINGS}/{tx.id}/release-hold", json={"reason": "reviewed, genuine visit"}, headers=boss)
    assert r.status_code == 200 and r.json()["status"] == "pending_clearance"


def test_finance_can_still_release_an_ordinary_hold_on_an_unflagged_booking(db, client):
    cg, booking, tx = completed_earning(db, client)
    finance = auth_headers(make_admin(db, ["finance"], password=None))
    assert client.post(f"{ADMIN_EARNINGS}/{tx.id}/hold", json={"reason": "x"}, headers=finance).status_code == 200
    assert client.post(f"{ADMIN_EARNINGS}/{tx.id}/release-hold", json={"reason": "y"}, headers=finance).status_code == 200
