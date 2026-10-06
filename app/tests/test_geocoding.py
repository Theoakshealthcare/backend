"""The geocoder: Google's answer is only trusted at street level, a failure is "no result", the key is optional."""

import logging

import httpx
import pytest

from app.core.config import settings
from app.services.geocoding import GeocodeResult, GoogleGeocoder, NoopGeocoder, build_geocoder


def google_body(location_type="ROOFTOP", types=("street_address",), partial=False, lat=6.4474, lng=3.4723,
                status="OK", half_span=0.0005, admin="Lagos"):
    if status != "OK":
        return {"status": status, "results": []}
    result = {"types": list(types),
              "address_components": [{"long_name": "Nigeria", "types": ["country", "political"]},
                                     {"long_name": admin, "types": ["administrative_area_level_1", "political"]}],
              "geometry": {"location": {"lat": lat, "lng": lng}, "location_type": location_type,
                           "viewport": {"northeast": {"lat": lat + half_span, "lng": lng + half_span},
                                        "southwest": {"lat": lat - half_span, "lng": lng - half_span}}}}
    if partial:
        result["partial_match"] = True
    return {"status": "OK", "results": [result]}


def geocoder(body=None, status_code=200, raises=None, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if raises:
            raise raises
        return httpx.Response(status_code, json=body if body is not None else google_body())
    return GoogleGeocoder("test-key", timeout=2.0, transport=httpx.MockTransport(handler))


def ask(g):
    return g.geocode("12 Admiralty Way, Lekki", city="Lagos", state="Lagos")


def test_a_rooftop_street_address_is_accepted():
    assert ask(geocoder()) == GeocodeResult(lat=6.4474, lng=3.4723, precision="rooftop", admin_area="Lagos")


@pytest.mark.parametrize("location_type,types", [
    ("RANGE_INTERPOLATED", ("street_address",)),
    ("GEOMETRIC_CENTER", ("street_address",)),
    ("RANGE_INTERPOLATED", ("route",)),
    ("ROOFTOP", ("route",)),
    ("ROOFTOP", ("premise",)),
    ("ROOFTOP", ("establishment", "point_of_interest", "premise")),
])
def test_street_and_premise_level_results_are_accepted(location_type, types):
    assert ask(geocoder(google_body(location_type, types))) is not None


@pytest.mark.parametrize("body", [
    google_body("APPROXIMATE", ("locality", "political")),          # city level
    google_body("APPROXIMATE", ("street_address",)),                # Google itself is guessing
    google_body("GEOMETRIC_CENTER", ("locality", "political")),     # the middle of an area
    google_body("ROOFTOP", ("sublocality", "political")),           # a neighbourhood, not a door
    google_body("GEOMETRIC_CENTER", ("route",)),                    # the midpoint of a long road, no house number
    google_body("ROOFTOP", ("street_address",), half_span=0.01),    # viewport of ~3 km: not a building
    google_body("ROOFTOP", ("street_address",), partial=True),      # did not match everything that was typed
    google_body(status="ZERO_RESULTS"),
    google_body(status="OVER_QUERY_LIMIT"),
    google_body(status="REQUEST_DENIED"),
    {"status": "OK", "results": []},
    {"status": "OK", "results": [{"types": ["street_address"], "geometry": {"location_type": "ROOFTOP"}}]},
    {"status": "OK", "results": [{"types": ["street_address"], "geometry": {
        "location": {"lat": 6.4, "lng": 3.4}, "location_type": "ROOFTOP"}}]},  # no viewport: cannot tell how big
    {"status": "OK", "results": [{"types": ["street_address"], "geometry": {
        "location": {"lat": "x", "lng": None}, "location_type": "ROOFTOP"}}]},
    ["not", "an", "object"],
])
def test_anything_less_precise_or_malformed_is_no_result(body):
    assert ask(geocoder(body)) is None


@pytest.mark.parametrize("kwargs", [
    dict(status_code=500), dict(status_code=403), dict(raises=httpx.ConnectTimeout("slow")),
    dict(raises=httpx.ReadTimeout("slow")), dict(raises=httpx.ConnectError("down")),
])
def test_a_timeout_or_error_is_no_result_and_never_raises(kwargs):
    assert ask(geocoder(**kwargs)) is None


def test_non_json_is_no_result():
    g = GoogleGeocoder("k", timeout=2.0, transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")))
    assert ask(g) is None


def test_only_the_address_city_and_country_are_sent_and_the_request_has_a_timeout():
    seen = []
    ask(geocoder(seen=seen))
    (request,) = seen
    params = dict(request.url.params)
    assert request.method == "GET" and request.url.host == "maps.googleapis.com"
    assert params["address"] == "12 Admiralty Way, Lekki, Lagos, Lagos, Nigeria"
    assert params["components"] == "country:NG" and params["key"] == "test-key"
    assert set(params) == {"address", "components", "region", "key"}
    assert request.extensions["timeout"]["read"] == 2.0


def test_a_failure_logs_the_error_kind_but_not_the_address_or_the_key(caplog):
    with caplog.at_level(logging.DEBUG):
        ask(geocoder(raises=httpx.ConnectTimeout("slow")))
        ask(geocoder(status_code=500))
        ask(geocoder(google_body(status="REQUEST_DENIED")))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "Admiralty" not in text and "test-key" not in text and "Lekki" not in text
    assert "ConnectTimeout" in text and "REQUEST_DENIED" in text


def test_httpx_request_logging_cannot_leak_the_key_or_address():
    # httpx logs every request URL at INFO, and the Geocoding API takes the key and address in the query string
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_no_key_means_the_disabled_geocoder(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_MAPS_API_KEY", None)
    g = build_geocoder()
    assert isinstance(g, NoopGeocoder) and g.enabled is False and ask(g) is None
    monkeypatch.setattr(settings, "GOOGLE_MAPS_API_KEY", "   ")
    assert isinstance(build_geocoder(), NoopGeocoder)


def test_a_key_builds_the_google_geocoder(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_MAPS_API_KEY", "abc")
    g = build_geocoder()
    assert isinstance(g, GoogleGeocoder) and g.enabled is True
    assert "abc" not in repr(g)


def test_the_state_google_reports_is_returned_and_missing_is_none():
    assert ask(geocoder(google_body(admin="Ogun State"))).admin_area == "Ogun State"
    body = google_body()
    body["results"][0]["address_components"] = []
    assert ask(geocoder(body)).admin_area is None


def test_the_whole_call_has_a_hard_deadline_even_if_the_provider_trickles(monkeypatch):
    import time
    def slow(request):
        time.sleep(1.5)
        return httpx.Response(200, json=google_body())
    g = GoogleGeocoder("k", timeout=0.2, transport=httpx.MockTransport(slow))
    monkeypatch.setattr(g, "_deadline", 0.3)
    started = time.monotonic()
    assert ask(g) is None
    assert time.monotonic() - started < 1.0
