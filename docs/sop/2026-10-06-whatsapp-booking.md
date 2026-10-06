# SOP: WhatsApp booking in chat (WP-W2)

| | |
|---|---|
| **Date** | 2026-10-06 |
| **Branch** | `feat/whatsapp-booking` (base `main` at `186cfd1`, which includes W1) |
| **Migration** | `0005_whatsapp_booking` (down_revision `0004_whatsapp_foundations`; re-parent at merge if another migration lands first) |
| **Plan / spec** | [plan W2](../plans/2026-10-06-whatsapp-channel.md) · [PRD](../prd/PRD-whatsapp-patient-channel.md) §5.3, §8.4–8.6 (WA-FR-13–27), §9, §10, §11.1, §14 |
| **Status** | Verified locally: unit/integration tests and the Docker end-to-end script against a fake Meta and a fake Paystack. Not pushed. **Not run against real Meta**: see "Owner steps" and "What to check against real Meta" below. |

## What shipped
A person who has consented can now book, price, pay for and be told about a home visit entirely inside WhatsApp:
- **Booking Flow** (`app/channels/whatsapp/flows/booking.json`, Flow JSON 6.3, data API 3.0): who is it for (myself, a saved person, someone new), service with the price per hour, date and start time, duration and condition, address and city.
- **Flow data endpoint** `POST /api/v1/whatsapp/flows/data`: signed, RSA-OAEP + AES-128-GCM encrypted, answers `ping`, `INIT`, `data_exchange` and `BACK`.
- **Location request**, **service-area check** with a waitlist, **locked quote**, **Confirm & pay**, **Pay now** link, **reminder at 20 minutes**, **cancellation at 30**, **payment received**, and **verify with Paystack** when the person comes back before the webhook.
- **Template registry** with the five W2 templates and the 24-hour window rule enforced in the one function every send goes through.
- Four new conversation states, a 30-minute idle expiry, one migration, and the extended end-to-end script.

"Book a visit" and "Doctor home visit" open the Flow when `WHATSAPP_BOOKING_FLOW_ID` is set and the contact has an Oakly account; otherwise they hand over to a person exactly as in W1. "My bookings" was a placeholder in W2; W3 made it real ([SOP](2026-10-06-whatsapp-notifications.md)).

## Why
W1 could only take a stranger as far as a consented menu. To launch the closed pilot a patient has to be able to book without an app. Everything had to reuse the existing money code (one price function, one settlement function, the existing expiry and dispatch) rather than fork it, and nothing the person says about their health may land anywhere it should not.

## How: the journey

```
idle --Book--> booking_flow --Flow done--> awaiting_location --pin/SKIP--> quoted --Confirm--> awaiting_payment --paid / expired / cancelled--> idle
                  |  (city not served: waitlist offer, back to idle)       |--Change--> booking_flow     |--Cancel--> idle
```

