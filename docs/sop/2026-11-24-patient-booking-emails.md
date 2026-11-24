# SOP: patient booking e-mails

| | |
|---|---|
| **What shipped** | An app patient now gets an e-mail for the key booking events, in addition to the in-app feed row and push they already had. Branch `feat/patient-booking-emails`, one PR into `develop` (PR link in the PR list). No migration, no new endpoint, OpenAPI unchanged. |
| **Why** | The owner found on 2026-11-24 that patients got no booking e-mails: the backend only mailed the sign-up code, the password-reset code and caregiver notices. A patient who pays for a visit had no receipt in their inbox. |
| **Not verified** | The e-mail text has never been seen in a real inbox. Tests fake the Resend transport and read the HTML it was given. Rendering in Gmail/Apple Mail, spam placement and the sender domain on production are unchecked. |

## Who gets which e-mail
Only the booker of a `channel == 'app'` booking, only if their account is active and has a **verified** e-mail address (`User.is_verified`; login does not require verification, so an unverified address may be mistyped or a stranger's and must never get a receipt). WhatsApp bookings get none (`app/channels/whatsapp/notifications.py` serves them).

| Outbox event | Subject (after `Oaklycare - `) | Body facts |
|---|---|---|
| `payment_received` | Booking confirmed: payment received | Receipt: service, date and time (Lagos), duration, amount paid and currency as stored, Paystack payment reference |
| `caregiver_assigned` | Your caregiver has been assigned | Caregiver first name, visit date and time |
| `caregiver_reassigned` | Your caregiver has changed | New caregiver first name, visit date and time |
| `visit_completed` | Your visit is complete | The feed's "Your visit is complete." plus a thank-you; service and date |
| `booking_cancelled_by_caregiver`, `booking_cancelled_by_admin` | Your booking was cancelled | What happened; one refund line only when `refund_required`: the amount only when `booking.refund_amount_kobo` is set, otherwise "Our team will contact you about your refund." (the total is never quoted as the refund) |
| `no_caregiver_found` | We're still looking for a caregiver | The feed text; a refund line only when `refund_required` |
| `refund_processed` | Your refund has been sent | Refund amount and where it goes (original card/bank, or Oakly wallet) |

Every mail has a first-name greeting, the booking code ("quote it if you contact support") and the app name. Currency is "Nigerian naira (NGN)": bookings have no currency column, every amount is naira in kobo.

**Not e-mailed, on purpose:** `caregiver_en_route`, `caregiver_arrived`, `visit_started` (push only, too noisy), `refund_flagged` / `payment_refund_pending` ("refund started": the cancel mail carries a refund line, and `refund_processed` carries the money), `refund_needs_bank_details` (still feed and push only; it needs a person, see follow-ups). **Never in a body:** visit address, phone numbers, ID data, condition, special instructions, visit code, vitals, care-plan or visit-note content.

## How
- `app/services/patient_notifications.py::deliver` already writes the feed row (unique `notifications.event_id`) and pushes. After the push it now calls `patient_email.send_for_event` (`:161-162`), **only when this pass inserted the feed row**. `_write_feed_row` now returns `(id, created)`.
- **De-duplication key:** `notifications.event_id` (the `booking_events.id`, itself unique per `(booking, event, dedupe_key)`). A replayed event hits `ON CONFLICT DO NOTHING`, `created` is False, no second mail. Chosen over a new `email_sent_at` column: no migration, same guarantee. Cost: at-most-once. A crash between the feed commit and the send loses that one mail; a failed send is not retried (best effort by design).
- `app/services/patient_email.py` builds the content (`compose_mail`) and sends it. It re-checks channel and e-mail, renders `app/templates/emails/patient_booking.html` (one template, brand style of `caregiver_decision.html`; every value HTML-escaped by Jinja) and calls `email.send_email`. It never raises, rolls the session back on error, and logs the event kind only (`patient e-mail visit_completed sent` / `not sent`). `send_email` itself logs a failed send's subject and Resend's error text, never the address.
- The booking itself is never touched: `deliver` runs in the worker after the booking transaction, and the mail is sent after the feed commit.
- Rejected: sending from the booking code paths (would block requests and need its own de-dupe); a per-event template (one template, content from data, is easier to review).

## What's involved
| File | Role |
|---|---|
| `app/services/patient_email.py` | new: content, gating, send |
| `app/templates/emails/patient_booking.html` | new template |
| `app/services/patient_notifications.py` | `_write_feed_row` returns `(id, created)`; `deliver` calls the mailer |
| `app/tests/test_patient_booking_emails.py` | 26 tests |

**Where money is read (for review):** `patient_email.py` `compose_mail` (`total_amount_kobo` and `paystack_reference` for the receipt; the refund row's `amount_kobo`, falling back to the booking's refund amount, for `refund_processed`) and `_refund_sentence` (`booking.refund_amount_kobo`, cancel refund line). All formatted by `booking_rules.naira`. Nothing is computed.

## Verification
- `app/tests/test_patient_booking_emails.py`: each event sends exactly one mail to the booker with the right subject and facts and none of the forbidden strings (condition, notes, address, phone, visit code, caregiver surname, booker surname); unpaid cancel, WhatsApp booking, user without e-mail, erased account send none; en-route, arrived, started, refund-started, bank-details send none; failing (HTTP 500) and raising transports and a template error leave the feed row, the event and the booking intact; a replayed event and a retried feed failure send once; the mail leaves only after the feed row is committed (checked from a second connection).
- Pinning files run unchanged and green: `test_patient_notifications.py`, `test_caregiver_arrived_earning.py`, `test_whatsapp_admin_notifications.py`, `test_patient_notifications_migration.py` (119 tests across the five files). `ruff check --select E9,F63,F7,F82 .` clean; `scripts.ci_smoke` OK (217 operations, one Alembic head). CI runs the full suite.

## Review fixes (2026-11-24, independent review of PR #75)
- Verified address only (above), tested both ways.
- `booking_notices._STALE_FOR` gained `refund_processed` (`needs_review`, `failed`, `closed`, `rejected`): a refund that left `succeeded` before delivery is no longer announced as sent, in the e-mail, the feed row or the WhatsApp text.
- Cancel refund line no longer guesses: no decided amount means the "our team will contact you" wording.
- `email.send_email` now logs only the HTTP status and Resend's error `name` for a failed send, not the response text (which can echo an address).
- The template keeps explicit `|e` on every value (autoescape stays off app-wide); a test renders hostile values and asserts they are escaped, and a comment at the top of `patient_booking.html` says to keep `|e`.

## Operate / roll back
Needs `RESEND_KEY` and `EMAILS_FROM_EMAIL` (already set on staging); without them each send is a logged no-op. To switch off without a deploy, unset `RESEND_KEY`. To revert, revert the PR: nothing was migrated. Worst case latency: `send_email` waits up to 15 s per mail inside the worker pass.

## Follow-ups
- A patient who cancels their own booking gets no e-mail: there is no outbox event for it (`cancelled_by_booker` tells only the caregiver, and a new event needs a check-constraint migration). They get the refund mail when the refund is sent.
- `refund_needs_bank_details` is actionable; consider an e-mail once the wording is agreed.
- Send one real receipt on staging and read it in an inbox.
- A mail lost to a crash or a Resend outage is not retried; add `email_sent_at` on `notifications` if that proves to matter.
