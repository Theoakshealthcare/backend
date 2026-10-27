"""Refunds v2 task 8: the admin refunds API the console uses (thin routes over app.services.refunds), and the retired legacy routes.

The invariants: each route needs exactly the permission of spec section 8; naira in and out with integer kobo underneath; the
queue's default shows every unfinished refund plus finished ones flagged for review; the detail never carries a full account
number; ``available_actions`` never offers what the service would refuse; service errors keep the ``{"detail": message}`` shape;
the legacy refund routes answer 410 and the legacy refund webhook no longer moves a wallet."""
import hashlib
import hmac
import json
import uuid
from datetime import datetime

import pytest

from app.core.config import settings
from app.db.models.audit import AdminAuditLog
from app.db.models.booking import BookingStatus
from app.db.models.patient import PatientTransaction, PatientWallet
from app.db.models.refund import Refund, RefundAttempt
from app.tests.factories import auth_headers, make_admin, make_booking, make_patient

API = "/api/v1"
URL = f"{API}/admin/refunds"
BIG = 10_000_000  # N100,000; the Finance limit is N50,000 by default

ROLES = ["super_admin", "operations", "verification", "finance", "support", "clinical_reviewer", "read_only_analyst"]


def admin_of(db, *roles):
    return make_admin(db, roles, password=None)


def finance(db):
    return admin_of(db, "finance")


def super_admin(db):
    return admin_of(db, "super_admin")


def booking_with(db, total=BIG, **kw):
    kw.setdefault("status", BookingStatus.CANCELLED)
    return make_booking(db, make_patient(db), total_amount_kobo=total, **kw)


def refund_row(db, booking, *, status="pending_approval", suggested=5_000_000, amount=None, origin="system", by=None,
               approved_by=None, destination=None, reason="other", **fields):
    r = Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code=reason, origin=origin,
               requested_by_admin_id=by.id if by else None, approved_by_admin_id=approved_by.id if approved_by else None,
               suggested_amount_kobo=suggested, amount_kobo=amount, destination=destination, status=status, **fields)
    db.add(r)
    db.commit()
    return r


def get(client, admin, refund):
    return client.get(f"{URL}/{refund.id}", headers=auth_headers(admin))


def actions(client, admin, refund):
    r = get(client, admin, refund)
    assert r.status_code == 200, r.text
    return r.json()["available_actions"]


# --- permissions (spec section 8) -------------------------------------------------------------------------------------
READ_ANY = {"refunds.request", "refunds.approve", "payments.read"}
FAKE = "7b1f0c2e-5d3a-4c8e-9a41-0e2f6b7d8c90"  # a fixed id, so the parametrized test ids are stable
ROUTES = [
    ("GET", URL, None, READ_ANY),
    ("GET", f"{URL}/{FAKE}", None, READ_ANY),
    ("POST", URL, {"booking_id": FAKE, "amount_naira": 10, "reason_code": "complaint"}, {"refunds.request"}),
    ("POST", f"{URL}/{FAKE}/approve", {}, {"refunds.approve"}),
    ("POST", f"{URL}/{FAKE}/reject", {"reason": "no"}, {"refunds.approve"}),
    ("POST", f"{URL}/{FAKE}/retry", None, {"refunds.approve"}),
    ("POST", f"{URL}/{FAKE}/bank-details", {"bank_code": "058", "account_number": "0123456789"}, {"refunds.approve"}),
    ("POST", f"{URL}/{FAKE}/close", {"reason": "no"}, {"refunds.approve"}),
    ("POST", f"{URL}/{FAKE}/switch-to-wallet", None, {"refunds.approve_above_limit"}),
    ("POST", f"{URL}/{FAKE}/resolve", {"outcome": "failed", "note": "checked"}, {"refunds.approve_above_limit"}),
]


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: f"{r[0]} {r[1].replace(API, '').replace(FAKE, '{id}')}")
def test_permissions_per_route(db, client, fake_paystack, route):
    from app.core.admin_permissions import ROLE_PERMISSIONS

    method, url, body, needs = route
    assert client.request(method, url, json=body).status_code == 401  # no token
    for role in ROLES:
        r = client.request(method, url, json=body, headers=auth_headers(admin_of(db, role)))
        if needs & ROLE_PERMISSIONS[role]:
            assert r.status_code not in (401, 403), (role, r.status_code, r.text)  # 404/200: past the permission check
        else:
            assert r.status_code == 403, (role, r.status_code, r.text)


