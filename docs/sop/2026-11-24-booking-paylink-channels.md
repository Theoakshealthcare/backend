# SOP: the patient can choose Card or Bank Transfer on Paystack's page (optional `channels`)

| | |
|---|---|
| **What shipped** | An optional `channels` list (`card`, `bank_transfer`; one or two values) on `POST /booking/create`, passed to Paystack's `transaction/initialize` `channels` parameter only when given. `POST /booking/{id}/payment-link` accepts the same optional body, used only when it has to make a new link. Absent = today's behaviour, byte for byte. |
| **Branch / PR** | `feat/booking-paylink-channels`, cut from `oakly/develop` `17c478b`. PR: see the branch on `Theoakshealthcare/backend`. **Money path: needs an Opus review before merge; not merged.** |
| **Migration** | None. |
| **FE guide** | [`docs/fe-integration-guide-patient-bookings.md`](../fe-integration-guide-patient-bookings.md) section 14. **Not captured live** (labelled in the guide): the 422 bodies were printed from an in-process test run. |

## Why
The patient app's "Payment Details" screen offers Card or Bank Transfer, but nothing in the booking flow could carry the choice to Paystack, so the screen was left unbuilt (patient parity slice 2, `caregiver/docs/sop/2026-11-23-patient-booking-parity.md`). Paystack's initialise call has a `channels` array for exactly this.

## How
| Decision | Why / alternative rejected |
|---|---|
| **The decisive field is on `/create`, not on `/payment-link`.** | `/create` is the only place a link is first made; migration 0023 then stores it and `/payment-link` hands the stored link back (the real Paystack refuses to initialise a reference twice). A `channels` that existed only on `/payment-link` would never reach Paystack for any booking made in the app. It is also on `/payment-link`, for the one case where that route initialises (a booking with no stored link: made before 0023, or whose copy failed to save). |
| **Idempotency: the first link wins.** A repeat request for the same booking, naming different or no channels, returns the stored link and asks Paystack for nothing new (still one initialise, one reference, nothing written). | Paystack cannot change the channels of an initialised transaction and refuses the same reference again. Rejected: a fresh reference per choice (webhook, refunds and the sweep key on the one reference; it reopens the double-charge window the 2026-11-07 SOPs closed) and storing the choice (a migration, for a field the app cannot change anyway). The cost: after "Pay now" the app cannot learn which methods the stored link has; the guide tells the app to treat the choice as fixed once the booking exists. |
| **Strict allow-list, enforced twice.** `PaymentChannels` in `app/schemas/booking.py`: 1 or 2 values, each exactly `card` or `bank_transfer`, no repeats; anything else is a 422 before any work (no booking row, no Paystack call). `PaystackService` checks again (`_check_channels`, `ValueError`), so no other caller can send a free-form value. | Money code: the schema is the contract, the service is the choke point. |
| **Payload unchanged when absent.** `channels` is appended last, only when truthy; `BookingService.initialise_payment` passes the keyword only when given, so older call sites and test stubs without the parameter keep working. | Pinned by a test comparing the exact JSON string of the real service's request. |
| **Nothing money-shaped comes from the client.** Amount, currency, reference, callback URL and metadata are built server-side as before. `/payment-link`'s new body model is `extra="forbid"`: an `amount` or card field is a 422, not silently dropped. `/create` still ignores unknown fields as it always did (changing that would break clients). | |
| **The `/payment-link` body stays optional** (no body, `{}`, `null` channels and an empty body labelled JSON all work). | Every existing client sends no body. |

