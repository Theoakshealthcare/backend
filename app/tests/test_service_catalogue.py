"""The service catalogue (phase 2, slice 1): seed, the bookable rule, the patient list, preview/create refusals,
the admin read/write, the audit row and the WhatsApp Flow's service list."""

import pytest
from sqlalchemy import text

from app.db.models.audit import AdminAuditLog
from app.db.models.booking import Booking
from app.db.models.booking_settings import BookingSettings
from app.db.models.service_catalog import ServiceCatalog
from app.services import service_catalog as catalog
from app.services.pricing import is_priced
from app.tests.booking_fixtures import make_prices as _make_prices
from app.tests.factories import auth_headers, make_admin, make_patient
from app.tests.fake_meta import fake_paystack  # noqa: F401  (fixture)

API = "/api/v1"
PREVIEW = {"service_type": "nurse", "duration_hours": 2}
CREATE = dict(service_type="nurse", assistance_type="x", duration_hours=2, scheduled_date="2030-01-01T10:00:00",
              patient_address="1 Test Close", patient_latitude=6.4, patient_longitude=3.4, patient_city="Lagos",
              patient_state="Lagos", booking_for="myself")
UNAVAILABLE = {"code": "service_unavailable",
               "message": "This service is not available right now. Please choose another service."}
NO_PRICE = {"code": "service_has_no_price",
            "message": "This service has no price yet. Set its multiplier in Pricing first, then enable it."}


def make_prices(db, **kw):
    return _make_prices(db, enable_services=False, **kw)


def _put_body(**overrides):
    body = dict(enabled=True, display_name="Doctor visit", mode="home", min_minutes=60, step_minutes=30,
                max_minutes=1440, sort_order=20)
    body.update(overrides)
    return body


def _row(db, service_type) -> ServiceCatalog:
    db.expire_all()
    return db.query(ServiceCatalog).filter(ServiceCatalog.service_type == service_type).one()


def _enable(db, *types):
    db.query(ServiceCatalog).filter(ServiceCatalog.service_type.in_(types)).update({"enabled": True}, synchronize_session=False)
    db.commit()


# --- the seed -----------------------------------------------------------------------------------------------------------
def test_seed_is_five_services_nurse_only_enabled_with_no_prices(db):
    rows = {r.service_type: r for r in db.query(ServiceCatalog).all()}
    assert set(rows) == {"nurse", "doctor", "physiotherapist", "caregiver", "nutritionist"}
    assert [t for t, r in rows.items() if r.enabled] == ["nurse"]
    assert {t: r.display_name for t, r in rows.items()} == {
        "nurse": "Nurse visit", "doctor": "Doctor visit", "physiotherapist": "Physiotherapy visit",
        "caregiver": "Caregiver visit", "nutritionist": "Nutrition consult"}
    # migration 0027: 30-minute steps everywhere, and the nutritionist's shortest booking is 30 minutes
    assert {t: (r.mode, r.min_minutes, r.step_minutes, r.max_minutes) for t, r in rows.items()} == {
        t: ("home", 30 if t == "nutritionist" else 60, 30, 1440) for t in rows}
    assert not any("price" in c.name or "multiplier" in c.name or "fee" in c.name for c in ServiceCatalog.__table__.columns)


def test_database_refuses_an_unknown_service_or_mode(db):
    for sql in ("update service_catalog set mode = 'teleport' where service_type = 'nurse'",
                "update service_catalog set service_type = 'wizard' where service_type = 'nurse'",
                "update service_catalog set max_minutes = 30 where service_type = 'nurse'"):
        with pytest.raises(Exception):
            db.execute(text(sql))
            db.commit()
        db.rollback()


# --- the "priced" rule --------------------------------------------------------------------------------------------------
class _S:
    def __init__(self, multipliers):
        self.multipliers = multipliers


@pytest.mark.parametrize("multipliers,service,expected", [
    (None, "nurse", True),                       # backward compatible: the nurse is sold at 1.0 with nothing set
    ({}, "nurse", True),
    (None, "doctor", False),                     # every other service needs an explicit multiplier
    ({}, "caregiver", False),
    ({"doctor": 1.5}, "doctor", True),
    ({"doctor": 1}, "doctor", True),             # an int is a number too
    ({"doctor": 0}, "doctor", False),
    ({"doctor": -1.0}, "doctor", False),
    ({"doctor": 11.0}, "doctor", False),         # above MAX_MULTIPLIER counts as not configured
    ({"doctor": True}, "doctor", False),
    ({"doctor": "1.5"}, "doctor", False),
    ({"nurse": 150.0}, "nurse", True),           # an unusable nurse value still falls back to the 1.0 default
    ({"doctor": 1.5}, "nurse", True),
])
def test_is_priced(multipliers, service, expected):
    assert is_priced(_S(multipliers), service) is expected


