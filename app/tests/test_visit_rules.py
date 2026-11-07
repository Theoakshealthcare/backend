"""Device-pass-1 owner rules (2026-11-07): short visits are held for Finance, a declined offer is final, and wallet-credit
refunds are off in v1.

* Rule 1: completing a visit after under SHORT_VISIT_MIN_PERCENT (50%) of the booked time on site still completes, but the
  earning is credited already held (a system hold: no admin, no audit row). Exactly 50% is not short. ``start`` is untouched.
* Rule 2: accepting an offer the caregiver already declined is 403 "You declined this offer"; the notification list shows the
  offer's ``response`` so the app can hide Accept/Decline.
* Rule 3: with REFUND_WALLET_CREDIT_ENABLED off (the default), choosing the Oakly wallet for a refund is 409
  ``wallet_refunds_disabled``; with it on, behaviour is as before.
"""
from datetime import datetime, timedelta

import pytest

from app.core.config import Settings, settings
from app.db.models.audit import AdminAuditLog
from app.db.models.booking import Booking, BookingNotification, BookingStatus
from app.db.models.caregiver import CaregiverTransaction, CaregiverWallet
from app.db.models.dropout import CaregiverDropout
from app.db.models.patient import PatientTransaction
from app.db.models.refund import Refund
from app.services import earnings, refunds
from app.tests.factories import auth_headers, make_admin, make_booking, make_caregiver, make_patient
from app.tests.test_access_booking import notify
from app.tests.test_admin_refunds_api import URL as REFUNDS_URL, booking_with, finance, make_failed, refund_row, super_admin
from app.tests.test_caregiver_earnings import ADMIN_EARNINGS, complete, txs
from app.tests.test_visit_lifecycle import act

ACTION = "/api/v1/booking/caregiver/action"
NOTIFICATIONS = "/api/v1/booking/caregiver/notifications"
BOOKED_HOURS = 4


def visit_with_time_on_site(db, on_site: timedelta, *, hours=BOOKED_HOURS):
    """An in-progress visit whose caregiver started ``on_site`` ago; completing it now ends it ``on_site`` after the start."""
    cg = make_caregiver(db)
    booking = make_booking(db, make_patient(db), cg, status=BookingStatus.IN_PROGRESS, duration_hours=hours,
                           actual_start_time=datetime.utcnow() - on_site)
    return cg, booking


def timed(db, on_site, hours=BOOKED_HOURS):
    """A booking with exact actual start/end times (no clock involved), for the boundary tests."""
    cg = make_caregiver(db)
    start = datetime(2026, 11, 7, 10, 0, 0)
    booking = make_booking(db, make_patient(db), cg, status=BookingStatus.COMPLETED, duration_hours=hours,
                           actual_start_time=start, actual_end_time=None if on_site is None else start + on_site)
    return cg, booking


# === rule 1: the short-visit hold ===========================================================================================


@pytest.mark.parametrize("on_site, short", [
    (timedelta(hours=2), False),                                   # exactly 50% of 4h: NOT held
    (timedelta(hours=2) - timedelta(seconds=1), True),             # one second under: held
    (timedelta(hours=2) - timedelta(microseconds=1), True),        # the smallest step under: held
    (timedelta(hours=2) + timedelta(seconds=1), False),
    (timedelta(hours=4), False),                                   # a full visit
    (timedelta(hours=6), False),                                   # an overrun is never short
    (timedelta(0), True),                                          # start and complete in the same instant
])
def test_is_short_visit_boundaries(db, on_site, short):
    _cg, booking = timed(db, on_site)
    assert earnings.is_short_visit(booking) is short


def test_the_threshold_is_the_documented_constant():
    assert earnings.SHORT_VISIT_MIN_PERCENT == 50