def test_reads_return_200_for_every_reading_role(db, client):
    refund = refund_row(db, booking_with(db))
    for role in ("super_admin", "operations", "finance", "support"):
        admin = admin_of(db, role)
        assert client.get(URL, headers=auth_headers(admin)).status_code == 200, role
        assert get(client, admin, refund).status_code == 200, role


# --- the queue ---------------------------------------------------------------------------------------------------------
def test_list_defaults_to_open_and_filters(db, client):
    admin = finance(db)
    h = auth_headers(admin)
    pending = refund_row(db, booking_with(db), reason="complaint")
    failed = refund_row(db, booking_with(db), status="failed", amount=1_000_000, reason="caregiver_cancelled")
    done = refund_row(db, booking_with(db), status="succeeded", amount=1_000_000)
    closed = refund_row(db, booking_with(db), status="closed", amount=1_000_000)
    rejected = refund_row(db, booking_with(db), status="rejected")
    flagged = refund_row(db, booking_with(db), status="succeeded", amount=1_000_000, review_flagged_at=datetime.utcnow(),
                         review_reason="Paystack said failed after we recorded processed")

    body = client.get(URL, headers=h).json()
    ids = {i["id"] for i in body["items"]}
    assert ids == {str(pending.id), str(failed.id), str(flagged.id)}  # open, plus the finished one flagged for review
    assert body["total"] == 3 and body["limit"] == 50 and body["offset"] == 0
    row = next(i for i in body["items"] if i["id"] == str(flagged.id))
    assert row["review_reason"] == "Paystack said failed after we recorded processed" and row["review_flagged_at"]

    by_status = lambda s: {i["id"] for i in client.get(URL, params={"status": s}, headers=h).json()["items"]}  # noqa: E731
    assert by_status("succeeded") == {str(done.id), str(flagged.id)}
    assert by_status("closed") == {str(closed.id)}
    assert by_status("rejected") == {str(rejected.id)}
    assert len(by_status("all")) == 6
    assert by_status("open") == ids

    only = client.get(URL, params={"reason_code": "complaint"}, headers=h).json()
    assert [i["id"] for i in only["items"]] == [str(pending.id)]
    one = client.get(URL, params={"booking_id": str(failed.booking_id), "status": "all"}, headers=h).json()
    assert [i["id"] for i in one["items"]] == [str(failed.id)]

    page = client.get(URL, params={"limit": 2, "offset": 2}, headers=h).json()
    assert page["total"] == 3 and len(page["items"]) == 1 and page["limit"] == 2 and page["offset"] == 2
    assert client.get(URL, params={"status": "bogus"}, headers=h).status_code == 422
    assert client.get(URL, params={"limit": 0}, headers=h).status_code == 422


def test_list_above_limit_filter_counts_the_booking_cumulatively(db, client):
    h = auth_headers(finance(db))
    big_booking = booking_with(db, total=20_000_000)
    refund_row(db, big_booking, status="succeeded", amount=4_000_000)  # earlier refund of the same booking
    over = refund_row(db, big_booking, suggested=2_000_000)  # 4M + 2M is above the N50,000 limit
    under = refund_row(db, booking_with(db), suggested=1_000_000)
    above = {i["id"] for i in client.get(URL, params={"above_limit": True}, headers=h).json()["items"]}
    within = {i["id"] for i in client.get(URL, params={"above_limit": False}, headers=h).json()["items"]}
    assert above == {str(over.id)} and within == {str(under.id)}


