# SOP: Money in kobo (WP-E2)

| | |
|---|---|
| **Date** | 2026-10-05 |
| **Branch** | `foundations/kobo` (base `main` at `e4aca4f`) |
| **Migration** | `68b8941cfc28` (down_revision `6fb30639d5f4`) |
| **Status** | Verified locally (full suite green, migration round-trip on seeded rows) |

## What shipped
Every money column now stores **integer kobo (BIGINT)** under a `*_kobo` name. Pricing, settlement, refunds and the patient wallet compute and compare integers. The API still speaks **naira with the same field names**, so mobile clients are unaffected.

## Why
Money was a mix of `Float` (bookings, settings, prescriptions) and `Numeric(10,2)` (wallet). Float arithmetic drifts (`0.1 + 0.2`), and WP-D had to patch it with 2dp rounding plus a `to_kobo` comparison at every boundary. One representation end to end removes the whole class of "what we showed, what we charged and what we compared disagree by a kobo" bugs.

## How
| Decision | Choice | Alternative rejected |
|---|---|---|
| Storage | `BIGINT` kobo, column renamed `*_kobo` | Keeping `Numeric(10,2)` naira: still needs a conversion at every Paystack call, and a plain rename would not have caught missed call sites (they fail loudly now) |
| Pricing | New `app/services/pricing.py::compute_price`: Decimal arithmetic, each amount rounded half-up to kobo, **`total = service_fee + platform_fee` exactly**. Preview, `create_booking` and prescriptions all call it | Three copies of float maths (what existed): preview and create could disagree |
| Rounding change | Previously `total = round(service + platform)` on unrounded values; now the sum of two rounded kobo amounts. Can differ from the old figure by 1 kobo | Rounding the total separately: the stored parts would not add up to the charged total |
| Settings (`base_fee`) | Stored as `base_fee_kobo`. The admin API still reads and writes naira `base_fee` through a property + setter on the model (setter uses `to_kobo`) | Changing the admin schemas: breaks the admin tooling |
| Response compatibility | Read-only naira properties on `Booking` (`total_amount`, `service_fee`, `platform_fee`), `Prescription` (`total_cost`), settings (`base_fee`), built on `kobo_to_naira`. Wallet and transaction responses convert in `PatientWalletService`. No setters on the money models | Duplicating response schemas |
| `kobo_to_naira` | `app/core/money.py`, returns an exact 2dp `Decimal`. For display only; never fed back into stored arithmetic | Float division |
| Paystack service | `initialize_transaction(email, amount_kobo, ...)` and `initiate_refund(ref, amount_kobo)` take kobo and send it as-is. `verify_transaction` and `parse_webhook_event` return `amount_kobo` (the raw integer) instead of a naira float | Keeping the naira float and re-converting: float in a money path |
| Webhook amount | Settlement and prescription/deposit checks compare `amount_kobo` ints directly. A non-integer reported amount counts as a mismatch | Coercing strings/floats |
| Input validation | Wallet deposit, payment and refund amounts must round to at least 1 kobo (a naira amount of `0.004` would otherwise store 0 kobo) | Allowing sub-kobo amounts |
| Migration | `ALTER COLUMN ... TYPE BIGINT USING ROUND(col::numeric * 100)::bigint`, then rename. `::numeric` first matters for Float columns: Postgres casts a double to numeric at 15 significant digits, so `1531.885` becomes 153189, not the binary `153188.4999...` -> 153188. One transaction. Server defaults are dropped (a numeric default cannot be cast to bigint); the app always supplies values | Per-column `to_kobo` in Python: slow and not transactional |
| Downgrade | Rename back, `USING (col / 100.0)::<original type>` (Float, or `numeric(10,2)` for wallet, transaction and refund-request amounts) | |

## What's involved
- **Migration:** `app/alembic/versions/68b8941cfc28_money_in_kobo.py`. Columns: `bookings.{total_amount,service_fee,platform_fee}`, `booking_settings.base_fee`, `patient_wallets.{balance,total_deposited,total_spent,total_refunded}`, `patient_transactions.amount`, `patient_refund_requests.amount`, `prescriptions.total_cost`, `prescription_settings.base_fee` (all gain `_kobo`).
- **Models:** `app/db/models/{booking,booking_settings,patient,prescription,prescription_settings}.py`.
- **Money core:** `app/core/money.py` (`kobo_to_naira`), `app/services/pricing.py` (new).
- **Services:** `booking.py` (preview, `create_booking`), `booking_payment.py` (`expected_kobo` = stored kobo; the `naira_to_kobo` alias is gone), `patient_wallet.py` (deposit, payment, refund request/approval, compensation, webhook match, summary), `prescription.py`, `paystack.py`.
- **Endpoints:** `endpoints/payment.py` (webhook compares `amount_kobo`), `endpoints/booking.py` (`enrich_booking_with_details` adds the naira fields, because it builds the response from `__dict__`; verify-payment), `endpoints/patient.py` (admin refund list), `schemas/patient.py` (>= 1 kobo validators).
- **Not touched:** caregiver models, services and endpoints. The caregiver wallet was already integer kobo (`balance`, `amount`) and keeps its names.
- **API contract:** no field renamed or removed. Verified by tests that read the JSON back (`test_money_in_kobo.py`). Wallet `Decimal` fields keep serialising exactly as before (Pydantic v2 `Decimal`); float-typed booking fields stay numbers with 2dp.
- No new endpoints, so no new FE integration guide is needed; the existing wire format is unchanged.