def test_a_completion_with_no_recorded_start_is_not_short(db):
    """Decision: with no actual_start_time the time on site cannot be proven short, so the visit is credited normally
    (a caregiver's own complete always follows a start that stamped it; this is only legacy/odd data)."""
    _cg, booking = timed(db, timedelta(0))
    booking.actual_start_time = None
    assert earnings.is_short_visit(booking) is False
    _cg2, no_end = timed(db, None)
    assert earnings.is_short_visit(no_end) is False


def test_a_one_hour_booking_uses_its_own_duration(db):
    assert earnings.is_short_visit(timed(db, timedelta(minutes=29), hours=1)[1]) is True
    assert earnings.is_short_visit(timed(db, timedelta(minutes=30), hours=1)[1]) is False


def test_credit_visit_holds_a_short_visit_only_when_asked_to(db):
    _cg, booking = timed(db, timedelta(minutes=5))
    tx = earnings.credit_visit(db, booking, check_short_visit=True)
    assert tx.status == "held" and tx.status_reason == earnings.SHORT_VISIT_HOLD_REASON
    _cg2, other = timed(db, timedelta(minutes=5))
    assert earnings.credit_visit(db, other).status == "pending_clearance"  # the default call is unchanged


def test_a_short_visit_still_completes_and_is_credited_held_with_a_system_reason(db, client):
    cg, booking = visit_with_time_on_site(db, timedelta(minutes=10))
    r = complete(client, booking, cg)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(Booking, booking.id).status == BookingStatus.COMPLETED
    [tx] = txs(db, cg, type="earning")
    assert tx.status == "held" and tx.amount > 0
    assert tx.status_reason == earnings.SHORT_VISIT_HOLD_REASON and "50%" in tx.status_reason
    assert tx.status_changed_by is None and tx.status_changed_at is not None  # a system hold: no admin
    audit = db.query(AdminAuditLog).filter_by(entity_type="caregiver_transaction", entity_id=str(tx.id)).count()
    assert audit == 0  # admin_audit_log.admin_user_id is NOT NULL: nobody to attribute it to


def test_a_normal_visit_is_unchanged(db, client):
    cg, booking = visit_with_time_on_site(db, timedelta(hours=3))
    assert complete(client, booking, cg).status_code == 200
    [tx] = txs(db, cg, type="earning")
    assert tx.status == "pending_clearance" and tx.status_reason is None and tx.status_changed_by is None
    assert tx.amount == earnings.share(booking.service_fee_kobo, 80)


def test_a_held_short_visit_cannot_clear_or_be_paid_out_until_finance_releases_it(db, client):
    cg, booking = visit_with_time_on_site(db, timedelta(minutes=10))
    assert complete(client, booking, cg).status_code == 200
    [tx] = txs(db, cg, type="earning")
    later = datetime.utcnow() + timedelta(days=30)
    assert earnings.release_due(db, now=later) == 0  # the release job skips held rows, however old
    db.expire_all()
    assert db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).count() == 0 or db.query(CaregiverWallet).filter_by(
        caregiver_id=cg.id).one().balance == 0  # nothing reached the withdrawable balance, so nothing can be paid out
    assert db.get(CaregiverTransaction, tx.id).status == "held"

    fin = auth_headers(make_admin(db, ["finance"], password=None))
    listed = client.get(ADMIN_EARNINGS, params={"status": "held", "booking_id": str(booking.id)}, headers=fin).json()
    [row] = listed["items"]  # where Finance already sees held earnings, with the reason
    assert row["id"] == str(tx.id) and row["status_reason"] == earnings.SHORT_VISIT_HOLD_REASON
    assert row["status_changed_by"] is None

    r = client.post(f"{ADMIN_EARNINGS}/{tx.id}/release-hold", json={"reason": "checked with the family: visit was fine"},
                    headers=fin)  # Finance can release it: the booking is not a no-show flag
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_clearance" and r.json()["status_changed_by"] is not None
    assert earnings.release_due(db, now=later) == 1  # now it clears like any earning
    db.expire_all()
    assert db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).one().balance == tx.amount


