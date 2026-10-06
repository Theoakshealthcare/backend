"""W2 follow-up: a booking with no location pin tries to geocode the typed address before falling back to the
city-centre placeholder and the address-check hold."""

import json

import pytest
from sqlalchemy.exc import OperationalError

from app.channels.whatsapp import booking_chat
from app.channels.whatsapp import booking_rules as rules
from app.db.models.booking import Booking, PaymentStatus
from app.db.models.support import SupportConversation
from app.db.models.whatsapp import WhatsAppConversation
from app.db.models.whatsapp_booking import Quote
from app.db.session import SessionLocal
from app.services import dispatch
from app.services.booking_payment import settle_booking_payment
from app.services.geocoding import GeocodeResult
from app.tests.fake_geocoder import GARKI, LEKKI, LONDON, NO_STATE, OTA, FakeGeocoder
from app.tests.test_whatsapp_booking_chat import (  # noqa: F401  (fixtures and helpers)
    chat, dispatched, pin, reply_text, start, the_booking, the_quote,
)

CITY_CENTRE_LAGOS = (6.5244, 3.3792)


@pytest.fixture
def geocoder(monkeypatch):
    fake = FakeGeocoder(LEKKI)
    monkeypatch.setattr(booking_chat, "build_geocoder", lambda: fake)
    return fake


def no_pin(chat):
    return chat.say("skip")


def test_a_geocoded_address_inside_the_city_is_used_and_the_booking_is_not_held(db, chat, client, booking_env, geocoder):
    start(chat, client, booking_env)
    sent = no_pin(chat)
    quote = the_quote(db)
    assert chat.state == "quoted" and quote.status == "quoted"
    assert (quote.payload["lat"], quote.payload["lng"]) == (LEKKI.lat, LEKKI.lng)
    assert quote.payload["needs_address_check"] is False and quote.payload["location_source"] == "geocoded"
    assert sent[0]["interactive"]["type"] == "button" and "₦44,000" in reply_text(sent[0])


def test_the_provider_is_given_only_the_typed_address_and_the_city_and_state(db, chat, client, booking_env, geocoder):
    start(chat, client, booking_env)
    no_pin(chat)
    (call,) = geocoder.calls
    payload = the_quote(db).payload
    assert call == {"address": payload["address"], "city": "Lagos", "state": "Lagos"}


def test_a_geocoded_booking_is_not_held_and_is_dispatched_once_paid(db, chat, client, booking_env, geocoder, dispatched):
    start(chat, client, booking_env)
    no_pin(chat)
    chat.tap("quote:confirm")
    booking = the_booking(db)
    assert booking.needs_address_check is False
    assert (booking.patient_latitude, booking.patient_longitude) == (LEKKI.lat, LEKKI.lng)
    settle_booking_payment(db, booking.id, booking.total_amount_kobo)
    assert the_booking(db).payment_status == PaymentStatus.PAID and dispatched == [booking.id]


@pytest.mark.parametrize("result", [None, GARKI, LONDON, OTA, NO_STATE],
                         ids=["no_result", "other_city", "outside_nigeria", "inside_box_but_ogun", "no_state_reported"])
def test_no_confident_result_in_the_booking_city_keeps_the_placeholder_and_the_hold(db, chat, client, booking_env,
                                                                                     geocoder, result):
    geocoder.result = result  # GARKI is in Abuja, the booking is for Lagos
    start(chat, client, booking_env)
    no_pin(chat)
    quote = the_quote(db)
    assert chat.state == "quoted" and quote.status == "quoted"
    assert (quote.payload["lat"], quote.payload["lng"]) == CITY_CENTRE_LAGOS
    assert quote.payload["needs_address_check"] is True and quote.payload["location_source"] == "placeholder"


def test_a_geocoder_that_blows_up_is_treated_as_no_result(db, chat, client, booking_env, geocoder):
    geocoder.raises = RuntimeError("provider exploded")
    start(chat, client, booking_env)
    sent = no_pin(chat)
    quote = the_quote(db)
    assert chat.state == "quoted" and quote.payload["needs_address_check"] is True
    assert sent[0]["interactive"]["type"] == "button"


def test_a_shared_pin_wins_and_the_geocoder_is_not_asked(db, chat, client, booking_env, geocoder):
    start(chat, client, booking_env)
    pin(chat, 6.4300, 3.4200)
    quote = the_quote(db)
    assert geocoder.calls == []
    assert (quote.payload["lat"], quote.payload["lng"]) == (6.43, 3.42)
    assert quote.payload["needs_address_check"] is False and quote.payload["location_source"] == "pin"


def test_a_pin_outside_nigeria_is_no_pin_so_the_address_is_geocoded(db, chat, client, booking_env, geocoder):
    start(chat, client, booking_env)
    pin(chat, 51.5, -0.12)
    assert len(geocoder.calls) == 1 and the_quote(db).payload["location_source"] == "geocoded"


def test_with_no_geocoder_configured_it_is_todays_behaviour(db, chat, client, booking_env):
    start(chat, client, booking_env)  # no key in the test environment: the disabled geocoder
    no_pin(chat)
    quote = the_quote(db)
    assert (quote.payload["lat"], quote.payload["lng"]) == CITY_CENTRE_LAGOS
    assert quote.payload["needs_address_check"] is True and quote.payload["location_source"] == "placeholder"