# --- the detail --------------------------------------------------------------------------------------------------------
def test_detail_shows_unknown_paystack_fee_as_null(db, client):
    admin = finance(db)
    unknown = refund_row(db, booking_with(db))  # paystack_fee_kobo is NULL: settled before the fee was recorded
    body = get(client, admin, unknown).json()
    assert "paystack_fee_naira" in body["booking"] and body["booking"]["paystack_fee_naira"] is None
    known = refund_row(db, booking_with(db, paystack_fee_kobo=150_050))
    assert get(client, admin, known).json()["booking"]["paystack_fee_naira"] == 1500.5


def test_detail_booking_context_and_numbers(db, client):
    admin = finance(db)
    b = booking_with(db, total=BIG, status=BookingStatus.COMPLETED, actual_start_time=datetime.utcnow(), channel="app")
    refund_row(db, b, status="succeeded", amount=2_500_000)
    pending = refund_row(db, b, suggested=1_000_050)
    body = get(client, admin, pending).json()
    ctx = body["booking"]
    assert (ctx["status"], ctx["channel"], ctx["completed"], ctx["caregiver_arrived"], ctx["caregiver_assigned"]) == (
        "completed", "app", True, True, False)
    assert (ctx["total_naira"], ctx["refunded_naira"], ctx["refundable_naira"]) == (100000.0, 25000.0, 75000.0)
    assert body["suggested_amount_naira"] == 10000.5 and body["amount_naira"] is None
    assert body["limit_naira"] == 50000.0 and body["needs_super_admin"] is False
    assert body["suggestion_why"]
    assert body["take_back_preview_naira"] == 0.0  # a completed visit with no earning row: nothing to take back
    over = refund_row(db, booking_with(db), suggested=5_000_100)
    assert get(client, admin, over).json()["needs_super_admin"] is True


def test_detail_attempts_never_carry_a_full_account_number(db, client):
    admin = finance(db)
    b = booking_with(db)
    r = refund_row(db, b, status="succeeded", amount=1_000_000, destination="original_method", processed_at=datetime.utcnow(),
                   take_back_from_caregiver=True, take_back_kobo=300_000, take_back_debited_kobo=200_000,
                   take_back_owed_kobo=100_000)
    db.add(RefundAttempt(refund_id=r.id, attempt=1, status="needs_attention", paystack_refund_id="PR_1"))
    db.add(RefundAttempt(refund_id=r.id, attempt=2, status="processed", paystack_refund_id="PR_2", bank_code="058",
                         account_last4="6789", account_name="Ada Obi", bank_details_submitted_at=datetime.utcnow()))
    db.commit()
    resp = get(client, admin, r)
    body = resp.json()
    assert [a["attempt"] for a in body["attempts"]] == [1, 2]
    second = body["attempts"][1]
    assert (second["bank_code"], second["account_last4"], second["account_name"]) == ("058", "6789", "Ada Obi")
    assert "account_number" not in resp.text and "0123456789" not in resp.text
    assert body["take_back_naira"] == 3000.0
    assert (body["take_back_debited_naira"], body["take_back_owed_naira"]) == (2000.0, 1000.0)
    assert body["take_back_preview_naira"] is None  # finished: nothing left to preview


def test_unknown_refund_is_404_with_detail(db, client):
    r = client.get(f"{URL}/{uuid.uuid4()}", headers=auth_headers(finance(db)))
    assert r.status_code == 404 and r.json() == {"detail": "Refund not found"}


# --- available_actions mirrors the service rules ------------------------------------------------------------------------
def test_available_actions_hide_approve_for_requester(db, client):
    raiser, other, boss = finance(db), finance(db), super_admin(db)
    staff = refund_row(db, booking_with(db), origin="staff", by=raiser, reason="complaint")
    assert actions(client, raiser, staff) == ["reject"]  # may withdraw their own request, not approve it
    assert actions(client, other, staff) == ["approve", "reject"]
    assert actions(client, boss, staff) == ["approve", "reject"]
    system = refund_row(db, booking_with(db))
    assert actions(client, raiser, system) == ["approve", "reject"]  # RF-D11 is for staff-raised refunds only


