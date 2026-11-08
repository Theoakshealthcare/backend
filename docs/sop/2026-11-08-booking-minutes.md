# SOP: booking durations in minutes, 30-minute steps (phase 2, slice 2)

| | |
|---|---|
| **What shipped** | Bookings are stored and priced in **minutes** (`bookings.duration_minutes`, migration `0027`; `duration_hours` is now a computed property). Preview and create accept `duration_minutes` and still accept `duration_hours`; responses return both. The length is validated against the service catalogue (min / step / max) with `422 invalid_duration`. The price prorates by minutes with one rounding rule. |
| **Branch / commit** | `feature/booking-minutes`, stacked on `feature/service-catalogue` ([SOP slice 1](2026-11-08-service-catalogue.md)). Not pushed; the lead pushes after review. |
| **Owner decisions (2026-11-08)** | Durations move to half-hour steps with a shortest booking per service (nutritionist 30, the rest 60); prices and surge unknown, so nothing is invented; every service is a home visit. |

## Why
Services such as a nutrition consult are shorter than an hour, and the owner wants half-hour steps. Hours as an integer column could not express that, and every consumer of the hours (price, end time, short-visit rule) needs the same exact length.

## How (decisions)
| Decision | Why / alternative rejected |
|---|---|
| **Store minutes; derive hours.** The `duration_hours` column is dropped (backfilled `x 60` into `duration_minutes`, NOT NULL, `> 0`). `Booking.duration_hours` returns an `int` for whole hours (4) and a `float` otherwise (1.5), and has a setter | One source of truth. Rejected: keeping both columns (they would drift). The setter keeps old callers (`make_booking(duration_hours=2)`, scripts) working |
| **Request: `duration_minutes` OR `duration_hours` (or both if they agree).** Hours may be fractional (`1.5`) if it is a whole number of minutes. Both missing, both disagreeing, or a non-whole-minute value is a FastAPI `422` list (`loc: ["body"]`) | The shipped apps send `duration_hours`; they keep working |
| **Response: both `duration_minutes` and `duration_hours`** on preview, the booking views, the caregiver home, offers and the admin detail. `duration_hours` is a JSON integer for whole hours, a decimal otherwise | Compatibility with apps that read `duration_hours` as a number; risks below |
| **`422 invalid_duration`** `detail = {code, message, min_minutes, step_minutes, max_minutes, allowed_minutes[]}` when the length is not min, min + step, ... up to max for the service. It also replaces the old `Duration must be between 1 and 24 hours` list error | The catalogue is the single rule; the allowed values let the app show the choices. A hidden service is refused first (`service_unavailable`). WhatsApp bookings (`locked_price`) skip the check at booking time (the Flow checks it, see below) |
| **Catalogue seed (migration 0027, not an edit of 0026):** step 30 everywhere; nutritionist min 30, others min 60, max 1440. Admin validation now allows step 15, 30 or 60, with min and max multiples of the step | The PR 1 limits (whole hours only) are relaxed in the stacked PR as planned. Admin may change any value |
| **Rounding rule (one):** the price is the exact `Decimal` product `base fee/hour x multiplier x minutes / 60`, then surge, discount and platform fee all unrounded; each reported amount (base, surge, discount, service fee, platform fee) is rounded to whole kobo **half-up, once**, from the exact value. `total = service fee + platform fee`. The service fee is **at least 1 kobo** when the exact value is above zero | Prorating adds no rounding step of its own, so `quantity=hours` and `minutes=hours x 60` give identical results (tested). The 1-kobo floor makes "a short visit never prices at nothing" unconditional even for absurd sub-kobo prices; a 100 % discount is still free |
| **Short-visit rule uses booked MINUTES** (`earnings.is_short_visit`): on-site time x 100 `<` booked minutes x `short_visit_min_percent` | A 90-minute booking is short under 45 minutes at 50 % (was: whole booked hours) |
| **WhatsApp stays whole hours** (the quote payload keeps `duration_hours`, `price_service` is called with hours, "Base fee (N h)" text) but the Flow's duration options and its answer check now follow the catalogue (a service whose minimum is 2 h offers 2, 3, ... hours) | Smallest change that keeps the channel correct; half-hour WhatsApp bookings are a follow-up |

