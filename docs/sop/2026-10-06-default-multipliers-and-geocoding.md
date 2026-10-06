# SOP: default service multipliers and geocoding of typed addresses

| | |
|---|---|
| **Date** | 2026-10-06 |
| **Branch** | `feat/default-multipliers-geocode` (from `main` at `0c02df1`). Not pushed. |
| **Migration** | None. `location_source` lives in the quote payload (JSON). |
| **Extends** | [W2 booking in chat](2026-10-06-whatsapp-booking.md) |

## What shipped
1. **Default multiplier 1.0 in both channels.** One lookup, `pricing.service_multiplier(settings, service_type, channel=...)`, used by the app's preview and create, the WhatsApp quote and the Flow's price list.
2. **Geocoding for no-pin bookings.** A typed address is geocoded before the city-centre placeholder is used.

## Why
Owner decisions. WhatsApp refused to quote a service with no multiplier while the app silently used 1.0, so the channels disagreed. Admins will configure multipliers later (A4). Separately, every no-pin booking was held for a manual address check even when the address was perfectly findable.

## How
| Decision | Detail |
|---|---|
| Missing or unusable = default | Missing, `None`, non-numeric, boolean, zero or negative all count as "not configured" and give 1.0. This also fixes the app path, where a stored `0` used to price a booking at nothing. |
| Flagged, not silent | `logger.warning("pricing: no usable multiplier configured, using default 1.0 service=<s> channel=<c>")`. Service and channel only, no personal data. Fires on every use, so the Flow's per-service price list adds up to five lines per render. |
| Every service offered | `offered_services`, `explicit_multiplier` and `MissingMultiplier` are gone. The Flow lists all of `SERVICE_ORDER` when a settings row exists; with no settings row it still ends the Flow, and a quote with no settings row still hands over to a person. |
| Quote lock unchanged | The quote still stores its breakdown and the booking is created from it. |
| `Geocoder` seam | `app/services/geocoding.py`: `GoogleGeocoder` (httpx, `GEOCODING_TIMEOUT_SECONDS`, default 3 s), `NoopGeocoder` (no `GOOGLE_MAPS_API_KEY`), `FakeGeocoder` in `app/tests/fake_geocoder.py`. Only the address, city, state and "Nigeria" are sent. Nothing raises; any failure is "no result", logged by error kind only. |
| Precision | Accept `location_type` ROOFTOP / RANGE_INTERPOLATED / GEOMETRIC_CENTER **and** a result type of street_address / premise / subpremise / route / intersection, and not `partial_match`. APPROXIMATE and area-level results are rejected. |
| City check | `booking_rules.CITY_BOUNDS` boxes (Lagos, Abuja) and `in_city()`. A result in another city, state or country is rejected. |
| Off the locks | `shared_location` returns an `after_commit` step (the same mechanism as the Paystack link), staying in `awaiting_location`. The step reads the quote, ends its transaction, makes the HTTP call, then locks the quote only to write the result, re-checking it is still `submitted` (the person may have cancelled meanwhile; then nothing is written or sent). |
| Source recorded | `quote.payload["location_source"]`: `pin`, `geocoded` or `placeholder`. A booking is held (`needs_address_check`) exactly when it is `placeholder`. |

Rejected: a migration column on `bookings` (nothing reads it yet; the quote is linked by `quotes.booking_id`); rejecting partial matches was kept because a hold is the safe failure.

## What's involved
- `app/services/pricing.py` (`service_multiplier`, `price_service`), `app/services/booking.py` (preview and create use the shared lookup)
- `app/services/geocoding.py` (new), `app/core/config.py` (`GOOGLE_MAPS_API_KEY`, `GEOCODING_TIMEOUT_SECONDS`), `.env.example`
- `app/channels/whatsapp/booking_chat.py` (`shared_location`, `_geocode_then_price`, `_use_placeholder`), `booking_rules.py` (`CITY_BOUNDS`, `in_city`), `flows/service.py`
- Tests: `test_geocoding.py`, `test_whatsapp_booking_geocode.py`, updated `test_whatsapp_booking_rules.py`, `test_whatsapp_booking_chat.py`, `test_whatsapp_flow_endpoint.py`

## Verification
Unit and integration tests against the real Postgres (see the hand-back for the full-suite result). The geocode tests prove: a confident in-city result is used and not held; no result, wrong city, abroad, or an exception is held; a pin skips the lookup; the HTTP call runs with the quote and conversation rows unlocked (`FOR UPDATE NOWAIT` from a second session) and no open transaction; a quote cancelled during the lookup is left alone. **Not run against the real Google API**: the response shape comes from Google's documented format and fixtures.

## Operate / roll back
- Turn on: set `GOOGLE_MAPS_API_KEY` (restrict the key to the Geocoding API and the server IP). Turn off: unset it; behaviour is exactly the old hold.
- Watch: `grep "no usable multiplier configured"` for unconfigured services; `whatsapp quote <id> location source=` lines for the geocoded / placeholder split.
- Roll back: revert the commits; no schema change.

## Follow-ups
- A4 admin UI for multipliers (until then, defaults apply and are logged).
- Tune `CITY_BOUNDS` with real addresses; add cities there when `WHATSAPP_SERVICE_CITIES` grows (a served city without a box is never auto-accepted, so it stays held).
- A2's confirm-address action should read `location_source` from the linked quote.
- The geocoded point is street level, not a pin: consider showing the address back to the person for confirmation.