def test_available_actions_follow_state_and_permission(db, client):
    fin, boss = finance(db), super_admin(db)
    ops = admin_of(db, "operations")
    app = lambda **kw: booking_with(db, channel="app", **kw)  # noqa: E731
    failed = refund_row(db, app(), status="failed", amount=1_000_000)
    assert actions(client, fin, failed) == ["retry", "close"]
    assert actions(client, boss, failed) == ["retry", "switch_to_wallet", "close"]
    assert actions(client, ops, failed) == []  # Ops can raise a refund, not act on a failed one
    wa_failed = refund_row(db, booking_with(db, channel="whatsapp"), status="failed", amount=1_000_000)
    assert "switch_to_wallet" not in actions(client, boss, wa_failed)  # no app wallet to credit
    review = refund_row(db, app(), status="needs_review", amount=1_000_000)
    assert actions(client, fin, review) == [] and actions(client, boss, review) == ["resolve"]
    flagged = refund_row(db, app(), status="succeeded", amount=1_000_000, review_flagged_at=datetime.utcnow())
    assert actions(client, boss, flagged) == ["resolve"] and actions(client, fin, flagged) == []
    for status_ in ("approved", "processing", "succeeded", "rejected", "closed"):
        quiet = refund_row(db, app(), status=status_, amount=1_000_000)
        assert actions(client, boss, quiet) == [], status_


def test_available_actions_bank_details_rules(db, client):
    approver, other, boss = finance(db), finance(db), super_admin(db)
    attn = refund_row(db, booking_with(db), status="needs_attention", amount=1_000_000, approved_by=approver)
    assert actions(client, other, attn) == ["bank_details"]
    assert actions(client, approver, attn) == []  # the approver cannot also choose where the money goes
    raised = refund_row(db, booking_with(db), status="needs_attention", amount=1_000_000, origin="staff", by=other,
                        approved_by=approver)
    assert actions(client, other, raised) == []  # nor the raiser
    big = booking_with(db, total=20_000_000)
    refund_row(db, big, status="succeeded", amount=4_000_000)
    over = refund_row(db, big, status="needs_attention", amount=2_000_000, approved_by=approver)  # 6M: above the 5M limit
    assert actions(client, other, over) == []  # above the Finance limit: Super Admin only
    assert actions(client, boss, over) == ["bank_details"]


def test_approve_is_offered_only_within_the_callers_limit(db, client):
    fin, boss = finance(db), super_admin(db)
    over = refund_row(db, booking_with(db), suggested=6_000_000)  # N60,000 against the N50,000 Finance limit
    body = get(client, fin, over).json()
    assert "approve" in body["available_actions"]  # a lower amount is still within reach
    assert body["max_approvable_naira"] == 50000.0 and body["needs_super_admin"] is True
    r = client.post(f"{URL}/{over.id}/approve", json={"amount_naira": 50000}, headers=auth_headers(fin))
    assert r.status_code == 200 and r.json()["amount_naira"] == 50000.0
    again = refund_row(db, booking_with(db), suggested=6_000_000)
    assert client.post(f"{URL}/{again.id}/approve", json={}, headers=auth_headers(fin)).status_code == 403
    listed = next(i for i in client.get(URL, headers=auth_headers(fin)).json()["items"] if i["id"] == str(again.id))
    assert listed["max_approvable_naira"] == 50000.0 and "approve" in listed["available_actions"]
    top = get(client, boss, again).json()
    assert top["max_approvable_naira"] == 100000.0 and top["needs_super_admin"] is False  # refundable, whatever the limit


def test_approve_hidden_when_the_limit_is_already_used(db, client):
    fin, boss = finance(db), super_admin(db)
    big = booking_with(db, total=20_000_000)
    refund_row(db, big, status="succeeded", amount=5_000_000)  # N50,000 already committed: nothing left for Finance
    pending = refund_row(db, big, suggested=1_000_000)
    body = get(client, fin, pending).json()
    assert body["max_approvable_naira"] == 0.0 and "approve" not in body["available_actions"]
    assert "reject" in body["available_actions"]
    top = get(client, boss, pending).json()
    assert top["max_approvable_naira"] == 150000.0 and "approve" in top["available_actions"]


