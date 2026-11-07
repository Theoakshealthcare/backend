# SOP: the Paystack link is stored at create, so "Pay now" works against the real Paystack (migration 0023)

| | |
|---|---|
| **What shipped** | `bookings.paystack_authorization_url` and `bookings.paystack_access_code` (nullable, migration `0023_booking_payment_link`): `/create` (and the WhatsApp booking flow) keep the link Paystack gave, and `POST /booking/{id}/payment-link` hands that stored link back instead of asking Paystack to initialise the same reference a second time (the real Paystack refuses that: "Duplicate Transaction Reference", so the route used to answer 503). |
| **Branch / commits** | `feature/payment-url-persist`, cut from `oakly/main` `f3b0a7c` (backend #44 and #42 merged). Commits: see `git log oakly/main..feature/payment-url-persist`. Not pushed; the lead opens the PR. Follows up [2026-11-07-patient-payment-link.md](2026-11-07-patient-payment-link.md). |
| **Migration** | `0023_booking_payment_link` (down: `0022_password_reset_code`). Two nullable `String` columns on `bookings`; downgrade drops them. |
| **FE guide** | [`docs/fe-integration-guide-patient-bookings.md`](../fe-integration-guide-patient-bookings.md) section 10 re-rendered from captures 065 onward in [`docs/fe-captures/patient/`](../fe-captures/patient/) (`scripts/capture_patient_flow.py --only-payment-link`). |

## Why
Paystack accepts a reference for initialisation once. `/create` initialised the booking's reference, so the later `payment-link` call (the "Pay now" button after an app restart) asked Paystack to initialise it again and got a refusal, which the route turned into `503 payment_link_unavailable`. The merged route was correct everywhere else (refusals, paid check, advisory lock) but could not work for any booking `/create` had initialised.

## How
| Decision | Why / alternative rejected |
|---|---|
| **Keep the link Paystack returned**, on the booking, in `BookingService.store_payment_link`, called from `start_payment` after a successful initialise. | One reference per booking stays true, so webhook, verify-payment, the expiry sweep and refunds (all keyed on the reference) did not change. Rejected: a fresh reference per link (needs webhook matching by booking code, refunds that know which reference was paid, and re-opens a double-charge window because the old link stays payable). |
| **Columns named `paystack_*`, not `payment_url`.** | `enrich_booking_with_details` builds every booking view from `booking.__dict__`, and the view models already have `payment_url` / `payment_access_code` fields: same-named columns would have shown the link in booking detail, lists and **caregiver** views. The two names are also popped in `enrich_booking_with_details` (belt and braces), and a test proves they appear in no view. |
| **The route order is unchanged**: refuse unpayable, advisory lock, **Paystack verify first (settle a late-webhook payment)**, window check, and only then return the stored link. | The paid check still runs on every request, so a paid booking never gets a link; an expired window never gets one; Paystack unreachable for the paid check issues nothing. |
| **A booking with no stored link** (created before 0023, or whose save failed) tries to initialise as before; success is stored, failure is `503 payment_link_unavailable` and the booking is untouched (never cancelled). | Old pending bookings have a 30-minute window (`PAYMENT_LINK_TTL_MINUTES`), so they drain on their own after the deploy; no backfill is possible (the link was never saved). A distinct error code was not added: the app's handling of the 503 is unchanged and the window error follows. |
| **Saving the link never costs the person the link in hand**: `store_payment_link` commits in a try/except; on failure it rolls back, logs, and `start_payment` still returns the URL. | The booking is then a "no stored link" one. |
| `store_payment_link` commits. | In the route the commit releases the advisory lock a moment before `issue` returns, which is after Paystack was called, so the single-flight rule still holds; in `start_payment` the booking is already committed by `insert_booking`. |

## What's involved
| Path | Change |
|---|---|
| `app/alembic/versions/0023_booking_payment_link.py` (new) | the migration |
| `app/db/models/booking.py` | the two columns |
| `app/services/booking.py` | `store_payment_link`; `start_payment` calls it |
| `app/services/booking_payment_link.py` | returns the stored link; falls back to initialise-and-store |
| `app/api/v1/endpoints/booking.py` | `enrich_booking_with_details` drops the two columns; route docstring |
| `scripts/ci_smoke.py` | Postgres-less check: Alembic has exactly one head (CI's smoke step) |
| `scripts/capture_patient_flow.py` | `--only-payment-link` (re-record section 5 only, captures 001-064 untouched), duplicate-refusing fake Paystack, new captures |
| `app/tests/test_booking_payment_link_stored.py` (new), `..._migration.py` (new), `test_booking_payment_link.py` (race test updated) | tests |

## Verification
- **Tests** (`test_booking_payment_link_stored.py`, with the shared fake set to refuse a repeated reference like the real Paystack): create stores the link and the route returns the same one with **Paystack initialised once**; repeat and double tap write nothing; the WhatsApp `start_payment` stores it; a failed initialise at create stores nothing and cancels as before; a failure to save never loses the link in hand; a late-webhook payment is settled and refused even with a stored link; an amount mismatch is held for review; Paystack down for the paid check issues no link; expired, paid and cancelled bookings never get their stored link; another patient is refused; a booking with no stored link: Paystack refusing is a 503 and untouched, Paystack healthy stores the link; the stored link is in no patient detail, list, enriched dict, or assigned caregiver's detail/list. The existing 5-way race test now asserts one initialise and one link for all winners.
- **Migration test** (`test_booking_payment_link_migration.py`): follows 0022, one head, nullable columns, upgrade/downgrade/upgrade round trip. **`ci_smoke`** asserts one Alembic head with no database.
- **Live**: 27 captures from `scripts/capture_patient_flow.py --only-payment-link` against a running API (local Postgres 16, local fake Paystack that refuses duplicate references): create then link return the same link; the database holds it and Paystack was asked once; detail shows `payment_url: null`; the no-stored-link states are SEEDED rows (labelled), not a real pre-0023 migration. Late webhook, outage and refusal are simulated.
- **Not verified**: anything against the real Paystack (that a stored checkout link keeps opening for the whole 30-minute window; its real duplicate-reference answer); a phone.
- **Opus review**: one read-only round, **no major finding**; two minors fixed (a link is stored only when both URL and code are present; a test for a failed save on the route's fallback path) and the create/link race recorded under Follow-ups. No re-review (nothing major).
- **Local CI**: `poetry check --lock`, `ruff --select E9,F63,F7,F82`, `scripts.ci_smoke` with CI's dummy env, both `render_fe_guide --check` pairs (and the patient bookings guide), full pytest.

## Operate / roll back
`alembic upgrade head` adds the columns (instant; nullable). Roll back: `alembic downgrade 0022_password_reset_code` drops them; a pending booking then has no stored link, and the route behaves as before the change (503 against the real Paystack). Log lines to look for: `could not store the Paystack payment link` (the save failed; the person still got the link) and `Paystack would not initialise a payment link`.

## Follow-ups
- **Create/link race (Opus minor, not fixed):** `/create` commits the booking before Paystack's answer is stored, so a `/payment-link` call in that gap (double submit, restart mid-create) finds no stored link and tries to initialise: against the real Paystack a transient 503 that succeeds on retry; same reference, so never a double charge. Fix if it shows up: answer `payment_link_in_progress` for a no-link booking created in the last few seconds, or take the advisory lock in `start_payment`.
- Prove a stored link against Paystack test mode (open it after several minutes; reopen it after a failed card).
- Re-point the patient "Pay now" screen to the route (apps/patient), and drop any in-memory copy of the `/create` link.
- The authorization URL is a bearer link to the checkout page of one booking: it is stored in clear text and handed only to the booker; consider clearing both columns when the booking is paid or cancelled (data minimisation), not done here.
