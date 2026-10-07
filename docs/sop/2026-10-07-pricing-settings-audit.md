# SOP: Pricing settings: versioned writes, reasons, audit, validation, admin-only reads

| | |
|---|---|
| **Date** | 2026-10-07 |
| **Branch** | `fix/booking-settings-audit` (base `main` at `8226ea1`) |
| **Migration** | None (rebased onto `main` at `dff3f5f`, head `0010_dropout_fee_tiers`) |
| **Status** | Verified locally: full suite green, CI steps reproduced, admin FE guide re-captured on the Docker stack (compose project `oakly-settings`) |

## What shipped
The `/api/v1/booking-settings` routes now publish a new pricing version on every write, require a reason, write an audit row, validate every value, and need an admin token to read.

## Why
Found in a security review of the pricing routes:
- `POST`/`PUT`/`DELETE` took no reason and wrote no audit row, so a price change had no owner or explanation.
- Values were unchecked: a negative `base_fee` or a `platform_fee_percent` above 100 was accepted.
- `PUT` edited the stored row in place. WhatsApp quotes (`whatsapp_booking.settings_id`) point at that row, so a quote already shown to a person silently changed price. `DELETE` removed it outright (or failed on the foreign key).
- Both `GET`s had no auth (low risk: they only expose prices).

## How
| Decision | Choice | Alternative rejected |
|---|---|---|
| Edits | `POST` (only while no version exists; otherwise `409`, "use PUT on the latest") and `PUT /{id}` both **insert a new `booking_settings` row** and leave old rows untouched. Pricing already reads the newest row (`created_at` desc), so the new row is live at once | Edit in place plus a history table: two sources of truth, and quotes would still move |
| `PUT /{id}` | `{id}` must be the latest version, else `409` (a stale screen cannot overwrite a newer price). Unknown id is `404` | Last write wins |
| Ordering | While holding the lock, `created_at = max(now, latest.created_at + 1 microsecond)`, so a slow app-server clock can never publish an older "latest". Every reader (`booking.py` x2, `booking_chat.py`, `flows/service.py`, `service.latest`, the list route) also sorts by `id desc` as a tie-break for legacy rows with equal timestamps | The DB clock (`clock_timestamp()`): same effect, but `created_at` elsewhere is app-clock |
| Concurrency | `pg_advisory_xact_lock` around "read latest, check, insert" so two admins cannot both publish from the same base (test with real threads: one `200`, one `409`) | `SELECT ... FOR UPDATE` on the latest row: the lock does not stop the second writer from inserting a sibling |
| `DELETE` | **Always refused with `409`.** Rows are the history that quotes point at. To go back, publish the old values again | Soft deactivate: needs a new `is_active` column and migration (which would have to wait for `0010_dropout_fee_tiers`), and adds "no active settings" states that pricing does not handle. Refusing is the safest and needs no migration |
| Reason and audit | Same pattern as `/admin/booking-policy` (reason required, not blank, max 2000; `AuditService.record` in the same transaction; action named `<entity>.<verb>`). Differences kept on purpose: `pricing.write` is the existing permission, and the body is a full version rather than a patch. Detail: `reason` required in the body (not blank, max 2000). One `admin_audit_log` row per publish through `AuditService.record`, in the same transaction: `booking_settings.create` / `booking_settings.update`, `entity_type=booking_settings`, `entity_id` = the new row, `before` = the version that was live (null for the first), `after` = the new one, values in naira | |
| Validation | One strict Pydantic model (`extra="forbid"`): `base_fee` 0 < x <= 1,000,000; `platform_fee_percent` 0 to 100; `discount` 0 to 100; `surge_multiplier` 1 to 5; multipliers keyed by the five known service types (`SERVICE_TYPES`), each above 0 and at most `pricing.MAX_MULTIPLIER` (10, the cap `service_multiplier` already applies); NaN and infinity refused; `base_fee` at least 0.01 with at most 2 decimals (0.001 used to round to 0 kobo); strict types, so `"5000"`, `true` as a number and `1` as a bool are `422` (JSON ints and floats both fine) | |
| Reads | `GET /` and `GET /latest` need `require_permission("bookings.read")`. Grep of `backend` and `caregiver` (including `apps/` and generated `packages/api-client`) found no caller of either route; the app and the WhatsApp flow read the table directly. So no public price-preview route was added | A public preview route (not needed today; add one when a client needs it) |
| Response shape | Unchanged: `id, base_fee (naira), platform_fee_percent, multipliers, surge_pricing, surge_multiplier, discount, created_at, updated_at`. Request body gains `reason` | |

Pricing consumers were checked and still read "latest": `app/services/booking.py` (preview and create), `app/channels/whatsapp/booking_chat.py`, `app/channels/whatsapp/flows/service.py`, all `order_by(created_at.desc()).first()`. `service_multiplier` is untouched.

## What's involved
| File | Change |
|---|---|
| `app/api/v1/endpoints/booking_settings.py` | Admin-only reads; `POST`/`PUT` call the service; `DELETE` refuses |
| `app/services/booking_settings.py` (new) | `publish()`: lock, stale check, insert, audit, commit; `latest()`, `snapshot()` |
| `app/schemas/booking_settings.py` | `BookingSettingsWrite` (strict) and `BookingSettingsResponse` |
| `app/tests/test_booking_settings_pricing.py` (new) | about 60 cases: versioning, stale `PUT`, delete refused, concurrency, reason, audit, validation, reads |
| `app/tests/test_admin_rbac.py` | The two `GET`s added to the permission matrix (`bookings.read`) |
| `app/tests/test_money_in_kobo.py` | The naira round-trip test now signs in as an admin and sends a reason |
| `scripts/capture_admin_flow.py` | New `pricing_ops` section and `--section pricing` |
| `docs/fe-integration-guide-admin.md`, `docs/fe-captures/admin/132..149` | Section 13 (pricing), verification table rows |

## Verification
- New tests written first and run against the old routes: 38 failed (the validation, reason, auth and matrix cases); after the change they pass.
- Full suite and every CI step (`poetry check --lock`, ruff bug-class lint, `scripts.ci_smoke`, both FE guide `--check`s) run locally with the CI env values.
- Live capture against the Docker stack: `132` to `149`.

## Operate / roll back
No migration and no config. Roll back by reverting the commit. Rows written by the new code are ordinary `booking_settings` rows, so the old code reads them fine. Existing rows are untouched.

## Follow-ups
- Existing rows may hold multiplier keys outside the five service types or values outside the new bounds; they keep working (`service_multiplier` falls back to the default) but a `PUT` must replace them with valid values.
- A prescription-settings route (`/prescription/settings`) has the same in-place edit and no audit; not touched here.