def test_finance_can_void_a_short_visit_hold_only_through_the_existing_super_admin_void(db, client):
    cg, booking = visit_with_time_on_site(db, timedelta(minutes=10))
    assert complete(client, booking, cg).status_code == 200
    [tx] = txs(db, cg, type="earning")
    fin = auth_headers(make_admin(db, ["finance"], password=None))
    assert client.post(f"{ADMIN_EARNINGS}/{tx.id}/void", json={"reason": "x"}, headers=fin).status_code == 403
    boss = auth_headers(make_admin(db, ["super_admin"], password=None))
    assert client.post(f"{ADMIN_EARNINGS}/{tx.id}/void", json={"reason": "never on site"}, headers=boss).json()["status"] == "voided"


def test_completing_twice_credits_once(db, client):
    cg, booking = visit_with_time_on_site(db, timedelta(minutes=10))
    assert complete(client, booking, cg).status_code == 200
    assert complete(client, booking, cg).status_code == 409  # already completed
    assert earnings.credit_visit(db, db.get(Booking, booking.id), check_short_visit=True).id == txs(db, cg, type="earning")[0].id
    assert len(txs(db, cg, type="earning")) == 1


def test_a_no_show_flagged_short_visit_keeps_the_no_show_hold(db):
    """When both apply, the no-show hold wins: it is attributed to the admin and needs ledger.adjust to release."""
    ops = make_admin(db, ["operations"], password=None)
    _cg, booking = timed(db, timedelta(minutes=5))
    booking.no_show_flagged_at, booking.no_show_flagged_by = datetime.utcnow(), ops.id
    db.commit()
    tx = earnings.credit_visit(db, booking, check_short_visit=True)
    assert tx.status == "held" and tx.status_reason == earnings.NO_SHOW_HOLD_REASON and tx.status_changed_by == ops.id


def test_a_super_admin_status_correction_is_not_a_short_visit(db, client):
    """correct_status stamps start and end together (zero time on site): it is a deliberate admin write, never held."""
    cg = make_caregiver(db)
    booking = make_booking(db, make_patient(db), cg, status=BookingStatus.ACCEPTED, duration_hours=BOOKED_HOURS)
    boss = auth_headers(make_admin(db, ["super_admin"], password=None))
    r = client.post(f"/api/v1/admin/bookings/{booking.id}/correct-status",
                    json={"status": "completed", "reason": "caregiver forgot to clock in and out"}, headers=boss)
    assert r.status_code == 200, r.text
    [tx] = txs(db, cg, type="earning")
    assert tx.status == "pending_clearance"


def test_start_is_unchanged_an_assigned_caregiver_can_start_any_time(db, client):
    """Owner decision: follow the backend. Only on_my_way is gated by the compensation window; start is not."""
    from app.tests.test_visit_lifecycle import CODE

    cg = make_caregiver(db)
    far = datetime.utcnow() + timedelta(days=3)
    booking = make_booking(db, make_patient(db), cg, status=BookingStatus.ACCEPTED, visit_code=CODE,
                           scheduled_date=far, scheduled_end_date=far + timedelta(hours=4))
    assert act(client, booking, cg, "on_my_way").status_code == 409
    assert act(client, booking, cg, "start", visit_code=CODE).status_code == 200


# === rule 2: a declined offer is final ======================================================================================


def offered(db, response=None):
    cg = make_caregiver(db)
    booking = make_booking(db, make_patient(db))
    n = notify(db, booking, cg)
    if response:
        n.response, n.response_at = response, datetime.utcnow()
        db.commit()
    return cg, booking, n


