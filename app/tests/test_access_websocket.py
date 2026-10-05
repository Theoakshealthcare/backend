"""WP-C item 2: authenticated booking WebSockets."""

import json
import uuid

import pytest
from starlette.websockets import WebSocketDisconnect

from app.core.security import create_access_token, create_refresh_token
from app.db.models.booking import BookingStatus, CaregiverLocation
from app.services.websocket_manager import websocket_manager
from app.tests.factories import make_booking, make_caregiver, make_patient

CAREGIVER_WS = "/api/v1/booking/ws/caregiver/location"
PATIENT_WS = "/api/v1/booking/ws/patient/tracking"


@pytest.fixture(autouse=True)
def _reset_manager():
    yield
    websocket_manager.caregiver_connections.clear()
    websocket_manager.patient_connections.clear()
    websocket_manager.tracking_subscriptions.clear()
    websocket_manager.last_heartbeat.clear()


def assert_closed_with(client, url, code, **kwargs):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(url, **kwargs):
            pass
    assert exc.value.code == code


def roundtrip(ws, message):
    """Send one message, then a heartbeat; return everything received before the heartbeat reply."""
    ws.send_text(json.dumps(message))
    ws.send_text(json.dumps({"type": "heartbeat"}))
    seen = []
    while True:
        data = json.loads(ws.receive_text())
        if data["type"] == "heartbeat":
            return seen
        seen.append(data)


def drain_connected(ws):
    assert json.loads(ws.receive_text())["type"] == "connection_status"


# --- authentication -------------------------------------------------------------


@pytest.mark.parametrize("url", [CAREGIVER_WS, PATIENT_WS])
def test_missing_token_closes_4401(client, url):
    assert_closed_with(client, url, 4401)


@pytest.mark.parametrize("url", [CAREGIVER_WS, PATIENT_WS])
def test_garbage_token_closes_4401(client, url):
    assert_closed_with(client, f"{url}?token=not-a-jwt", 4401)


@pytest.mark.parametrize("url", [CAREGIVER_WS, PATIENT_WS])
def test_refresh_token_closes_4401(db, client, url):
    user = make_caregiver(db).user if url == CAREGIVER_WS else make_patient(db).user
    assert_closed_with(client, f"{url}?token={create_refresh_token(user.id)}", 4401)


@pytest.mark.parametrize("url", [CAREGIVER_WS, PATIENT_WS])
def test_old_demo_token_value_does_not_authenticate(client, url):
    # the endpoints used to accept any non-empty token and run as a hardcoded demo user
    assert_closed_with(client, f"{url}?token=anything", 4401)


def test_inactive_user_closes_4401(db, client):
    patient = make_patient(db)
    patient.user.is_active = False
    db.commit()
    assert_closed_with(client, f"{PATIENT_WS}?token={create_access_token(patient.user.id)}", 4401)


def test_token_for_deleted_user_closes_4401(client):
    assert_closed_with(client, f"{PATIENT_WS}?token={create_access_token(uuid.uuid4())}", 4401)


# --- role separation --------------------------------------------------------------


def test_patient_cannot_open_caregiver_socket(db, client):
    patient = make_patient(db)
    assert_closed_with(client, f"{CAREGIVER_WS}?token={create_access_token(patient.user.id)}", 4403)
    assert websocket_manager.caregiver_connections == {}


def test_caregiver_cannot_open_patient_socket(db, client):
    caregiver = make_caregiver(db)
    assert_closed_with(client, f"{PATIENT_WS}?token={create_access_token(caregiver.user.id)}", 4403)
    assert websocket_manager.patient_connections == {}


# --- connecting as the right user -------------------------------------------------


def test_caregiver_connects_as_themselves_and_updates_their_own_location(db, client):
    caregiver = make_caregiver(db)
    token = create_access_token(caregiver.user.id)
    with client.websocket_connect(f"{CAREGIVER_WS}?token={token}") as ws:
        drain_connected(ws)
        assert str(caregiver.user.id) in websocket_manager.caregiver_connections
        assert "demo_caregiver_user_id" not in websocket_manager.caregiver_connections
        roundtrip(
            ws,
            {"type": "location_update", "data": {"latitude": 6.5, "longitude": 3.4, "is_available": True}},
        )
    db.expire_all()
    row = db.query(CaregiverLocation).filter(CaregiverLocation.caregiver_id == caregiver.id).first()
    assert row is not None and row.latitude == 6.5


def test_token_can_be_sent_in_the_subprotocol_header(db, client):
    caregiver = make_caregiver(db)
    token = create_access_token(caregiver.user.id)
    with client.websocket_connect(CAREGIVER_WS, subprotocols=["bearer", token]) as ws:
        drain_connected(ws)
        assert ws.accepted_subprotocol == "bearer"
        assert str(caregiver.user.id) in websocket_manager.caregiver_connections


def test_patient_connects_as_themselves(db, client):
    patient = make_patient(db)
    with client.websocket_connect(f"{PATIENT_WS}?token={create_access_token(patient.user.id)}") as ws:
        drain_connected(ws)
        assert str(patient.user.id) in websocket_manager.patient_connections
        assert "demo_patient_user_id" not in websocket_manager.patient_connections


# --- patient tracking: own booking, accepted / in_progress only ------------------


def subscribe(client, patient, booking):
    with client.websocket_connect(f"{PATIENT_WS}?token={create_access_token(patient.user.id)}") as ws:
        drain_connected(ws)
        seen = roundtrip(ws, {"type": "tracking_subscribe", "booking_id": str(booking.id)})
        subscribed = str(patient.user.id) in websocket_manager.tracking_subscriptions.get(
            str(booking.id), set()
        )
    return seen, subscribed


@pytest.mark.parametrize("status", [BookingStatus.ACCEPTED, BookingStatus.IN_PROGRESS])
def test_patient_can_track_own_active_booking(db, client, status):
    patient, caregiver = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient, caregiver, status=status)
    seen, subscribed = subscribe(client, patient, booking)
    assert subscribed
    assert not [m for m in seen if m["type"] == "error"]


def test_patient_cannot_track_someone_elses_booking(db, client):
    me, other = make_patient(db), make_patient(db)
    booking = make_booking(db, other, make_caregiver(db), status=BookingStatus.ACCEPTED)
    seen, subscribed = subscribe(client, me, booking)
    assert not subscribed
    assert [m for m in seen if m["type"] == "error"]


@pytest.mark.parametrize(
    "status", [BookingStatus.PENDING, BookingStatus.COMPLETED, BookingStatus.CANCELLED, BookingStatus.DECLINED]
)
def test_patient_cannot_track_booking_outside_accepted_or_in_progress(db, client, status):
    patient = make_patient(db)
    booking = make_booking(db, patient, make_caregiver(db), status=status)
    seen, subscribed = subscribe(client, patient, booking)
    assert not subscribed
    assert [m for m in seen if m["type"] == "error"]


def test_subscribe_with_malformed_booking_id_is_a_clean_error(db, client):
    patient = make_patient(db)
    with client.websocket_connect(f"{PATIENT_WS}?token={create_access_token(patient.user.id)}") as ws:
        drain_connected(ws)
        seen = roundtrip(ws, {"type": "tracking_subscribe", "booking_id": "not-a-uuid"})
    errors = [m for m in seen if m["type"] == "error"]
    assert errors and "not-a-uuid" not in errors[0]["error"]