def test_bank_holder_details_hidden_from_roles_without_payments_access(db, client):
    b = booking_with(db)
    r = refund_row(db, b, status="succeeded", amount=1_000_000, destination="original_method")
    db.add(RefundAttempt(refund_id=r.id, attempt=1, status="processed", paystack_refund_id="PR_9", bank_code="058",
                         account_last4="6789", account_name="Ada Obi"))
    db.commit()
    seen = get(client, admin_of(db, "support"), r).json()["attempts"][0]
    assert (seen["account_name"], seen["account_last4"], seen["bank_code"]) == (None, None, None)
    seen = get(client, finance(db), r).json()["attempts"][0]
    assert (seen["account_name"], seen["account_last4"], seen["bank_code"]) == ("Ada Obi", "6789", "058")


# --- naira at the edge ---------------------------------------------------------------------------------------------------
def test_naira_in_and_out(db, client):
    raiser, approver = finance(db), finance(db)
    b = booking_with(db, total=BIG)
    r = client.post(URL, json={"booking_id": str(b.id), "amount_naira": 35000.5, "reason_code": "complaint", "note": "late"},
                    headers=auth_headers(raiser))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["suggested_amount_naira"] == 35000.5 and body["status"] == "pending_approval" and body["origin"] == "staff"
    assert body["requested_by"] == str(raiser.id) and body["available_actions"] == ["reject"]
    row = db.query(Refund).filter_by(id=body["id"]).one()
    assert row.suggested_amount_kobo == 3_500_050

    ok = client.post(f"{URL}/{body['id']}/approve", json={"amount_naira": 12345.67}, headers=auth_headers(approver))
    assert ok.status_code == 200, ok.text
    assert ok.json()["amount_naira"] == 12345.67 and ok.json()["status"] == "approved"
    db.expire_all()
    assert db.query(Refund).filter_by(id=body["id"]).one().amount_kobo == 1_234_567
    audit = db.query(AdminAuditLog).filter(AdminAuditLog.action.in_(["refund.request", "refund.approve"])).count()
    assert audit == 2  # every write is audited


def test_approve_without_amount_takes_the_suggestion(db, client):
    refund = refund_row(db, booking_with(db), suggested=2_000_000)
    r = client.post(f"{URL}/{refund.id}/approve", json={}, headers=auth_headers(finance(db)))
    assert r.status_code == 200 and r.json()["amount_naira"] == 20000.0 and r.json()["destination"] == "original_method"


@pytest.mark.parametrize("body", [
    {"amount_naira": 0}, {"amount_naira": -5}, {"amount_naira": 10.123}, {"amount_naira": "abc"}, {"amount_naira": 1e12},
    {"destination": "cash"}, {"take_back_from_caregiver": "maybe"}, {"unexpected": 1},
])
def test_approve_body_validation_422s(db, client, body):
    refund = refund_row(db, booking_with(db))
    r = client.post(f"{URL}/{refund.id}/approve", json=body, headers=auth_headers(finance(db)))
    assert r.status_code == 422, r.text
    db.expire_all()
    assert db.get(Refund, refund.id).status == "pending_approval"


@pytest.mark.parametrize("path,body", [
    ("", {"amount_naira": 10, "reason_code": "complaint"}),  # no booking_id
    ("", {"booking_id": str(uuid.uuid4()), "amount_naira": 10, "reason_code": "bogus"}),
    ("", {"booking_id": str(uuid.uuid4()), "amount_naira": 0, "reason_code": "complaint"}),
    ("", {"booking_id": "not-a-uuid", "amount_naira": 10, "reason_code": "complaint"}),
    (f"/{FAKE}/reject", {"reason": "   "}),
    (f"/{FAKE}/reject", {}),
    (f"/{FAKE}/close", {"reason": ""}),
    (f"/{FAKE}/bank-details", {"bank_code": "058", "account_number": "12345"}),
    (f"/{FAKE}/bank-details", {"bank_code": "058", "account_number": "01234567890"}),
    (f"/{FAKE}/bank-details", {"bank_code": "", "account_number": "0123456789"}),
    (f"/{FAKE}/resolve", {"outcome": "maybe", "note": "x"}),
    (f"/{FAKE}/resolve", {"outcome": "failed", "note": "  "}),
])
def test_other_body_validation_422s(db, client, path, body):
    boss = super_admin(db)
    r = client.post(f"{URL}{path}", json=body, headers=auth_headers(boss))
    assert r.status_code == 422, r.text