def test_accepting_after_declining_is_refused_with_a_stable_message_and_changes_nothing(db, client):
    cg, booking, n = offered(db)
    assert act(client, booking, cg, "decline").status_code == 200
    r = act(client, booking, cg, "accept")
    assert r.status_code == 403 and r.json()["detail"] == "You declined this offer"
    db.expire_all()
    b = db.get(Booking, booking.id)
    assert b.status == BookingStatus.PENDING and b.caregiver_id is None and b.visit_code_enc is None
    assert db.get(BookingNotification, n.id).response == "declined"
    assert db.query(CaregiverDropout).filter_by(booking_id=booking.id).count() == 0
    assert db.query(CaregiverTransaction).filter_by(caregiver_id=cg.id).count() == 0


def test_declining_twice_stays_declined(db, client):
    cg, booking, n = offered(db)
    assert act(client, booking, cg, "decline").status_code == 200
    assert act(client, booking, cg, "decline").status_code == 200
    db.expire_all()
    assert db.get(BookingNotification, n.id).response == "declined"


def test_accept_without_a_decline_still_works(db, client):
    cg, booking, n = offered(db)
    assert act(client, booking, cg, "accept").status_code == 200
    db.expire_all()
    assert db.get(Booking, booking.id).caregiver_id == cg.id and db.get(BookingNotification, n.id).response == "accept"


def test_a_decline_by_one_caregiver_does_not_stop_another_from_accepting(db, client):
    cg, booking, _n = offered(db)
    other = make_caregiver(db)
    notify(db, booking, other)
    assert act(client, booking, cg, "decline").status_code == 200
    assert act(client, booking, other, "accept").status_code == 200


def test_an_auto_declined_offer_still_conflicts_rather_than_being_called_a_decline(db, client):
    """Someone else took the booking: the loser's offer is auto_declined and accept stays the 409 the app already handles."""
    first, booking, _ = offered(db)
    loser = make_caregiver(db)
    notify(db, booking, loser)
    assert act(client, booking, first, "accept").status_code == 200
    db.expire_all()
    assert db.query(BookingNotification).filter_by(caregiver_id=loser.id).one().response == "auto_declined"
    r = act(client, booking, loser, "accept")
    assert r.status_code == 409 and r.json()["detail"] == "Booking is no longer available"


def test_a_fresh_offer_resets_a_decline(db, client):
    """An admin re-offer (or a re-dispatch wave) resets the response to null: that is a new offer, so accepting works."""
    cg, booking, n = offered(db, response="declined")
    assert act(client, booking, cg, "accept").status_code == 403
    n.response, n.response_at = None, None  # what dispatch.send_wave(renotify) / admin _offer_to do
    db.commit()
    assert act(client, booking, cg, "accept").status_code == 200


def test_the_notification_list_exposes_the_offers_response(db, client):
    cg, booking, n = offered(db)
    h = auth_headers(cg.user)
    [item] = client.get(NOTIFICATIONS, headers=h).json()
    assert "response" in item and item["response"] is None
    assert act(client, booking, cg, "decline").status_code == 200
    [item] = client.get(NOTIFICATIONS, headers=h).json()
    assert item["response"] == "declined" and item["id"] == str(n.id)


# === rule 3: wallet-credit refunds are off by default =======================================================================


def test_the_flag_defaults_to_off():
    assert Settings.model_fields["REFUND_WALLET_CREDIT_ENABLED"].default is False


def pending(db, booking, amount=1_000_000):
    return refund_row(db, booking, suggested=amount, origin="system")


def wallet_credits(db, booking):
    db.expire_all()
    return db.query(PatientTransaction).filter_by(patient_id=booking.patient_id, type="refund").count()