def test_the_http_call_runs_with_no_row_locked_and_no_open_transaction(db, chat, client, booking_env, geocoder):
    seen = {}

    def probe():
        other = SessionLocal()
        try:
            seen["in_transaction"] = db.in_transaction()
            for model in (Quote, WhatsAppConversation):  # NOWAIT: raises at once if anything holds the row
                other.query(model).with_for_update(nowait=True).all()
            seen["unlocked"] = True
        except OperationalError:
            seen["unlocked"] = False
        finally:
            other.rollback()
            other.close()

    geocoder.on_call = probe
    start(chat, client, booking_env)
    no_pin(chat)
    assert seen == {"in_transaction": False, "unlocked": True}


def test_a_quote_cancelled_while_geocoding_is_left_alone_and_nothing_is_sent(db, chat, client, booking_env, geocoder):
    def cancel_meanwhile():
        other = SessionLocal()
        other.query(Quote).update({"status": "cancelled"})
        other.commit()
        other.close()

    geocoder.on_call = cancel_meanwhile
    start(chat, client, booking_env)
    sent = no_pin(chat)
    quote = the_quote(db)
    assert quote.status == "cancelled" and "lat" not in quote.payload and db.query(Booking).count() == 0
    assert sent == []


def test_a_missing_price_still_ends_with_a_person_following_up(db, chat, client, booking_env, geocoder):
    from app.db.models.booking_settings import BookingSettings
    start(chat, client, booking_env)
    db.query(BookingSettings).delete()
    db.commit()
    sent = no_pin(chat)
    assert chat.state == "human" and "can't price" in reply_text(sent[0])
    assert db.query(SupportConversation).filter_by(reason="booking_placeholder").count() == 1


@pytest.mark.parametrize("city", ["Lagos", "Abuja"])
def test_the_city_boxes_contain_their_centre_and_exclude_the_other(city):
    other = "Abuja" if city == "Lagos" else "Lagos"
    assert rules.in_city(city, *rules.CITY_CENTRE[city])
    assert not rules.in_city(city, *rules.CITY_CENTRE[other])
    assert not rules.in_city(city, 51.5, -0.12) and not rules.in_city("Kano", 12.0, 8.5)


def _move_conversation(state, quote_id_in_context=True):
    other = SessionLocal()
    row = other.query(WhatsAppConversation).one()
    row.state = state
    row.automation_paused = state in ("human", "emergency")
    if not quote_id_in_context:
        row.context = {k: v for k, v in row.context.items() if k != "quote_id"}
    other.commit()
    other.close()


@pytest.mark.parametrize("state", ["human", "emergency"])
def test_a_conversation_that_went_to_a_person_or_an_emergency_mid_lookup_is_left_alone(db, chat, client, booking_env,
                                                                                         geocoder, state):
    geocoder.on_call = lambda: _move_conversation(state)
    start(chat, client, booking_env)
    sent = no_pin(chat)
    db.expire_all()
    row = db.query(WhatsAppConversation).one()
    assert sent == []  # no price after an emergency
    assert row.state == state and row.automation_paused is True
    assert the_quote(db).status == "submitted" and "lat" not in the_quote(db).payload


def test_a_conversation_that_moved_to_another_quote_mid_lookup_is_left_alone(db, chat, client, booking_env, geocoder):
    geocoder.on_call = lambda: _move_conversation("awaiting_location", quote_id_in_context=False)
    start(chat, client, booking_env)
    assert no_pin(chat) == [] and the_quote(db).status == "submitted"


def test_the_quote_and_the_conversation_change_in_one_transaction(db, chat, client, booking_env, geocoder, monkeypatch):
    start(chat, client, booking_env)
    no_pin(chat)
    db.expire_all()
    row = db.query(WhatsAppConversation).one()
    assert the_quote(db).status == "quoted" and row.state == "quoted" and row.automation_paused is False
    assert row.context["quote_id"] == str(the_quote(db).id)
    assert "geocode_in_flight" not in the_quote(db).payload


def test_a_failed_lookup_that_ends_with_a_person_pauses_automation_consistently(db, chat, client, booking_env, geocoder):
    from app.db.models.booking_settings import BookingSettings
    start(chat, client, booking_env)
    db.query(BookingSettings).delete()
    db.commit()
    no_pin(chat)
    db.expire_all()
    row = db.query(WhatsAppConversation).one()
    assert row.state == "human" and row.automation_paused is True


def test_a_second_lookup_for_the_same_quote_is_skipped_while_one_is_in_flight(db, chat, client, booking_env, geocoder):
    results = []

    def second_message_arrives():
        other = SessionLocal()
        quote = other.query(Quote).one()
        results.append(booking_chat._geocode_then_price(other, quote.contact_id, quote.id))
        other.close()

    geocoder.on_call = second_message_arrives
    start(chat, client, booking_env)
    sent = no_pin(chat)
    assert len(geocoder.calls) == 1 and results[0].replies == [] and sent  # the first lookup still answers


def test_a_stale_in_flight_marker_does_not_block_a_new_lookup(db, chat, client, booking_env, geocoder):
    start(chat, client, booking_env)
    quote = the_quote(db)
    quote.payload = {**quote.payload, "geocode_in_flight": "2020-01-01T00:00:00"}
    db.commit()
    no_pin(chat)
    assert len(geocoder.calls) == 1 and the_quote(db).status == "quoted"


def test_if_the_lookup_step_crashes_the_person_gets_a_neutral_message_not_a_payment_error(db, chat, client,
                                                                                         booking_env, geocoder,
                                                                                         monkeypatch):
    def boom(ctx, quote):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(booking_chat, "_price", boom)
    start(chat, client, booking_env)
    sent = no_pin(chat)
    text = reply_text(sent[0]).lower()
    assert "couldn't price" in text and "payment" not in text and "link" not in text
    assert db.query(SupportConversation).filter_by(reason="booking_placeholder").count() == 1
