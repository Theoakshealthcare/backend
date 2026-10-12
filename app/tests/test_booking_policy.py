"""The booking policy the admin edits: defaults, validation, the admin API and its audit row."""

import pytest

from app.db.models.audit import AdminAuditLog
from app.services import booking_policy
from app.services.booking_policy import PolicyInvalid
from app.tests.factories import auth_headers, make_admin
from app.tests.policy_helpers import set_policy

URL = "/api/v1/admin/booking-policy"
DEFAULTS = {
    "cancel_free_hours": 12, "cancel_late_hours": 2, "cancel_fee_percent": 20, "cancel_late_fee_percent": 50,
    "dropout_redispatch_min_lead_minutes": 120, "dropout_decision_lead_minutes": 60,
    "onmyway_compensation_window_minutes": 120, "caregiver_late_fee_share_percent": 80,
    "caregiver_visit_share_percent": 80, "earning_hold_hours": 24, "payout_cadence": "weekly",
    "payout_min_naira": 5000, "finance_refund_limit_naira": 50000,
}


def headers(db, role="super_admin"):
    return auth_headers(make_admin(db, [role], password=None))


def test_without_a_row_the_defaults_apply(db):
    assert booking_policy.current(db).as_dict() == DEFAULTS


def test_a_stored_row_overrides_the_defaults(db):
    set_policy(db, cancel_fee_percent=35)
    assert booking_policy.current(db).cancel_fee_percent == 35


@pytest.mark.parametrize("change", [
    {"cancel_fee_percent": 101}, {"cancel_fee_percent": -1}, {"cancel_late_fee_percent": 101},
    {"caregiver_late_fee_share_percent": 120}, {"caregiver_late_fee_share_percent": -5},
    {"cancel_free_hours": -1}, {"cancel_late_hours": -1}, {"dropout_decision_lead_minutes": -1},
    {"onmyway_compensation_window_minutes": -1},
    {"cancel_late_hours": 12}, {"cancel_late_hours": 13},      # the late window must be shorter than the free one
    {"cancel_free_hours": 2},                                  # ... in either direction
    {"dropout_decision_lead_minutes": 120}, {"dropout_redispatch_min_lead_minutes": 60},
    {"cancel_free_hours": 169}, {"cancel_late_hours": 169}, {"dropout_redispatch_min_lead_minutes": 10_081},
    {"dropout_decision_lead_minutes": 10_081}, {"onmyway_compensation_window_minutes": 10_081},
    {"cancel_free_hours": 2_147_483_648},                      # beyond a database integer: refused here, not a 500
    {"cancel_fee_percent": 60},                                # the late fee (50) may not be lower than the fee
    {"cancel_late_fee_percent": 10},
    {"caregiver_visit_share_percent": 101}, {"caregiver_visit_share_percent": -1},
    {"earning_hold_hours": 169}, {"earning_hold_hours": -1},
    {"payout_cadence": "daily"}, {"payout_cadence": ""}, {"payout_cadence": 7},
    {"payout_min_naira": -1}, {"payout_min_naira": 100_000_001},
    {"finance_refund_limit_naira": -1}, {"finance_refund_limit_naira": 100_000_001},
    {"earning_hold_hours": True},
])
def test_invalid_policies_are_refused(change):
    with pytest.raises(PolicyInvalid):
        booking_policy.validate({**DEFAULTS, **change})


def test_the_upper_bounds_and_the_late_fee_ordering_have_inclusive_edges():
    booking_policy.validate({**DEFAULTS, "cancel_free_hours": 168, "onmyway_compensation_window_minutes": 10_080,
                             "dropout_redispatch_min_lead_minutes": 10_080, "cancel_fee_percent": 50})


def test_an_oversized_integer_is_a_422_not_a_500(client, db):
    h = headers(db)
    for body in ({"cancel_free_hours": 2_147_483_648}, {"cancel_free_hours": 99_999_999_999_999},
                 {"onmyway_compensation_window_minutes": 10_081}, {"cancel_fee_percent": 60}):
        r = client.patch(URL, json={"reason": "too big", **body}, headers=h)
        assert r.status_code == 422, (body, r.text)
    assert booking_policy.current(db).as_dict() == DEFAULTS


def test_a_valid_edge_is_accepted():
    booking_policy.validate({**DEFAULTS, "cancel_fee_percent": 0, "cancel_late_fee_percent": 100,
                             "cancel_late_hours": 0, "dropout_decision_lead_minutes": 0})


# --- the admin API -----------------------------------------------------------------------------------------
def test_get_returns_the_policy(client, db):
    r = client.get(URL, headers=headers(db))
    assert r.status_code == 200
    body = r.json()
    assert {k: body[k] for k in DEFAULTS} == DEFAULTS and "updated_at" in body


