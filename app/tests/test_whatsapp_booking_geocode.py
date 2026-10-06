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
from app.tests.fake_geocoder import GARKI, LEKKI, LONDON, FakeGeocoder
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


@pytest.mark.parametrize("result", [None, GARKI, LONDON], ids=["no_result", "other_city", "outside_nigeria"])
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