## Verification
- **Baseline:** 202 tests green on `e4aca4f`. **After:** 228 green (`pytest -q -p no:warnings`), `ruff --select E9,F63,F7,F82`, `ci_smoke` and `poetry check --lock` clean.
- `app/tests/test_money_in_kobo.py` (written first, failed on import before the change):
  - fractional price (base 1234.57, x1.3, 3.3% discount, 7.5% platform, 3 hours): preview kobo == stored kobo == Paystack init amount == webhook settlement amount, and the booking flips to PAID;
  - wallet deposit -> webhook credit -> payment -> refund request -> admin approval, balances asserted in kobo and in the naira JSON; a deposit one kobo short is not credited;
  - the refund Paystack call receives integer kobo;
  - booking, booking-settings and prescription responses still return naira with the old field names;
  - all 12 columns are `bigint`; `compute_price` invariants and half-up rounding.
- Existing money suites (webhook, verify-payment, refunds, refund flow, stuck refunds, wallet verify, prescriptions access) were migrated to kobo and still pass unchanged in intent.
- **Migration, automated:** `app/tests/test_migration_money_in_kobo.py` (marker `migration`, ~7s). Builds a scratch database from the models, stamps it, `downgrade -1` to get the old column types, seeds fractional rows, then `upgrade head` -> asserts exact kobo -> models vs DB diff is empty -> `downgrade -1` -> asserts naira restored -> `upgrade head`. Also asserts a single Alembic head.
- **Migration, manual, on a database built by `main`** (the procedure the plan asks for). `postgres:16-alpine` container; code at `e4aca4f`: `ENVIRONMENT=development python -m scripts.init_local_db` (stamped `6fb30639d5f4`); seeded fractional rows; then on this branch:

  | Step | bookings (total, service, platform) | settings base | wallet (bal, dep, spent, refunded) | tx | refund req | prescription costs | rx settings |
  |---|---|---|---|---|---|---|---|
  | before (naira) | 1531.885, 0.30000000000000004, 44000 | 1234.567 | 1234.56, 99999999.99, 0.01, 0.00 | 5000.50 | 0.05 | 2500.499, NULL | 5000 |
  | `alembic upgrade head` | 153189, 30, 4400000 | 123457 | 123456, 9999999999, 1, 0 | 500050 | 5 | 250050, NULL | 500000 |
  | `alembic downgrade -1` | 1531.89, 0.3, 44000 | 1234.57 | 1234.56, 99999999.99, 0.01, 0.00 | 5000.50 | 0.05 | 2500.5, NULL | 5000 |
  | `alembic upgrade head` | identical to the first upgrade | | | | | | |

  `alembic heads` -> `68b8941cfc28 (head)`. Downgrade is lossy only for sub-kobo precision that the old floats carried (e.g. 1531.885 returns as 1531.89), which is the intent.

## Operate / roll back
- **Deploy order:** the migration and the new code must go together (old code reads `total_amount`, which no longer exists). Run `alembic upgrade head` in the release step, in a quiet window: each `ALTER TYPE` takes an `ACCESS EXCLUSIVE` lock and rewrites the table (small tables, seconds).
- **Roll back:** `alembic downgrade -1`, then deploy the previous image. Any booking or wallet write made between upgrade and downgrade is converted back at 2dp.
- **Before running against production:** the downgrade types assume the production columns match the models (`Float` / `Numeric(10,2)`). Confirm with `\d bookings` and `\d patient_wallets` first, because the migration history can't rebuild an empty DB (WP-E1).
- Config: none.

## Follow-ups
- Add `CHECK (balance_kobo >= 0)` (and similar) once production data is confirmed clean.
- `booking_settings` / `prescription_settings` percentages and multipliers are still `Float` (they are ratios, not money). Move to `Numeric` if admin needs exact 7.5% style values to round-trip.
- The admin settings API and booking preview responses still expose naira as JSON floats. If exact decimal strings are ever needed on the wire, that is a mobile contract change.
- Caregiver wallet keeps `balance`/`amount` (already kobo); renaming to `*_kobo` for consistency belongs with E5 or later.
- The Paystack webhook and verify payload shapes are unchanged from WP-D (still not a live capture).