def test_a_bad_account_number_422_does_not_echo_the_digits_back(db, client):
    boss = super_admin(db)
    for bad in ("98765", "09876543210", "98765abcde"):
        r = client.post(f"{URL}/{FAKE}/bank-details", json={"bank_code": "058", "account_number": bad},
                        headers=auth_headers(boss))
        assert r.status_code == 422 and bad not in r.text and "98765" not in r.text, r.text
        assert any("account_number" in e["loc"] for e in r.json()["detail"])  # still says which field is wrong


# --- service errors keep the {"detail": message} envelope --------------------------------------------------------------------
def test_service_errors_map_to_status_codes(db, client, fake_paystack):
    fin, other, boss = finance(db), finance(db), super_admin(db)
    h = auth_headers(fin)
    # 404
    r = client.post(f"{URL}/{uuid.uuid4()}/approve", json={}, headers=h)
    assert r.status_code == 404 and r.json() == {"detail": "Refund not found"}
    # 409: not pending
    done = refund_row(db, booking_with(db), status="succeeded", amount=1_000_000)
    r = client.post(f"{URL}/{done.id}/approve", json={}, headers=h)
    assert r.status_code == 409 and set(r.json()) == {"detail"} and r.json()["detail"]
    # 403: own staff refund; above the Finance limit
    mine = refund_row(db, booking_with(db), origin="staff", by=fin, reason="complaint")
    r = client.post(f"{URL}/{mine.id}/approve", json={}, headers=h)
    assert r.status_code == 403 and "raised this refund" in r.json()["detail"]
    big = refund_row(db, booking_with(db), suggested=6_000_000)
    r = client.post(f"{URL}/{big.id}/approve", json={}, headers=h)
    assert r.status_code == 403 and "limit" in r.json()["detail"]
    assert client.post(f"{URL}/{big.id}/approve", json={}, headers=auth_headers(boss)).status_code == 200
    # 422: a business-rule refusal (more than is left to refund)
    over = refund_row(db, booking_with(db, total=1_000_000), suggested=1_000_000)
    r = client.post(f"{URL}/{over.id}/approve", json={"amount_naira": 10_001}, headers=auth_headers(other))
    assert r.status_code == 422 and "left to refund" in r.json()["detail"]
    # 422 on create: nothing paid
    unpaid = booking_with(db, payment_status="pending")
    r = client.post(URL, json={"booking_id": str(unpaid.id), "amount_naira": 10, "reason_code": "complaint"}, headers=h)
    assert r.status_code == 422 and r.json() == {"detail": "This booking has no payment to refund"}
    # 409 on create: one open refund per booking
    taken = refund_row(db, booking_with(db))
    r = client.post(URL, json={"booking_id": str(taken.booking_id), "amount_naira": 10, "reason_code": "complaint"}, headers=h)
    assert r.status_code == 409
    # 404 on create: no such booking
    r = client.post(URL, json={"booking_id": FAKE, "amount_naira": 10, "reason_code": "complaint"}, headers=h)
    assert r.status_code == 404 and r.json() == {"detail": "Booking not found"}


def test_reject_and_the_state_it_leaves(db, client):
    refund = refund_row(db, booking_with(db))
    r = client.post(f"{URL}/{refund.id}/reject", json={"reason": "  not our fault  "}, headers=auth_headers(finance(db)))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected" and r.json()["rejected_reason"] == "not our fault"
    assert r.json()["available_actions"] == []


