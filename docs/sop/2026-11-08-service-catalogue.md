# SOP: the service catalogue (phase 2, slice 1): which services are on offer, with no prices

| | |
|---|---|
| **What shipped** | A `service_catalog` table (migration `0026`) with one row per service type; `GET /booking/services` (the bookable list, no prices); preview and create refuse a service that is not bookable (`422 service_unavailable`); admin `GET /admin/service-catalog` and idempotent `PUT /admin/service-catalog/{service_type}` (audited, refuses to enable an unpriced service with `422 service_has_no_price`); the WhatsApp Flow offers only bookable services. **No change to pricing math or durations.** |
| **Superseded in part by** | [booking minutes](2026-11-08-booking-minutes.md) (migration `0027`, stacked): the catalogue's step becomes 30 and its validation allows steps 15/30/60 and multiples of the step; the nutritionist's minimum becomes 30. The "whole hours only" rows below describe this slice on its own. |
| **Branch / commit** | `feature/service-catalogue` (first of two stacked PRs; the second is `feature/booking-minutes`, migration `0027`). Not pushed; the lead pushes after review. |
| **Owner decisions (2026-11-08)** | Phase 2 is built with prices and surge UNKNOWN: everything configurable in the admin console, a service with no price stays hidden, we never invent prices; every service is a home visit (video deferred, no provider chosen); the system has no real data, so the seed is applied by the migration. |

## Why
Phase 2 adds services (doctor, physiotherapy, caregiver, nutrition) whose prices the owner has not set. Until now every type in `SERVICE_TYPES` was sellable the moment someone called preview: an unconfigured type was silently priced at a multiplier of 1.0 (the app, and the WhatsApp Flow offered all five). That would put an invented price on screen. The catalogue makes "is this service on offer?" an explicit, admin-controlled fact.

## How (decisions)
| Decision | Why / alternative rejected |
|---|---|
| **Bookable = enabled AND priced.** `priced` = the newest `booking_settings.multipliers` holds a usable multiplier (same test `service_multiplier` already used: a real number, above 0, at most 10) | One rule, one place (`services/service_catalog.py`, `pricing.is_priced`). Rejected: copying prices or multipliers into the catalogue (two places to change a price) |
| **Backward compatibility: the nurse counts as priced at 1.0 when no multiplier is set;** every other type needs an explicit multiplier. A nurse with an unusable stored value (e.g. 150) still falls back to 1.0, as `service_multiplier` always did | Today's behaviour, and local flows and tests book the nurse with no multipliers. `UNCONFIGURED_PRICED_SERVICES = ("nurse",)` in `pricing.py` is the one list |
| No `booking_settings` row means nothing is bookable (even the nurse), and preview and create still answer `503 booking_settings_not_configured` first | Matches the WhatsApp Flow, which already offered nothing without pricing |
| `service_multiplier`'s fallback to 1.0 is **unchanged** | No pricing-math change. Preview and create no longer reach it for a hidden service; WhatsApp only offers bookable ones |
| The catalogue stores durations in minutes (min 60, step 60, max 1440) but PR 1 validation only accepts step 60 and whole hours in [60, 1440] | Nothing may contradict today's whole-hour bookings. Seed for the nutritionist is 60 now (30 is a PR 2 value) |
| `mode` is `home`; `video` / `both` exist in the DB check constraint but the API rejects them | Video is deferred (no provider). The constraint avoids a migration later |
| **PUT replaces all seven fields**, takes the row lock (`FOR UPDATE`), and a PUT that changes nothing writes no audit row and does not bump `updated_at` | Retry-safe by construction: the same request twice is the same state. Rejected: PATCH with partial fields (retry semantics murkier) |
| Only switching a service **on** needs a price. Disabling always works; an already-enabled service whose multiplier was later removed can still be edited | Blocking edits on a hidden service would trap the admin. `priced` and `bookable` in the GET show the state |
| Permissions: read `bookings.read` (same as the pricing and policy reads), write `pricing.write` (the permission that guards the multipliers) | Per the brief; RBAC matrix test updated |
| `GET /booking/services` needs a logged-in app user (`get_current_user`) | "Like the other booking routes" (preview is public, the rest need a token); returns no prices, so a token is not a secrecy matter, only consistency |
| `ServiceUnavailable` is raised by `BookingService.calculate_booking_preview` and `insert_booking` **only when pricing is looked up** (no `locked_price`) | A WhatsApp booking carries a price the person was already shown; see follow-ups |
| The WhatsApp Flow keeps its own labels (`SERVICE_LABELS`: "Nurse", "Doctor home visit"), not `display_name` | Nurse-only setups stay byte-identical; wiring the catalogue's names in is a small follow-up |