def test_no_pricing_row_means_nothing_is_priced():
    assert is_priced(None, "nurse") is False


# --- patient list --------------------------------------------------------------------------------------------------------
def test_services_list_needs_a_login(client):
    assert client.get(f"{API}/booking/services").status_code == 401


def test_services_list_is_empty_until_pricing_exists(db, client):
    patient = make_patient(db)
    r = client.get(f"{API}/booking/services", headers=auth_headers(patient.user))
    assert r.status_code == 200 and r.json() == {"services": []}


def test_services_list_is_nurse_only_with_no_multipliers_and_has_no_prices(db, client):
    make_prices(db, multipliers=None)
    patient = make_patient(db)
    r = client.get(f"{API}/booking/services", headers=auth_headers(patient.user))
    assert r.status_code == 200
    assert r.json() == {"services": [{"service_type": "nurse", "display_name": "Nurse visit", "mode": "home",
                                      "min_minutes": 60, "step_minutes": 30, "max_minutes": 1440}]}
    assert "fee" not in r.text and "multiplier" not in r.text and "price" not in r.text


def test_a_priced_service_stays_hidden_until_enabled_and_an_enabled_one_hidden_until_priced(db, client):
    patient = make_patient(db)
    headers = auth_headers(patient.user)

    def listed():
        return [s["service_type"] for s in client.get(f"{API}/booking/services", headers=headers).json()["services"]]

    make_prices(db, multipliers={"nurse": 1.0, "doctor": 2.0})
    assert listed() == ["nurse"]                      # priced but disabled: hidden
    _enable(db, "doctor", "caregiver")
    assert listed() == ["nurse", "doctor"]            # caregiver is enabled but has no multiplier: hidden; order is sort_order
    make_prices(db, multipliers={"nurse": 1.0, "doctor": 2.0, "caregiver": 0.8})  # a newer pricing version
    assert listed() == ["nurse", "doctor", "caregiver"]


# --- preview and create --------------------------------------------------------------------------------------------------
def test_preview_and_create_still_work_for_the_nurse_with_no_multipliers(db, client, fake_paystack):
    make_prices(db, multipliers=None)
    assert client.post(f"{API}/booking/preview", json=PREVIEW).status_code == 200
    patient = make_patient(db)
    r = client.post(f"{API}/booking/create", json=CREATE, headers=auth_headers(patient.user))
    assert r.status_code == 200 and r.json()["service_type"] == "nurse"


@pytest.mark.parametrize("multipliers,enabled", [
    ({"nurse": 1.0, "doctor": 2.0}, False),      # priced, not enabled
    ({"nurse": 1.0}, True),                      # enabled, no multiplier
])
def test_preview_and_create_refuse_a_service_that_is_not_bookable(db, client, fake_paystack, multipliers, enabled):
    make_prices(db, multipliers=multipliers)
    if enabled:
        _enable(db, "doctor")
    r = client.post(f"{API}/booking/preview", json={**PREVIEW, "service_type": "doctor"})
    assert r.status_code == 422 and r.json()["detail"] == UNAVAILABLE
    patient = make_patient(db)
    r = client.post(f"{API}/booking/create", json={**CREATE, "service_type": "doctor"}, headers=auth_headers(patient.user))
    assert r.status_code == 422 and r.json()["detail"] == UNAVAILABLE
    assert db.query(Booking).count() == 0 and fake_paystack.initialised == []


def test_preview_and_create_work_once_a_service_is_enabled_and_priced(db, client, fake_paystack):
    make_prices(db, multipliers={"nurse": 1.0, "doctor": 2.0})
    _enable(db, "doctor")
    r = client.post(f"{API}/booking/preview", json={**PREVIEW, "service_type": "doctor"})
    assert r.status_code == 200 and r.json()["service_multiplier"] == 2.0
    patient = make_patient(db)
    r = client.post(f"{API}/booking/create", json={**CREATE, "service_type": "doctor"}, headers=auth_headers(patient.user))
    assert r.status_code == 200