def test_patch_changes_only_what_was_sent_and_writes_an_audit_row_with_old_and_new(client, db):
    h = headers(db)
    r = client.patch(URL, json={"reason": "Raise the late fee for launch", "cancel_late_fee_percent": 60}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["cancel_late_fee_percent"] == 60 and r.json()["cancel_fee_percent"] == 20
    assert booking_policy.current(db).cancel_late_fee_percent == 60
    row = db.query(AdminAuditLog).filter_by(action="booking_policy.update").one()
    assert row.reason == "Raise the late fee for launch"
    assert row.before["cancel_late_fee_percent"] == 50 and row.after["cancel_late_fee_percent"] == 60
    assert row.entity_type == "booking_policy"


def test_patch_without_a_reason_or_without_a_change_is_refused(client, db):
    h = headers(db)
    assert client.patch(URL, json={"cancel_fee_percent": 25}, headers=h).status_code == 422
    assert client.patch(URL, json={"reason": "  ", "cancel_fee_percent": 25}, headers=h).status_code == 422
    assert client.patch(URL, json={"reason": "nothing to change"}, headers=h).status_code == 422
    assert db.query(AdminAuditLog).filter_by(action="booking_policy.update").count() == 0


def test_patch_refuses_an_unknown_field_and_an_invalid_combination_and_changes_nothing(client, db):
    h = headers(db)
    assert client.patch(URL, json={"reason": "x", "typo_field": 1}, headers=h).status_code == 422
    r = client.patch(URL, json={"reason": "bad", "cancel_late_hours": 20}, headers=h)  # not shorter than 12 h free
    assert r.status_code == 422 and "late" in r.json()["detail"].lower()
    r = client.patch(URL, json={"reason": "bad", "cancel_fee_percent": 150}, headers=h)
    assert r.status_code == 422
    assert booking_policy.current(db).as_dict() == DEFAULTS
    assert db.query(AdminAuditLog).filter_by(action="booking_policy.update").count() == 0


def test_the_combination_is_checked_against_the_stored_values(client, db):
    h = headers(db)
    assert client.patch(URL, json={"reason": "tighten", "cancel_free_hours": 6}, headers=h).status_code == 200
    r = client.patch(URL, json={"reason": "bad", "cancel_late_hours": 6}, headers=h)
    assert r.status_code == 422


def test_roles_support_can_read_but_only_settings_writers_can_change(client, db):
    assert client.get(URL, headers=headers(db, "support")).status_code == 200
    assert client.patch(URL, json={"reason": "r", "cancel_fee_percent": 30}, headers=headers(db, "support")).status_code == 403
    assert client.patch(URL, json={"reason": "r", "cancel_fee_percent": 30}, headers=headers(db, "operations")).status_code == 200
    assert client.get(URL).status_code == 401


# --- caregiver earnings and the settings later slices read --------------------------------------------------
def test_the_earnings_settings_default_and_can_be_edited_with_an_audit_row(client, db):
    h = headers(db)
    body = client.get(URL, headers=h).json()
    assert (body["caregiver_visit_share_percent"], body["earning_hold_hours"], body["payout_cadence"],
            body["payout_min_naira"], body["finance_refund_limit_naira"]) == (80, 24, "weekly", 5000, 50000)
    r = client.patch(URL, json={"reason": "Pilot terms", "caregiver_visit_share_percent": 75, "earning_hold_hours": 48,
                                "payout_cadence": "biweekly", "payout_min_naira": 10000,
                                "finance_refund_limit_naira": 20000}, headers=h)
    assert r.status_code == 200, r.text
    assert (r.json()["caregiver_visit_share_percent"], r.json()["payout_cadence"]) == (75, "biweekly")
    row = db.query(AdminAuditLog).filter_by(action="booking_policy.update").one()
    assert row.before["caregiver_visit_share_percent"] == 80 and row.after["caregiver_visit_share_percent"] == 75
    assert row.before["payout_cadence"] == "weekly" and row.after["payout_cadence"] == "biweekly"


@pytest.mark.parametrize("change", [
    {"caregiver_visit_share_percent": 101}, {"earning_hold_hours": 169}, {"payout_cadence": "daily"},
    {"payout_min_naira": -1}, {"finance_refund_limit_naira": 100_000_001}, {"earning_hold_hours": "24"},
    {"caregiver_visit_share_percent": 80.5},
])
def test_the_api_refuses_invalid_earnings_settings_and_changes_nothing(client, db, change):
    r = client.patch(URL, json={"reason": "bad", **change}, headers=headers(db))
    assert r.status_code == 422, r.text
    assert booking_policy.current(db).as_dict() == DEFAULTS
    assert db.query(AdminAuditLog).filter_by(action="booking_policy.update").count() == 0


def test_only_settings_writers_can_change_the_earnings_settings(client, db):
    for role, code in (("support", 403), ("finance", 403), ("operations", 200)):
        r = client.patch(URL, json={"reason": "r", "earning_hold_hours": 12}, headers=headers(db, role))
        assert r.status_code == code, (role, r.text)