## What's involved
| Path | Change |
|---|---|
| `app/alembic/versions/0026_service_catalog.py` | creates and seeds the table; downgrade drops it |
| `app/db/models/service_catalog.py` | `ServiceCatalog` (check constraints on type, mode, minutes; `updated_by` FK to `admin_users`, SET NULL) |
| `app/services/service_catalog.py` (new) | `bookable_services`, `get_bookable`, `is_bookable`, `update` (idempotent, audited), the exceptions |
| `app/services/pricing.py` | `is_priced`, `UNCONFIGURED_PRICED_SERVICES`; `_usable_multiplier` shared with `service_multiplier` |
| `app/services/booking.py`, `app/api/v1/endpoints/booking.py` | the refusal in preview and create; `GET /booking/services` |
| `app/api/v1/endpoints/admin_service_catalog.py`, `app/schemas/service_catalog.py`, `app/api/v1/api.py` | admin routes and models |
| `app/channels/whatsapp/flows/service.py`, `booking_rules.py` | the Flow's service list and the screen options come from the catalogue |
| `app/tests/` | `test_service_catalogue.py` (52 tests); `conftest.py` restores the seeded rows after each test's TRUNCATE (like the care-plan templates); `booking_fixtures.make_prices` now also switches on the services it has a multiplier for (`enable_services=False` to opt out); RBAC matrix gets the two routes; two WhatsApp Flow tests rewritten (below) |
| `scripts/capture_service_catalogue.py`, `docs/fe-captures/patient/200-208, 299`, `docs/fe-captures/admin/900-916, 999`, both FE guides | live captures and guide text (admin 13.1, patient 12) |

## Behaviour changes to know about
* **WhatsApp:** the Flow used to offer all five services (a service without a multiplier at 1.0). It now offers the bookable ones, in the catalogue's `sort_order` (nurse, doctor, physio, caregiver, nutrition) instead of the old fixed order (nurse, caregiver, physio, doctor, nutrition). A nurse-only setup is identical. The two tests that pinned "an unconfigured service is offered at the default" were rewritten to "a service with no multiplier is not offered" (`test_whatsapp_flow_endpoint.py`).
* **App:** preview and create for a non-nurse type answer `422 service_unavailable` until an admin prices (Pricing) and enables (Catalogue) it. **Existing app builds that offer a fixed list will show a generic error for those types**: the patient app slice must read `GET /booking/services` (FE guide patient section 12).
* **Fresh environment:** only the nurse is on offer.

## Verification
| Check | Result |
|---|---|
| Migration | `0026` upgraded, seed rows read back (5 rows, nurse enabled, the rest disabled, 60/60/1440), downgraded (table gone), upgraded again; a single Alembic head, on a scratch Postgres 16 |
| New tests | `test_service_catalogue.py`: seed and DB constraints; the `is_priced` rule (13 cases incl. nurse fallback, bool, string, 0, negative, above 10); list (no login, no pricing, nurse-only, priced-but-disabled, enabled-but-unpriced, ordering); preview/create refusals (nothing created, Paystack not called); admin list, enable, no-price `422`, retry (one audit row), validation (16 cases), unknown `404`, role `403`; no price field anywhere; WhatsApp Flow lists. 52 passed |
| Existing suites | the whole `test_whatsapp*`, `test_booking*`, `test_money*`, `test_caregiver_earn*`, `test_caregiver_arrived_earning`, `test_response_shapes`, `test_staging_smoke_script` and `test_admin_rbac` modules: 1,378 tests, the only 5 failures were the WhatsApp tests above (fixture and two rewritten); re-run green |
| Live | `scripts/capture_service_catalogue.py` against a real API on a scratch Postgres + Redis: 26 captures, FE guides rendered from them, `render_fe_guide --check` green for all eight guides |

## Operate / roll back
* To launch a service: Pricing (add its multiplier), then Catalogue (enable). The console should surface `service_has_no_price` as "set its multiplier in Pricing first".
* Roll back: `alembic downgrade 0025_visit_voice_notes` drops the table; revert the commit. Without the table nothing offers or refuses services.
* A pricing edit that drops a multiplier hides that service immediately (no warning at write time).

## Follow-ups
* The admin Pricing write could warn when it removes the multiplier of an enabled service.
* WhatsApp: use `display_name`; re-check bookability when a quote is turned into a booking (a service disabled between quote and booking is still created at the quoted price).
* Admin console screen for the catalogue; patient and caregiver apps to read `/booking/services` (separate slices).
* PR 2 (`feature/booking-minutes`) moves durations to minutes with 30-minute steps and relaxes the step validation here.