def test_a_disabled_nurse_is_refused_too(db, client):
    make_prices(db, multipliers=None)
    row = _row(db, "nurse")
    row.enabled = False
    db.commit()
    r = client.post(f"{API}/booking/preview", json=PREVIEW)
    assert r.status_code == 422 and r.json()["detail"] == UNAVAILABLE


# --- admin ---------------------------------------------------------------------------------------------------------------
def test_admin_list_says_which_services_are_priced_and_bookable(db, client):
    make_prices(db, multipliers={"nurse": 1.0, "doctor": 2.0})
    admin = make_admin(db, roles=("super_admin",))
    r = client.get(f"{API}/admin/service-catalog", headers=auth_headers(admin))
    assert r.status_code == 200
    rows = {x["service_type"]: x for x in r.json()}
    assert [x["service_type"] for x in r.json()] == ["nurse", "doctor", "physiotherapist", "caregiver", "nutritionist"]
    assert (rows["nurse"]["enabled"], rows["nurse"]["priced"], rows["nurse"]["bookable"]) == (True, True, True)
    assert (rows["doctor"]["enabled"], rows["doctor"]["priced"], rows["doctor"]["bookable"]) == (False, True, False)
    assert (rows["caregiver"]["priced"], rows["caregiver"]["bookable"]) == (False, False)
    assert set(rows["nurse"]) == {"service_type", "enabled", "display_name", "mode", "min_minutes", "step_minutes",
                                  "max_minutes", "sort_order", "priced", "bookable", "updated_at", "updated_by"}


def test_admin_cannot_enable_a_service_with_no_price(db, client):
    make_prices(db, multipliers={"nurse": 1.0})
    admin = make_admin(db, roles=("super_admin",))
    r = client.put(f"{API}/admin/service-catalog/doctor", json=_put_body(), headers=auth_headers(admin))
    assert r.status_code == 422 and r.json()["detail"] == NO_PRICE
    assert _row(db, "doctor").enabled is False
    assert db.query(AdminAuditLog).count() == 0


def test_admin_cannot_enable_anything_before_pricing_exists(db, client):
    admin = make_admin(db, roles=("super_admin",))
    r = client.put(f"{API}/admin/service-catalog/doctor", json=_put_body(), headers=auth_headers(admin))
    assert r.status_code == 422 and r.json()["detail"] == NO_PRICE


def test_admin_enables_a_priced_service_and_it_is_audited(db, client):
    make_prices(db, multipliers={"nurse": 1.0, "doctor": 2.0})
    admin = make_admin(db, roles=("super_admin",))
    r = client.put(f"{API}/admin/service-catalog/doctor", json={**_put_body(display_name="  Doctor home visit "), "reason": "launch"},
                   headers=auth_headers(admin))
    assert r.status_code == 200
    body = r.json()
    assert (body["enabled"], body["priced"], body["bookable"], body["display_name"]) == (True, True, True, "Doctor home visit")
    assert body["updated_by"] == str(admin.id)
    audit = db.query(AdminAuditLog).one()
    assert (audit.action, audit.entity_type, audit.entity_id, audit.reason) == (
        "service_catalog.update", "service_catalog", "doctor", "launch")
    assert audit.before["enabled"] is False and audit.after["enabled"] is True
    assert audit.after["display_name"] == "Doctor home visit"


def test_a_retried_put_is_a_no_op_with_one_audit_row(db, client):
    make_prices(db, multipliers={"nurse": 1.0, "doctor": 2.0})
    admin = make_admin(db, roles=("super_admin",))
    first = client.put(f"{API}/admin/service-catalog/doctor", json=_put_body(), headers=auth_headers(admin))
    second = client.put(f"{API}/admin/service-catalog/doctor", json=_put_body(), headers=auth_headers(admin))
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()  # same updated_at too: nothing was rewritten
    assert db.query(AdminAuditLog).count() == 1


