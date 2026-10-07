"""Pricing settings: versioned writes, reasons and audit rows, validation, and admin-only reads."""

import threading
import uuid

import pytest

from app.db.models.audit import AdminAuditLog
from app.db.models.booking_settings import BookingSettings
from app.db.session import SessionLocal
from app.services import pricing
from app.tests.booking_fixtures import make_prices
from app.tests.factories import auth_headers, make_admin, make_patient

URL = "/api/v1/booking-settings/"


def _body(**over):
    body = {"base_fee": 5000, "platform_fee_percent": 10, "multipliers": {"nurse": 1.0, "doctor": 2.0},
            "surge_pricing": False, "surge_multiplier": 1.0, "discount": 0, "reason": "Q4 price review"}
    body.update(over)
    return body


@pytest.fixture
def admin(db):
    return make_admin(db, password=None)


@pytest.fixture
def hdr(admin):
    return auth_headers(admin)


def _rows(db):
    db.expire_all()
    return db.query(BookingSettings).order_by(BookingSettings.created_at).all()


# --- versioning ---------------------------------------------------------------------------------

def test_put_publishes_a_new_row_and_leaves_the_old_one_untouched(db, client, hdr):
    old = make_prices(db)
    old_id, old_fee, old_updated = old.id, old.base_fee_kobo, old.updated_at
    r = client.put(f"{URL}{old_id}", headers=hdr, json=_body(base_fee=7000))
    assert r.status_code == 200, r.text
    assert r.json()["id"] != str(old_id) and r.json()["base_fee"] == 7000
    rows = _rows(db)
    assert len(rows) == 2
    assert rows[0].id == old_id and rows[0].base_fee_kobo == old_fee and rows[0].updated_at == old_updated
    assert rows[1].base_fee_kobo == 700000


def test_pricing_reads_the_new_version_but_a_booking_keeps_its_own(db, client, hdr):
    old = make_prices(db)
    assert client.put(f"{URL}{old.id}", headers=hdr, json=_body(base_fee=7000)).status_code == 200
    db.expire_all()
    assert pricing_latest(db).base_fee_kobo == 700000
    assert db.get(BookingSettings, old.id).base_fee_kobo == 1_000_000  # what an earlier quote points at


def pricing_latest(db):
    from app.services.booking_settings import latest
    return latest(db)


def test_put_on_a_superseded_version_is_refused(db, client, hdr):
    old = make_prices(db)
    assert client.put(f"{URL}{old.id}", headers=hdr, json=_body(base_fee=7000)).status_code == 200
    r = client.put(f"{URL}{old.id}", headers=hdr, json=_body(base_fee=9000))
    assert r.status_code == 409 and "newer pricing version" in r.json()["detail"]
    assert len(_rows(db)) == 2


def test_put_unknown_id_is_404(client, hdr):
    assert client.put(f"{URL}{uuid.uuid4()}", headers=hdr, json=_body()).status_code == 404


def test_post_publishes_a_new_latest_version(db, client, hdr):
    make_prices(db)
    r = client.post(URL, headers=hdr, json=_body(base_fee=6000))
    assert r.status_code == 200, r.text
    assert len(_rows(db)) == 2
    assert client.get(f"{URL}latest", headers=hdr).json()["id"] == r.json()["id"]


def test_delete_is_refused_and_removes_nothing(db, client, hdr):
    row = make_prices(db)
    r = client.delete(f"{URL}{row.id}", headers=hdr)
    assert r.status_code == 409
    assert len(_rows(db)) == 1
    assert db.query(AdminAuditLog).count() == 0