## What's involved
| Path | Change |
|---|---|
| `app/services/paystack.py` | `CheckoutChannel`, `ALLOWED_CHECKOUT_CHANNELS`, `_check_channels`; `initialize_transaction(..., channels=None)` adds the key last, only when given |
| `app/schemas/booking.py` | `PaymentChannels` type, `BookingCreate.channels`, `PaymentLinkRequest` (`extra="forbid"`) |
| `app/services/booking.py` | `create_booking`, `start_payment`, `initialise_payment` carry `channels` (forwarded only when given) |
| `app/services/booking_payment_link.py` | `issue(..., channels=None)`; used only on the no-stored-link branch |
| `app/api/v1/endpoints/booking.py` | optional body on `/payment-link`; docs on both routes |
| `app/tests/fake_meta.py` | `FakePaystack.initialize_transaction(..., channels=None)`; the key is in the recorded payload only when given |
| `app/tests/test_booking_payment_channels.py` (new, 52 tests) | see below |
| `docs/fe-integration-guide-patient-bookings.md` section 14, `docs/checklist/MASTER_CHECKLIST.md` | docs |

## Verification
- **New tests (52, pass):** one of them goes endpoint -> real `BookingService` -> real `PaystackService` -> the JSON sent (only `requests.post` stubbed), default and with `channels`; the real `PaystackService` against a stubbed `requests.post`: no channels gives exactly `{"email", "amount", "reference", "currency", "callback_url", "metadata"}` as the same JSON string as before; `channels=None` equals leaving it out; with channels only that key is added; nine bad values (unknown, wrong case, empty, duplicate, three, non-string) raise and never call Paystack. Booking flow with the fake Paystack: no `channels` key without it (also with explicit `null`); each of four valid choices is forwarded in the order given with amount, reference and metadata equal to the booking's; an `amount`, `currency`, `reference`, `authorization_code` or card number in the body changes nothing and reaches Paystack nowhere; thirteen bad values are 422 with no booking row and no Paystack call; `/payment-link` naming other channels returns the first link (one initialise, channels of the first); a booking made without channels is not "upgraded"; four ways of sending no body work; bad channels or any extra field are 422 before Paystack is asked anything; a no-stored-link booking is initialised with the channels asked for and then keeps that link; WhatsApp-style `start_payment` sends none.
- **Mutation check:** dropping the forwarding line in `booking.py` fails 7 of the first 51.
- **Neighbour files run (bounded, by name):** `test_booking_payment_link.py`, `test_booking_payment_link_stored.py`, `test_money_kobo_and_config.py`, `test_money_in_kobo.py`, `test_booking_expiry.py`, `test_whatsapp_phone_only_patient.py` (99 passed) and `test_service_catalogue.py` (60 passed; goes through `/create`).
- **Fast checks:** `ruff check --select E9,F63,F7,F82 .`, `scripts.ci_smoke` with CI's dummy env (217 operations, one Alembic head), all eight `render_fe_guide --check` pairs. The full suite is CI's.
- **Not verified:** anything against the real Paystack (what its checkout shows for each choice; that `bank_transfer` is enabled on Oakly's integration); a phone; a live-harness capture of the new shapes.

## Operate / roll back
No configuration or migration. Revert the commit: `channels` disappears from the schema (a client that still sends it on `/create` is ignored, as unknown fields are; on `/payment-link` it becomes a harmless unused body). Bookings already created keep the links they have.

## Follow-ups
- **Opus review** of the money path: `app/services/paystack.py` (the payload and `_check_channels`), `app/services/booking.py` (`initialise_payment`, `start_payment`), `app/services/booking_payment_link.py` (`issue`/`_issue`), the `/payment-link` route.
- **Owner, Paystack dashboard in test mode (unverified, blocks single-channel choices):** open a `["bank_transfer"]` and a `["card"]` link; confirm each channel is enabled on the integration. If a single channel is not enabled Paystack may accept the initialise and show a page with no usable method, and because the first link wins the patient is stuck until the 30-minute window ends. Until then the app sends both channels or none (FE guide 14.2).
- Re-record the 422 captures with `scripts/capture_patient_flow.py` so section 14 is rendered from live files.
- Re-sync `packages/api-client` in the monorepo (a later frontend PR) and build the patient Payment Details screen (Card / Bank Transfer only; no wallet).
- Not done on purpose: "change payment method on an existing unpaid booking" (needs a new reference or a Paystack-side feature; a booking made again is the path today).