def test_disabling_needs_no_price_and_an_enabled_service_may_be_edited_after_its_price_is_removed(db, client):
    make_prices(db, multipliers={"nurse": 1.0, "doctor": 2.0})
    admin = make_admin(db, roles=("super_admin",))
    h = auth_headers(admin)
    assert client.put(f"{API}/admin/service-catalog/doctor", json=_put_body(), headers=h).status_code == 200
    make_prices(db, multipliers={"nurse": 1.0})  # a newer pricing without the doctor multiplier
    rows = {x["service_type"]: x for x in client.get(f"{API}/admin/service-catalog", headers=h).json()}
    assert (rows["doctor"]["enabled"], rows["doctor"]["priced"], rows["doctor"]["bookable"]) == (True, False, False)
    assert client.put(f"{API}/admin/service-catalog/doctor", json=_put_body(display_name="Doctor"), headers=h).status_code == 200
    assert client.put(f"{API}/admin/service-catalog/doctor", json=_put_body(enabled=False), headers=h).status_code == 200


@pytest.mark.parametrize("override", [
    {"mode": "video"}, {"mode": "both"}, {"mode": "teleport"},
    {"step_minutes": 45}, {"step_minutes": 120}, {"step_minutes": 0},
    {"min_minutes": 20}, {"min_minutes": 45}, {"min_minutes": 100}, {"max_minutes": 1500}, {"max_minutes": 100},
    {"min_minutes": 0},
    {"min_minutes": 180, "max_minutes": 120},
    {"display_name": "  "}, {"display_name": "x" * 61}, {"sort_order": -1},
    {"enabled": "yes"}, {"min_minutes": 60.5}, {"unknown": 1},
])
def test_admin_put_validation(db, client, override):
    make_prices(db, multipliers={"nurse": 1.0, "doctor": 2.0})
    admin = make_admin(db, roles=("super_admin",))
    r = client.put(f"{API}/admin/service-catalog/doctor", json=_put_body(**override), headers=auth_headers(admin))
    assert r.status_code == 422
    assert db.query(AdminAuditLog).count() == 0


@pytest.mark.parametrize("override", [
    {"step_minutes": 60}, {"step_minutes": 30, "min_minutes": 30}, {"min_minutes": 90}, {"step_minutes": 15, "min_minutes": 45, "max_minutes": 600},
    {"min_minutes": 120, "max_minutes": 120},
])
def test_admin_put_accepts_half_hour_and_other_valid_lengths(db, client, override):
    make_prices(db, multipliers={"nurse": 1.0, "doctor": 2.0})
    admin = make_admin(db, roles=("super_admin",))
    r = client.put(f"{API}/admin/service-catalog/doctor", json=_put_body(**override), headers=auth_headers(admin))
    assert r.status_code == 200, r.text


def test_admin_put_unknown_service_is_404(db, client):
    admin = make_admin(db, roles=("super_admin",))
    r = client.put(f"{API}/admin/service-catalog/wizard", json=_put_body(), headers=auth_headers(admin))
    assert r.status_code == 404


def test_pricing_write_may_edit_but_other_roles_may_not(db, client):
    make_prices(db, multipliers={"nurse": 1.0, "doctor": 2.0})
    ops = make_admin(db, roles=("operations",))
    r = client.put(f"{API}/admin/service-catalog/doctor", json=_put_body(), headers=auth_headers(ops))
    assert r.status_code == 403
    assert client.get(f"{API}/admin/service-catalog", headers=auth_headers(ops)).status_code == 200


def test_the_catalogue_never_stores_or_returns_a_price(db, client):
    make_prices(db, multipliers={"nurse": 1.0, "doctor": 2.0})
    admin = make_admin(db, roles=("super_admin",))
    text_ = client.get(f"{API}/admin/service-catalog", headers=auth_headers(admin)).text
    assert "multiplier" not in text_ and "fee" not in text_ and "kobo" not in text_


# --- WhatsApp ------------------------------------------------------------------------------------------------------------
def test_flow_offers_only_bookable_services_in_catalogue_order(db):
    from app.channels.whatsapp.flows.service import FlowService

    make_prices(db, multipliers={"nurse": 1.0, "caregiver": 0.8, "doctor": 2.0})
    flow = FlowService.__new__(FlowService)
    flow.db = db
    assert flow._services() == ["nurse"]
    _enable(db, "doctor", "caregiver", "physiotherapist")  # physiotherapist has no multiplier: stays hidden
    assert flow._services() == ["nurse", "doctor", "caregiver"]


def test_flow_service_list_is_empty_without_pricing(db):
    from app.channels.whatsapp.flows.service import FlowService

    flow = FlowService.__new__(FlowService)
    flow.db = db
    assert flow._services() == []