## Every use of `duration_hours` in `app/` (walked, 2026-11-08)
| Place | What changed |
|---|---|
| `db/models/booking.py` | column `duration_minutes` (+ check `> 0`); `duration_hours` property and setter |
| `schemas/booking.py` `BookingCreate`, `BookingPreviewRequest` | both optional fields, `resolve_duration`; the old `1..24` validators removed (the catalogue rules) |
| `schemas/booking.py` `BookingPreviewResponse`, the two booking response models | both fields returned (`Hours = Union[int, float]`) |
| `schemas/caregiver_app.py` (home visit, offer), `schemas/admin_booking.py` (detail) | both fields |
| `api/v1/endpoints/booking.py` | `enrich_booking_with_details` adds `duration_hours` (it copies `__dict__`, which no longer has it); `422 invalid_duration` mapping; docs |
| `services/booking.py` preview | catalogue length check; price by minutes; response fields |
| `services/booking.py` `insert_booking` | length check (non-locked path), price by minutes, end time = start + minutes, stores minutes |
| `services/earnings.py` `is_short_visit` | booked minutes |
| `services/caregiver_home.py` (visit, offers), `services/admin_bookings.py` (detail) | both fields |
| `channels/whatsapp/booking_chat.py` (5 uses: price, when, base line, insert, kept payload) | unchanged: whole hours, converted by `BookingCreate`; `price_service` gained a `minutes` option |
| `channels/whatsapp/flows/service.py` (details screen and answer) | hours as before; options and check follow the catalogue |
| Checked, no change needed | earnings share (80 % of the service fee, not of time), cancellation fees (percent of the service fee), the arrival window (`scheduled_date`), visit end time (`scheduled_end_date`, now set from minutes), notifications and WhatsApp notices (no duration text), `voice_notes/access` (`scheduled_end_date`), care-plan `MAX_VISIT_SPAN` (independent), dispatch and location (service type only); there are no calendars, reports or CSV exports that read it |
| Scripts | `capture_caregiver_flow.py` and `capture_admin_flow.py` seed bookings with raw SQL: now `duration_minutes` (120) |

## What's involved
`app/alembic/versions/0027_booking_minutes.py`; the files above; `app/services/pricing.py` (`compute_price(minutes=...)`, `price_service(minutes=...)`); `app/services/service_catalog.py` (`check_duration`, `InvalidDuration`, `get_row`); `app/schemas/service_catalog.py` (relaxed validation); tests `test_booking_minutes.py` (45), `test_booking_minutes_migration.py`, `test_service_catalogue.py` (updated), `golden/response_shapes.json` (two additive `duration_minutes` keys), `factories.make_booking` (`duration_minutes` override drops the default hours); `scripts/capture_booking_minutes.py`; FE guides (patient section 13, admin booking detail note).