1. **Book a visit.** The processor creates a *draft* (a `quotes` row, status `draft`), and sends a Flow message with `flow_action = data_exchange` and a signed **flow token** (`flows/token.py`: draft id, expiry, HMAC keyed from `SECRET_KEY`; no personal data in it).
2. **Inside the Flow**, each screen calls the data endpoint. Answers are validated again server-side and merged into the draft. Who the visit is for is re-derived from the contact's own `care_relationships`, never from an id in the request. The condition and special instructions are Fernet-encrypted straight into `quotes.sensitive_encrypted` and are never in any response.
3. **Flow finished.** Meta posts an `nfm_reply` whose `response_json` carries *only* our flow token; the answers never travel through the webhook, ARQ or Redis. The bot replies with a **location request**.
4. **Pin or `SKIP`.** A pin inside Nigeria is stored. No pin (or a pin abroad) means the city centre as a placeholder and `needs_address_check = true` (see decisions).
5. **Quote.** `price_service` (same `compute_price`; a service with no configured multiplier is priced at the default 1.0, see [the follow-up SOP](2026-10-06-default-multipliers-and-geocoding.md)) prices it; the breakdown in kobo, the settings row and `expires_at = now + 30 min` are stored on the quote. The message is itemised in naira with the cancellation line and the buttons Confirm & pay · Change · Cancel.
6. **Confirm & pay.** One transaction, under the conversation and quote locks: the booking is created through `BookingService.insert_booking` with the **locked** price, `channel='whatsapp'`, `booked_by_user_id`, `care_recipient_id`, `payment_expires_at`, the condition encrypted; the quote is linked (`quotes.booking_id`, unique). After the commit, `BookingService.start_payment` asks Paystack (email from `paystack_email_for`) and the bot sends the **Pay now** CTA. If Paystack fails, the booking is cancelled (`payment_init_failed`), the quote goes back to `quoted` and the person is asked to tap Confirm again; nothing unpayable is left behind.
7. **Time passes.** The worker's new `whatsapp_events_job` (every 15 s) queues `payment_link_expiring` when the link has been out 20 minutes and delivers events; the existing `expire_unpaid_bookings` cancels at 30 minutes and records `booking_cancelled_unpaid`; settlement (webhook **or** verify-on-return, the same function) records `payment_received` in the same transaction as the payment.
8. **Told at least once.** `booking_events` is an outbox (unique per booking and event, claimed with a compare-and-set, five attempts). An event is marked sent in its own commit the moment Meta accepts the message (and Meta's message id is kept), so a duplicate is possible only if the worker dies between Meta accepting the send and that commit. Inside the 24-hour window the same words go out as free text with Pay now / Book again; outside, as the approved template.

### Decisions and tradeoffs
| Decision | Choice | Why / alternatives rejected |
|---|---|---|
| Where Flow answers go | Straight to the data endpoint and the draft; the Flow completes with only the token. | Putting answers in the completion `response_json` would route health text through the webhook, ARQ and Redis, against WA-NFR-6. |
| One table for draft and quote | `quotes.status`: `draft -> submitted -> quoted -> confirmed` (also `out_of_area`, `waitlist`, `cancelled`, `expired`). | A separate drafts table doubles the lifecycle code for no benefit. **Differs from PRD §10** (quotes only held the price). |
| Condition storage | Encrypted on the draft, copied encrypted to `bookings.condition_encrypted`; `bookings.condition` stays NULL. The booking API views (`enrich_booking_with_details`) decrypt it for the patient, admin and the *assigned* caregiver only; offers stay redacted. | Encrypting into the existing plain column would show ciphertext to every reader of it. Special instructions are copied to `special_instructions` in plain text, the app's existing convention (flagged below). |
| One booking per quote | Conversation row lock + quote row lock + `quotes.booking_id` UNIQUE. | Any one of them alone has a hole (a second worker, a retried job, a double tap). A two-worker test fails when the locks are removed (checked by mutation). |
| Price lock | The quote's stored kobo breakdown is passed to the booking; settings are never re-read after the quote. | Recomputing at confirm time is exactly what WA-FR-20 forbids. |
| ~~Strict multipliers~~ (superseded 2026-10-06) | Originally `explicit_multiplier` refused an unpriced service. The owner decided on a default of 1.0 in both channels; see [default multipliers and geocoding](2026-10-06-default-multipliers-and-geocoding.md). | WA-FR-21 is now "flagged", not "refused". |
| Paystack | `BookingService.create_booking` was split into `insert_booking` (optionally flush-only) and `start_payment`; `create_booking` calls both and is unchanged for the app. Paystack's base URL became `PAYSTACK_BASE_URL` (default the real one). | Reuse, not a second booking path. The URL setting exists so the e2e can point the API at a fake. |
| Slow work off the lock | Paystack init and verify run *after* the conversation commit (`Result.after_commit`). | A held row lock across an HTTP call to a third party would stall every message from that person. Crash window: a booking committed but its link never made is cancelled by the 30-minute expiry, and a stuck "preparing your link" is repaired after 2 minutes. |
| No pin | City-centre placeholder, `needs_address_check`, and `settle_booking_payment` **pays but does not dispatch** such a booking. | Dispatching to a placeholder offers the job to the wrong caregivers. There is no operator UI yet to clear the flag and start dispatch (follow-up). Since the follow-up SOP, a typed address is geocoded first and only an unconfident result keeps this placeholder and hold. |
| Outbox delivery | At least once, not exactly once. | A send to Meta and a database commit cannot be one atomic step. Marking the event sent immediately after Meta accepts (before the message row is recorded or anything else runs) shrinks the duplicate window to a crash in that one gap. |
| Outbox | New channel-agnostic `booking_events` table plus `app/services/booking_events.emit`, written in the *same transaction* as the change (settlement, expiry). | A hook that sends from inside the Paystack webhook would make the webhook slow and could lose the message; this is also the base W3's NotificationService needs. |
| Window rule | Enforced in `outbound.send_and_record`: a free-form message outside 24 h is not sent, is recorded as failed `window_closed`; callers pick the template with `templates.choose`. | One choke point instead of a habit. |
| STOP | Reminders and the cancellation notice are non-essential and skipped for an opted-out contact; the receipt of a payment they made (`essential`) still goes. | WA-FR-8. |
| Templates | A code registry (`templates.py`), not a `message_templates` table. | The wording must be reviewed in git; the table is only useful once the Admin console can show Meta's approval status. |
| Slots | Hourly, 07:00-20:00 Lagos time, at least 2 h from now; all configurable. | PRD asks for slots "with estimated supply"; supply needs the pin, which comes after the Flow. Flagged. |
| Recipient age | Stored as a July-1 date of birth for the approximate age typed. | The recipient model has only `date_of_birth`. |
| Relationship access | A new recipient is a `family` relationship with `clinical_access = false`. | WA-FR-53/54: logistics only until the recipient consents. |

Rejected: letting the quote message recompute the price on every show; geocoding a typed address now; creating the Paystack transaction inside the conversation lock; sending the reminder and the receipt straight from the webhook thread.

## What's involved
- **Channel:** `app/channels/whatsapp/`: `engine.py` (booking states and effects, still pure), `booking_chat.py` (what the effects do), `booking_rules.py` (services, cities, slots, Lagos time, ₦ formatting), `flows/booking.json`, `flows/crypto.py`, `flows/service.py`, `flows/token.py`, `templates.py`, `notifications.py`, plus edits to `processor.py` (effects, idle expiry, follow-ups), `ingest.py` (`nfm_reply`, template button taps, location pins), `queue.py` (coordinates ride the short-lived body key), `outbound.py` (window rule), `messages.py` (Flow `action`), `copy.py` (all new text).
- **API:** `app/api/v1/endpoints/whatsapp.py` (`/flows/data`), `app/api/v1/endpoints/booking.py` (`enrich_booking_with_details` decrypts the condition for allowed viewers, hides the ciphertext and, for offers, the booker and recipient ids).
- **Services:** `pricing.py` (`service_multiplier`, `price_service`; the strict helpers of the first cut were replaced), `booking.py` (`insert_booking`, `start_payment`), `booking_payment.py` (event in the payment transaction, hold for address check), `booking_expiry.py` (event in the cancel transaction), `booking_events.py` (new), `paystack.py` (`PAYSTACK_BASE_URL`).
- **Models and migration:** `app/db/models/whatsapp_booking.py` (`Quote`, `BookingEvent`), `booking.py` (`channel`, `booked_by_user_id`, `care_recipient_id`, `condition_encrypted`, `needs_address_check`), `whatsapp.py` (states); `app/alembic/versions/0005_whatsapp_booking.py`.
- **Worker:** `app/worker.py` adds `whatsapp_events_job` (cron at :00, :15, :30, :45).
- **Tests and tooling:** `app/tests/test_whatsapp_{flow_crypto,flow_endpoint,flow_json,booking_chat,booking_events,booking_rules,booking_ingest_schema,booking_privacy,engine_booking,templates}.py`, `booking_fixtures.py`, `whatsapp_booking_helpers.py`, additions to `fake_meta.py` (`FlowClient`, `FakePaystack`, `flow_reply_payload`, `template_button_payload`) and `scripts/whatsapp_local_e2e.py`.

### New settings (`.env.example` has placeholders only)
| Variable | Default | Meaning |
|---|---|---|
| `WHATSAPP_BOOKING_FLOW_ID` | empty | Flow id from Meta. **Empty = booking in chat is off** (W1 handover). |
| `WHATSAPP_FLOW_PRIVATE_KEY` (+ `_PASSPHRASE`) | empty | RSA private key (PEM, `\n` allowed on one line). Empty = the endpoint answers 503, never open. |
| `WHATSAPP_SERVICE_CITIES` | `Lagos,Abuja` | Served cities; anything else goes to the waitlist. |
| `WHATSAPP_SERVICE_MAX_HOURS` | `nurse:12,caregiver:24,physiotherapist:4,doctor:2,nutritionist:2` | Longest visit per service (placeholders: **[DECISION]** PRD §8.4). |
| `WHATSAPP_QUOTE_TTL_MINUTES` / `WHATSAPP_IDLE_EXPIRY_MINUTES` | 30 / 30 | Price lock; idle reset of an unfinished booking. |
| `WHATSAPP_PAYMENT_REMINDER_MINUTES` | 20 | After the link is sent (the link lives `PAYMENT_LINK_TTL_MINUTES` = 30). |
| `WHATSAPP_CANCEL_FREE_HOURS` | 12 | The cancellation line in the quote; **W3 made it the real policy** (with `WHATSAPP_CANCEL_FEE_PERCENT`). |
| `WHATSAPP_MIN_LEAD_HOURS`, `_SLOT_START_HOUR`, `_SLOT_END_HOUR` | 2, 7, 20 | Slots offered. |
| `WHATSAPP_MAX_RECIPIENTS` | 10 | Saved people per contact (WA-FR-17). |
| `WHATSAPP_PAYMENT_RETURN_URL` | oaklycare.com/pay/return | Paystack `callback_url` (the web companion page in W4). |
| `PAYSTACK_BASE_URL` | `https://api.paystack.co` | Only the local e2e changes it. **Production refuses to boot with any other value.** |

## Verification
- Baseline on `186cfd1`: 1096 passed. After: 1420 passed (see "Result").
- Tests first for each unit, then code. Notable:
  - **Crypto round trip and tamper:** a body, tag, IV or wrapped key altered by one bit is refused; a key for another public key; malformed envelopes; the flipped IV is proven necessary (decrypting with the request IV fails).
  - **Endpoint:** signature (432), decryption (421), no key configured (503), oversize (413), forged/expired token (427), every screen, every validation, recipient ownership, 10 recipients, per-service caps, the condition limit, waitlist drops the details, p95 well under the 3 s target.
  - **Quote lock vs price change:** the booking and the Paystack amount are the locked kobo after the admin doubles the rate; a *new* quote uses the new price; same total as the app's `calculate_booking_preview`.
  - **One booking:** two Confirm taps, three more taps, and two worker threads at once give one booking and one Paystack transaction. Removing the row locks makes the thread test fail (mutation check).
  - **No health data out:** a sentinel condition and note never appear in any message the bot sends, any WhatsApp table, any log line, the quote after confirm, or the plain booking columns; an offered (not assigned) caregiver sees neither the condition nor the booker ids.
  - **Window rule:** a free-form send outside 24 h is not attempted and is recorded `window_closed`; a template goes; the reminder and the cancellation switch between text and template at the boundary; STOP gets only the receipt.
  - **Time:** reminder not before 20 minutes, once, never for a paid booking; cancellation emits its own message; four threads delivering at once send one message; a dead claim is retried after the lease; five attempts then stop.
- Migration: `app/tests/test_migrations.py` (single head, models match, downgrade to base, upgrade again) passes.

### Result
- Full suite: **1444 passed** after the security-review fixes (1420 before) (`poetry run pytest -q -p no:warnings`; 324 new, baseline 1096). The only existing test that had to change is `test_caregiver_mobile_migration.py` (it inserted a booking through the ORM at the old 0002 schema and the model has since gained columns, so it now inserts with plain SQL), plus two worker tests that name the cron jobs / count completed jobs.
- `poetry check --lock`, `ruff check --select E9,F63,F7,F82 .`, `scripts.ci_smoke` (115 operations; the new endpoint is hidden from the schema) and both `render_fe_guide --check` pass. One alembic head (`0005_whatsapp_booking`); `test_migrations.py` round-trips it.
- **Docker end-to-end** (`poetry run python -m scripts.whatsapp_local_e2e --up --down`): **144 of 144 checks passed** (the security-review fixes added the pay-then-cancel scenario, booking E). It builds the stack, serves a fake Meta and a fake Paystack, and covers:

| Booking | What it proved |
|---|---|
| Endpoint | health check decrypts to `active`; wrong signature 432; tampered ciphertext 421; p95 well under 3 s over 24 real calls |
| A (happy path) | consent, menu, Flow, completion, pin, itemised quote (₦40,000 + ₦4,000 = ₦44,000, cancellation line, three buttons); the admin doubles the rate; Confirm still creates the booking at ₦44,000 and asks Paystack for 4,400,000 kobo with the internal email; condition encrypted, plain column empty; a second Confirm makes nothing new; signed `charge.success` settles it; dispatch started; the receipt arrives once from the worker; chat is idle |
| B (no pin) | the booking is flagged for an address check; reminder at 20 minutes with Pay now; at 30 minutes the real expiry job cancels it (`unpaid_timeout`); outside the 24 h window the notice is the `booking_cancelled_unpaid` template with a Book again button |
| C (return first) | not paid: the API calls the fake Paystack, says it has not arrived, resends the link; paid but no webhook: verify settles it, dispatch starts, the receipt comes straight away |
| E (pay, then Cancel) | Paystack has the money, the webhook has not arrived, the person taps Cancel: the booking is settled and dispatching, the receipt arrives, nothing is cancelled or flagged for refund |
| D (waitlist) | a city outside Lagos/Abuja gets the waitlist offer; joining keeps no address and makes no booking |
| Privacy | the condition is in no bot message, no WhatsApp table, no plain booking column, and not in Redis (nor the pin) |

The script found one thing in itself while being written: a reused price change made a later booking cost ₦88,000 (the script, not the channel).

## Security review fixes (W2 review, 2026-10-06)
| # | Finding | Fix | Where |
|---|---|---|---|
| M1 | `PAYSTACK_BASE_URL` was free text: the secret key goes to it as a Bearer token and its `success` marks bookings paid. | A settings validator refuses to boot when `ENVIRONMENT=production` and the URL is not `https://api.paystack.co` (a trailing slash is tolerated). Other environments may still point at a fake. | `app/core/config.py`; `test_money_kobo_and_config.py` |
| M2a | Tapping Cancel after paying, before the webhook, cancelled a paid booking. | `cancel_unpaid` no longer cancels in the conversation transaction. After the commit (no row lock held across HTTP) it asks Paystack (`booking_payment.verify_and_settle`); if paid it settles and the person gets the normal payment receipt. Only then does it lock the booking, re-check under the lock (a webhook may have won) and cancel. A paid amount that does not match the booking is not cancelled away: the person is told to talk to a person. | `booking_chat.cancel_unpaid` / `_cancel_after_check`, `booking_payment.verify_and_settle` |
| M2b | A booking cancelled by expiry but actually paid (webhook lost) was never restored. | Returning to the chat verifies with Paystack before saying "cancelled" for an `unpaid_timeout` booking; the worker does the same before sending the cancellation notice. Settlement reinstates it as before (visit at least 60 minutes away). | `booking_chat.check_payment*`, `notifications._deliver_one` |
| M2c | Paying a booking that stays cancelled (cancelled by the person, or too close to the visit to reinstate) set `refund_required` and told nobody. | New outbox event and template `payment_refund_pending` ("We received your payment of {{1}} for booking {{2}}, but that booking had already been cancelled. We'll refund you; our team will be in touch."), emitted in the same transaction as the settlement. It is **essential** (reaches someone who sent STOP) and carries no health data. | `booking_payment.settle_booking_payment`, `templates.py`, `whatsapp_booking.BOOKING_EVENTS` |
| L3 | The outbox could double-send after a crash following Meta accepting the message. | Marked sent in its own commit right after Meta accepts (`send_and_record(on_accepted=...)`); new column `booking_events.meta_message_id`. Wording changed from "exactly once" to "at least once". | `outbound.py`, `notifications.py` |
| L4 | "Cancelled because payment wasn't completed" was sent when no payment link was ever delivered. | Skipped (event settled as `stale`, conversation reset) when the quote has no `payment_url`. | `notifications._message_for` |
| L5 | `payment_link_expiring` hard-coded "10 minutes". | Now `expires in {{2}} minutes`, with the number computed as `PAYMENT_LINK_TTL_MINUTES - WHATSAPP_PAYMENT_REMINDER_MINUTES`. **Resubmit this template to Meta** (its body changed). | `templates.py`, `notifications._message_for` |
| L6 | The outbox index `(delivered_at, attempts)` served no query. | Partial index `ix_booking_events_pending (created_at) WHERE delivered_at IS NULL`, in migration 0005 (unmerged, edited in place) and the model. | `0005_whatsapp_booking.py`, `whatsapp_booking.py` |
| L7 | Meta retrying the last Flow screen (ADDRESS) got 427 because the draft was no longer `draft`. | That one screen, for a draft already `submitted` or `out_of_area`, returns the same SUCCESS (the same flow token) and changes nothing (no second recipient). Every other screen and INIT still answer 427. | `flows/service.py` |
| L9 | Cancel proceeded when Paystack could not be asked (timeout/5xx read as "not paid"). | `PaystackService.verify_transaction` now sets `unreachable` for transport errors, 5xx and 429 (a 4xx such as 404 stays a definitive "not success"); `verify_and_settle` returns `SettleOutcome.UNREACHABLE`. Cancel then replies "We couldn't confirm your payment status right now. Please try again in a minute." and cancels nothing; coming back to an expired booking says the same instead of "cancelled". The worker keeps the `booking_cancelled_unpaid` event pending (`last_error = paystack_unreachable`, retried after the claim lease, bounded by the five attempts). | `paystack.py`, `booking_payment.py`, `booking_chat.py`, `notifications._deliver_one` |
| L10 | The worker sent "cancelled" to someone whose money was taken with the wrong amount. | The event is settled as stale and an urgent `talk_to_person` support ticket is opened (no health data). | `notifications._deliver_one` |
| L8 | Downgrade drops `quotes`/`booking_events` data. | Accepted, no change (pre-launch). | |

Notes for operators: a `payment_refund_pending` message means money is at Paystack for a cancelled booking: find it with `SELECT booking_code, refund_required_reason FROM bookings WHERE refund_required;`. The template list to submit to Meta is now five (`payment_refund_pending` is new; `payment_link_expiring` changed). `booking_payment_link` still says "within 30 minutes" in its body; it is not yet derived from `PAYMENT_LINK_TTL_MINUTES` (follow-up).

## Operate / roll back
- **Run locally:** `docker compose up --build`. Booking in chat stays off until `WHATSAPP_BOOKING_FLOW_ID` and `WHATSAPP_FLOW_PRIVATE_KEY` are set. It also needs an active `booking_settings` row (the admin booking-settings endpoint). A service without a multiplier is sold at 1.0 and logged (`pricing: no usable multiplier configured`).
- **See what is happening:** `SELECT status, count(*) FROM quotes GROUP BY 1;` · pending messages: `SELECT event, attempts, last_error FROM booking_events WHERE delivered_at IS NULL;` · bookings waiting for an address check: `SELECT booking_code, patient_address FROM bookings WHERE needs_address_check AND payment_status = 'PAID' AND dispatch_state IS NULL;`
- **A held booking** (paid, `needs_address_check`): confirm the address with the person, set the real coordinates, `UPDATE bookings SET needs_address_check = false ...`, then run `dispatch.start_dispatch(db, booking_id)` (a shell, until the admin console has a button). Nothing dispatches it automatically.
- **Logs:** the worker logs `whatsapp_events: {...}` when it did something; a free-form send refused for the window logs `outside the 24 h window` with the contact ref, never the number.
- **Roll back:** `alembic downgrade -1` drops `quotes` and `booking_events`, the five booking columns and the check, and maps the four booking states to `idle`. WhatsApp bookings stay as bookings but lose channel, booker, recipient and the encrypted condition. Old code ignores everything else. Deploy the migration, API and worker together (the worker imports the new job).
- **Switch it off without a deploy:** clear `WHATSAPP_BOOKING_FLOW_ID`. New "Book a visit" taps hand over; bookings already created finish normally.

## Owner steps (nothing here has been done)
1. **Make a key pair** and keep the private half out of git: `openssl genrsa -out flow_private.pem 2048 && openssl rsa -in flow_private.pem -pubout -out flow_public.pem`. Put the private PEM in `WHATSAPP_FLOW_PRIVATE_KEY` (newlines as `\n`).
2. **Upload the public key** to Meta for the phone number (`POST /{phone-number-id}/whatsapp_business_encryption`, field `business_public_key`): https://developers.facebook.com/docs/whatsapp/cloud-api/reference/whatsapp-business-encryption
3. **Create the Flow** in the WhatsApp Manager Flow builder, paste `app/channels/whatsapp/flows/booking.json`, set the **endpoint URI** to `https://<host>/api/v1/whatsapp/flows/data`, let Meta run its health check (it sends a `ping`), fix anything its validator reports, **publish**, and copy the Flow id into `WHATSAPP_BOOKING_FLOW_ID`. Flow reference: https://developers.facebook.com/docs/whatsapp/flows/reference/flowjson
4. **Submit the templates** (the full list of twelve, with W3's seven, is in the [W3 SOP](2026-10-06-whatsapp-notifications.md#owner-steps-nothing-here-has-been-done)). The five from W2 are in `app/channels/whatsapp/templates.py` (`booking_payment_link`, `payment_link_expiring`, `payment_received`, `payment_refund_pending`, `booking_cancelled_unpaid`), category Utility, English, with the button definitions (URL buttons use base URL `https://checkout.paystack.com/{{1}}`, quick reply "Book again" with payload `menu:book`).
5. Put real prices and the per-service multipliers in booking settings; decide the hour caps and the cancellation policy line.

## What to check against real Meta
The fake Meta mirrors Meta's documented payloads, but these are the places where it could be wrong, in the order they will bite:
- Flow JSON accepted by the builder (the structure test is not Meta's validator): component names, `max-length`, `init-value` on the calendar, `refresh_on_back`, that the `notice` caption renders empty without an `If`.
- The `nfm_reply` shape (`response_json` with `flow_token`), and that the Flow really sends `flow_token` on every call.
- Status codes: 421 (bad decryption), 427 (bad token), 432 (bad signature) as the docs describe.
- Template URL button variable and the quick-reply `payload`; that a template quick-reply tap arrives as `type: button`.
- Paystack: that `checkout.paystack.com/<access_code>` is the authorization URL shape for your account, and that the internal `wa-<ref>@pay.oaklycare.com` address is acceptable to Paystack's terms (WA-FR-23 warning).

## Follow-ups
- ~~Decide: should the app also refuse a service with no multiplier?~~ Decided: no, default 1.0 in both channels.
- **Special instructions** are stored plain in `bookings.special_instructions` (the app's convention) though the Flow says they are private. Encrypt the column everywhere or treat it as non-health.
- Derive the `booking_payment_link` template's "30 minutes" from `PAYMENT_LINK_TTL_MINUTES` the way the reminder now is.
- An operator action (admin console) to clear `needs_address_check` and start dispatch.
- Supply-aware time slots (needs the pin first, or a city-level count).
- `message_templates` table and Meta approval status in the Admin console; Pidgin copy.
- WA-FR-26 return page and the `wa.me` deep link (web companion, W4); today `callback_url` points at `WHATSAPP_PAYMENT_RETURN_URL`.
- `payment_failed` from Paystack cancels the booking silently; tell the person (W3 events).
- Linking an existing app account by one-time code (still W1's `link_required` handover).
- A person who taps Confirm during a Paystack outage sees "couldn't create your payment link" and can retry; nothing alerts operators if it persists.