def test_with_the_flag_off_approving_to_the_wallet_is_409_wallet_refunds_disabled(db, client, monkeypatch):
    monkeypatch.setattr(settings, "REFUND_WALLET_CREDIT_ENABLED", False)
    b = booking_with(db, refund_required=True, channel="app")
    r = pending(db, b)
    boss = auth_headers(super_admin(db))
    res = client.post(f"{REFUNDS_URL}/{r.id}/approve", json={"destination": "wallet"}, headers=boss)
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == "wallet_refunds_disabled" and "original payment method" in res.json()["detail"]["message"]
    db.expire_all()
    assert db.get(Refund, r.id).status == "pending_approval" and wallet_credits(db, b) == 0
    # everything else is unchanged: the same refund approves to the original payment method
    ok = client.post(f"{REFUNDS_URL}/{r.id}/approve", json={"destination": "original_method"}, headers=boss)
    assert ok.status_code == 200 and ok.json()["destination"] == "original_method"


def test_finance_asking_for_the_wallet_is_still_the_403_not_the_flag(db, client, monkeypatch):
    monkeypatch.setattr(settings, "REFUND_WALLET_CREDIT_ENABLED", False)
    b = booking_with(db, channel="app")
    r = pending(db, b)
    res = client.post(f"{REFUNDS_URL}/{r.id}/approve", json={"destination": "wallet"}, headers=auth_headers(finance(db)))
    assert res.status_code == 403 and isinstance(res.json()["detail"], str)


def test_the_service_raises_the_coded_error_before_asking_paystack(db, monkeypatch):
    monkeypatch.setattr(settings, "REFUND_WALLET_CREDIT_ENABLED", False)
    b = booking_with(db, channel="app")
    r = pending(db, b)

    class NoPaystack:
        def __getattr__(self, name):
            raise AssertionError("Paystack must not be asked when the wallet is switched off")

    with pytest.raises(refunds.WalletRefundsDisabled) as exc:
        refunds.approve_refund(db, r.id, admin=super_admin(db), amount_kobo=None, destination="wallet", paystack=NoPaystack())
    assert exc.value.code == "wallet_refunds_disabled" and isinstance(exc.value, refunds.RefundConflict)


def test_with_the_flag_off_a_failed_refund_cannot_be_switched_to_the_wallet(db, client, fake_paystack, monkeypatch):
    booking, refund, _pid = make_failed(db, fake_paystack)
    boss = super_admin(db)
    monkeypatch.setattr(settings, "REFUND_WALLET_CREDIT_ENABLED", False)
    detail = client.get(f"{REFUNDS_URL}/{refund.id}", headers=auth_headers(boss)).json()
    assert "switch_to_wallet" not in detail["available_actions"] and "retry" in detail["available_actions"]
    res = client.post(f"{REFUNDS_URL}/{refund.id}/switch-to-wallet", headers=auth_headers(boss))
    assert res.status_code == 409 and res.json()["detail"]["code"] == "wallet_refunds_disabled"
    db.expire_all()
    assert db.get(Refund, refund.id).status == "failed" and wallet_credits(db, booking) == 0
    # retry and close are unaffected
    assert client.post(f"{REFUNDS_URL}/{refund.id}/retry", headers=auth_headers(finance(db))).status_code == 200


def test_with_the_flag_on_the_wallet_path_is_exactly_as_before(db, client, fake_paystack, monkeypatch):
    monkeypatch.setattr(settings, "REFUND_WALLET_CREDIT_ENABLED", True)
    b = booking_with(db, refund_required=True, channel="app")
    r = pending(db, b, 2_500_000)
    boss = auth_headers(super_admin(db))
    res = client.post(f"{REFUNDS_URL}/{r.id}/approve", json={"destination": "wallet"}, headers=boss)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "succeeded" and res.json()["destination"] == "wallet"
    assert wallet_credits(db, b) == 1
    booking, failed, _pid = make_failed(db, fake_paystack)
    detail = client.get(f"{REFUNDS_URL}/{failed.id}", headers=boss).json()
    assert "switch_to_wallet" in detail["available_actions"]
    sw = client.post(f"{REFUNDS_URL}/{failed.id}/switch-to-wallet", headers=boss)
    assert sw.status_code == 200 and sw.json()["destination"] == "wallet"