## Verification
| Check | Result |
|---|---|
| Migration `0027` | `test_booking_minutes_migration.py` (scratch DB): backfill (4 h -> 240, 1 h -> 60), column dropped, `> 0` check, catalogue 30/30, downgrade restores hours (a 90-minute booking becomes 2 h, rounded up) and the old catalogue, upgrade again; also `0026` seed and downgrade. Round trip also run by hand with `alembic` |
| Properties (seeded random, 300 to 600 cases each, hourly prices from 1 kobo to 1,000,000.00, multipliers 0.01 to 10, surge, discount, platform 0 to 100 %) | price and total are non-decreasing in minutes (every 15 minutes up to 24 h); caregiver share (0/50/80/100 %) + platform fee <= total; a 30-minute price is at least 1 kobo and never above the hour's; the minutes path equals the hours path for whole hours |
| Endpoints | minutes, hours, 1.5, both, neither, conflicting, non-whole minutes; every `invalid_duration` case on preview and create (nothing created, Paystack not called); the admin changing the rule takes effect; create stores minutes and `end = start + minutes`, Paystack gets the previewed total |
| Full suite | the whole backend suite on a scratch Postgres 16 (not just the touched modules, because the `Booking` model is shared): **4,591 passed, 9 skipped, 2 failed** (`test_caregiver_home.py`, two exact-shape assertions that needed the new `duration_minutes` key; fixed, file re-run: 14 passed). CI is the backstop for the final head |
| Review fixes (2026-11-08) | Downgrade of a catalogue row whose min and max share a part-hour (90/90, 45/75, 30/30): one statement builds the new max from the new min, so `ck_service_catalog_minutes` holds (test with a downgrade, re-upgrade). The Flow hides a service with no whole-hour length (`FlowService._hours_for`, `_services`) and refuses it on submit. JSON `true`/`false` for `duration_minutes` / `duration_hours` give a field-level `422` list, not 1 minute or 1 hour (`schemas/booking.py` `_refuse_boolean`), on preview and create |
| Live | `scripts/capture_booking_minutes.py` and the re-run of `capture_service_catalogue.py` against a real API; both FE guides rendered from them; `render_fe_guide --check` green |

## Compatibility risks for the apps and the console (read before shipping them)
1. **`duration_hours` can now be a decimal** (`1.5`) in every booking response. An app that types it as an integer (or formats with `%d`) misbehaves for a half-hour booking. Whole hours are still sent as integers. Until the catalogue offers half-hour visits to an app, an app on the old code only ever sees whole hours *if* it only lets people pick whole hours; the **nutritionist (30 minutes) and any 90-minute booking made on WhatsApp-free paths are the first fractional values**.
2. **The `422` for a bad length changed shape**: from FastAPI's list (`Duration must be between 1 and 24 hours`) to an object `{code: "invalid_duration", ...}`. An app that renders `detail[0].msg` shows a blank or `undefined`; read `detail.code` and `detail.allowed_minutes`.
3. **The OpenAPI types change** (`duration_hours` becomes `integer | number`, optional on requests, new `duration_minutes`): the monorepo's "api types drift" check will need a re-sync (`pnpm api:sync`).
4. Apps that keep sending `duration_hours` keep working, but cannot offer a half-hour option until they read `GET /booking/services` (`min/step/max_minutes`) and send `duration_minutes`.
5. Anything outside this repo that reads `bookings.duration_hours` in SQL breaks (none known; the capture scripts were updated).

## Operate / roll back
`alembic downgrade 0026_service_catalog` restores `duration_hours` (rounded up) and the 60-minute catalogue; revert the commit. Bookings of 30 or 90 minutes made after the upgrade are rounded up to whole hours by a downgrade (and priced as they were: the stored price is not recomputed).

## Follow-ups
* WhatsApp half-hour bookings (the Flow and quote copy are hour-based).
* Admin console and both apps: minutes inputs, the `invalid_duration` handling, fractional hours in labels.
* A `422` for a non-finite number still answers `500` (FastAPI cannot serialise `Infinity` into its own error): pre-existing for every float field.
* If the owner wants whole-minute prices rounded to the naira, that is a one-line change to the rounding rule above.
* A locked-price (WhatsApp) booking skips `check_duration` in `services/booking.py` (`insert_booking`, only runs when `locked_price` is None); the Flow checks the length itself with `duration_allowed`. A quote older than an admin change to the catalogue is therefore booked as quoted.
* Field-level `422`s (a boolean, text, both fields disagreeing, a non-whole-minute hours value) are FastAPI's list shape (`detail[].loc/msg/type`), not `detail.code`; only the catalogue's length rule is `detail.code = invalid_duration`. The guide captures 306 to 308 show this.
* A service with a maximum under 60 minutes is bookable in the apps but not offered in WhatsApp (whole hours only).