# --- the failure actions pass Paystack through ----------------------------------------------------------------------------
def make_failed(db, fake):
    """A refund Paystack failed: sent, then settled as failed through the webhook path."""
    from app.services import refunds

    booking = make_booking(db, make_patient(db), total_amount_kobo=BIG, channel="app")
    fake.initialize_transaction(None, "placeholder@example.com", BIG, booking.paystack_reference)
    fake.paid.add(booking.paystack_reference)
    refund = refund_row(db, booking, status="approved", suggested=BIG, amount=BIG, destination="original_method",
                        approved_at=datetime.utcnow())
    assert refunds.dispatch_due(db) == 1
    pid = db.query(RefundAttempt).filter_by(refund_id=refund.id).one().paystack_refund_id
    fake.settle_refund(pid, "failed")
    assert refunds.apply_refund_event(db, status="failed", transaction_reference=booking.paystack_reference,
                                      paystack_refund_id=pid, amount_kobo=BIG, reason="boom") == "applied"
    db.expire_all()
    return booking, db.get(Refund, refund.id), pid


def test_retry_through_the_api_asks_paystack_first(db, client, fake_paystack):
    h = auth_headers(finance(db))
    booking, refund, pid = make_failed(db, fake_paystack)
    booking2, refund2, pid2 = make_failed(db, fake_paystack)
    fake_paystack.refunds[pid2]["status"] = "processing"  # we recorded failed, Paystack is still working on it
    r = client.post(f"{URL}/{refund2.id}/retry", headers=h)
    assert r.status_code == 409 and set(r.json()) == {"detail"}
    db.expire_all()
    assert db.get(Refund, refund2.id).status == "failed"
    r = client.post(f"{URL}/{refund.id}/retry", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved" and r.json()["failure_reason"] is None
    assert [a["attempt"] for a in r.json()["attempts"]] == [1]  # the job makes attempt 2; attempt 1 stays as history


def test_switch_to_wallet_and_close_through_the_api(db, client, fake_paystack):
    booking, refund, pid = make_failed(db, fake_paystack)
    fin, boss = finance(db), super_admin(db)
    # Finance cannot switch (permission), a Super Admin can
    assert client.post(f"{URL}/{refund.id}/switch-to-wallet", headers=auth_headers(fin)).status_code == 403
    r = client.post(f"{URL}/{refund.id}/switch-to-wallet", headers=auth_headers(boss))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "succeeded" and r.json()["destination"] == "wallet"
    db.expire_all()
    assert db.query(PatientWallet).filter_by(patient_id=booking.patient_id).one().balance_kobo == BIG
    booking2, refund2, pid2 = make_failed(db, fake_paystack)
    r = client.post(f"{URL}/{refund2.id}/close", json={"reason": "family unreachable"}, headers=auth_headers(fin))
    assert r.status_code == 200 and r.json()["status"] == "closed" and "family unreachable" in r.json()["note"]


def test_paystack_unreachable_is_503(db, client, fake_paystack, monkeypatch):
    from app.services import refunds
    from app.services.paystack import PaystackService

    booking, refund, pid = make_failed(db, fake_paystack)
    monkeypatch.setattr(PaystackService, "_call", lambda self, *a, **kw: {"status": False, "ambiguous": True,
                                                                          "message": "timeout"})
    r = client.post(f"{URL}/{refund.id}/retry", headers=auth_headers(finance(db)))
    assert r.status_code == 503 and r.json() == {"detail": refunds.PAYSTACK_UNREACHABLE}
    db.expire_all()
    assert db.get(Refund, refund.id).status == "failed"


def test_resolve_through_the_api(db, client, fake_paystack):
    from app.services import refunds

    booking = make_booking(db, make_patient(db), total_amount_kobo=BIG, channel="app")
    fake_paystack.initialize_transaction(None, "placeholder@example.com", BIG, booking.paystack_reference)
    fake_paystack.paid.add(booking.paystack_reference)
    refund = refund_row(db, booking, status="approved", suggested=BIG, amount=BIG, destination="original_method",
                        approved_at=datetime.utcnow())
    assert refunds.dispatch_due(db) == 1
    pid = db.query(RefundAttempt).filter_by(refund_id=refund.id).one().paystack_refund_id
    assert refunds.apply_refund_event(db, status="processed", transaction_reference=booking.paystack_reference,
                                      paystack_refund_id=pid, amount_kobo=BIG - 1) == "review"
    db.expire_all()
    assert db.get(Refund, refund.id).status == "needs_review"
    fake_paystack.refunds[pid]["amount"] = BIG - 1  # the dashboard: processed, for one kobo less than we asked
    fake_paystack.settle_refund(pid, "processed")
    # review route is Super Admin only
    assert client.post(f"{URL}/{refund.id}/resolve", json={"outcome": "failed", "note": "checked"},
                       headers=auth_headers(finance(db))).status_code == 403
    boss = auth_headers(super_admin(db))
    # Paystack processed a different amount, so "failed" is refused with Paystack's word, in the {"detail"} shape
    r = client.post(f"{URL}/{refund.id}/resolve", json={"outcome": "failed", "note": "checked the dashboard"}, headers=boss)
    assert r.status_code == 409 and set(r.json()) == {"detail"}
    # "succeeded" for our amount is refused too, and the message names the amount Paystack confirmed
    r = client.post(f"{URL}/{refund.id}/resolve", json={"outcome": "succeeded", "note": "checked"}, headers=boss)
    assert r.status_code == 409 and "9999999 kobo" in r.json()["detail"]
    # recording what Paystack confirmed (in naira) settles it at that amount
    r = client.post(f"{URL}/{refund.id}/resolve", json={"outcome": "succeeded", "note": "checked", "amount_naira": 99999.99},
                    headers=boss)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "succeeded" and r.json()["amount_naira"] == 99999.99
    assert r.json()["review_flagged_at"] is None and r.json()["available_actions"] == []


# --- legacy routes ---------------------------------------------------------------------------------------------------------
PATIENT_GONE = "Refunds are now handled by the Oakly team. Contact support to request one."
ADMIN_MOVED = "This route has moved to /api/v1/admin/refunds."


def test_legacy_routes_answer_410(db, client):
    patient = make_patient(db)
    r = client.post(f"{API}/patient/wallet/refund/request", headers=auth_headers(patient.user),
                    json={"transaction_id": str(uuid.uuid4()), "amount": "10", "reason": "I changed my mind"})
    assert r.status_code == 410 and r.json() == {"detail": PATIENT_GONE}
    h = auth_headers(finance(db))
    r = client.get(f"{API}/patient/admin/refund/requests", headers=h)
    assert r.status_code == 410 and r.json() == {"detail": ADMIN_MOVED}
    r = client.put(f"{API}/patient/admin/refund/requests/{uuid.uuid4()}", headers=h, json={"action": "approve"})
    assert r.status_code == 410 and r.json() == {"detail": ADMIN_MOVED}
    # the permission check stays in front of the 410
    assert client.get(f"{API}/patient/admin/refund/requests").status_code == 401
    assert client.get(f"{API}/patient/admin/refund/requests", headers=auth_headers(admin_of(db, "support"))).status_code == 403
    assert client.post(f"{API}/patient/wallet/refund/request", json={}).status_code == 401


def test_legacy_refund_webhook_no_longer_touches_wallet(db, client):
    """A refund.* event that matches no v2 refund is ignored with a 200; the old pending refund transaction is not settled."""
    patient = make_patient(db)
    wallet = PatientWallet(patient_id=patient.id, balance_kobo=0)
    db.add(wallet)
    db.commit()
    tx = PatientTransaction(id=uuid.uuid4(), patient_id=patient.id, wallet_id=wallet.id, type="refund", status="pending",
                            amount_kobo=100_000, paystack_reference="777",
                            description=f"Refund to original payment [refund_request:{uuid.uuid4()}]")
    db.add(tx)
    db.commit()
    for event, status_ in (("refund.processed", "processed"), ("refund.failed", "failed")):
        body = json.dumps({"event": event, "data": {"id": 777, "status": status_, "transaction_reference": "DEP_legacy",
                                                   "amount": 100_000}}).encode()
        sig = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
        r = client.post(f"{API}/payment/paystack/webhook", content=body, headers={"x-paystack-signature": sig})
        assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(PatientTransaction, tx.id).status == "pending"
    assert db.get(PatientWallet, wallet.id).balance_kobo == 0