def test_concurrent_puts_on_the_same_version_publish_exactly_one(db, client, admin):
    old = make_prices(db)
    old_id = old.id
    hdr = auth_headers(admin)
    results = []

    def put(fee):
        results.append(client.put(f"{URL}{old_id}", headers=hdr, json=_body(base_fee=fee)).status_code)

    threads = [threading.Thread(target=put, args=(f,)) for f in (6000, 8000)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [200, 409]
    assert len(_rows(db)) == 2


# --- reason and audit ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["post", "put"])
@pytest.mark.parametrize("reason", [None, "", "   "])
def test_writes_need_a_reason(db, client, hdr, method, reason):
    row = make_prices(db)
    body = _body()
    body.pop("reason")
    if reason is not None:
        body["reason"] = reason
    url = URL if method == "post" else f"{URL}{row.id}"
    assert getattr(client, method)(url, headers=hdr, json=body).status_code == 422
    assert len(_rows(db)) == 1 and db.query(AdminAuditLog).count() == 0


def test_put_writes_an_audit_row_with_old_and_new_values(db, client, admin, hdr):
    old = make_prices(db)
    r = client.put(f"{URL}{old.id}", headers=hdr, json=_body(base_fee=7000, reason="Fuel costs"))
    assert r.status_code == 200
    db.expire_all()
    (a,) = db.query(AdminAuditLog).all()
    assert (a.action, a.entity_type, a.entity_id) == ("booking_settings.update", "booking_settings", r.json()["id"])
    assert a.admin_user_id == admin.id and a.reason == "Fuel costs"
    assert a.before["id"] == str(old.id) and a.before["base_fee"] == 10000
    assert a.after["base_fee"] == 7000 and a.after["id"] == r.json()["id"]


def test_post_writes_an_audit_row(db, client, hdr):
    r = client.post(URL, headers=hdr, json=_body())
    assert r.status_code == 200
    db.expire_all()
    (a,) = db.query(AdminAuditLog).all()
    assert a.action == "booking_settings.create" and a.before is None and a.after["base_fee"] == 5000


# --- validation ---------------------------------------------------------------------------------

@pytest.mark.parametrize("over", [
    {"base_fee": 0}, {"base_fee": -1}, {"base_fee": 1_000_001}, {"base_fee": "NaN"}, {"base_fee": None},
    {"platform_fee_percent": -0.1}, {"platform_fee_percent": 100.1}, {"platform_fee_percent": 150},
    {"discount": -1}, {"discount": 101},
    {"surge_multiplier": 0.9}, {"surge_multiplier": 5.1}, {"surge_multiplier": None},
    {"multipliers": {"nurse": 0}}, {"multipliers": {"nurse": -1}}, {"multipliers": {"nurse": 10.5}},
    {"multipliers": {"nurse": 150}}, {"multipliers": {"wizard": 1.0}}, {"multipliers": {"nurse": "x"}},
    {"surge_pricing": "maybe"}, {"unexpected": 1}, {"reason": "x" * 2001},
])
def test_invalid_values_are_rejected_and_nothing_is_written(db, client, hdr, over):
    make_prices(db)
    r = client.post(URL, headers=hdr, json=_body(**over))
    assert r.status_code == 422, r.text
    assert len(_rows(db)) == 1 and db.query(AdminAuditLog).count() == 0


def test_boundary_values_are_accepted(db, client, hdr):
    r = client.post(URL, headers=hdr, json=_body(
        base_fee=1_000_000, platform_fee_percent=100, discount=100, surge_pricing=True, surge_multiplier=5,
        multipliers={"nurse": 10, "doctor": 0.01}))
    assert r.status_code == 200, r.text


def test_put_unknown_field_refused(db, client, hdr):
    row = make_prices(db)
    assert client.put(f"{URL}{row.id}", headers=hdr, json=_body(id=str(uuid.uuid4()))).status_code == 422


# --- reads need an admin ------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["", "latest"])
def test_reads_need_an_admin_with_bookings_read(db, client, path):
    make_prices(db)
    assert client.get(f"{URL}{path}").status_code == 401
    patient = make_patient(db)
    assert client.get(f"{URL}{path}", headers=auth_headers(patient.user)).status_code == 401
    nobody = make_admin(db, ["read_only_analyst"], password=None)
    assert client.get(f"{URL}{path}", headers=auth_headers(nobody)).status_code == 403
    ok = make_admin(db, ["support"], password=None)
    assert client.get(f"{URL}{path}", headers=auth_headers(ok)).status_code == 200


def test_response_shape_is_unchanged(db, client, hdr):
    make_prices(db)
    body = client.get(f"{URL}latest", headers=hdr).json()
    assert set(body) == {"id", "base_fee", "platform_fee_percent", "multipliers", "surge_pricing",
                         "surge_multiplier", "discount", "created_at", "updated_at"}
    assert "reason" not in body
