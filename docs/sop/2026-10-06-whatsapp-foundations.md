# SOP: WhatsApp foundations (WP-W1)

| | |
|---|---|
| **Date** | 2026-10-06 |
| **Branch** | `feat/whatsapp-foundations` (base `main` at `343978a`) |
| **Migration** | `0004_whatsapp_foundations` (down_revision `0003_admin_identity`; re-parent at merge if the admin auth migration lands first) |
| **Plan / spec** | [plan W1](../plans/2026-10-06-whatsapp-channel.md) · [PRD](../prd/PRD-whatsapp-patient-channel.md) §8.1–8.3, §8.10, §9, §10, §11.1–11.2, §12 |
| **Status** | Verified locally (unit/integration tests and the Docker end-to-end script). Not pushed. Not tested against real Meta: see "Local testing with a Meta developer test number" below. |

## What shipped
A WhatsApp channel module (`app/channels/whatsapp/`) that, with no booking logic yet, can take a stranger from "Hi" to a consented, menu-driven conversation, recognise an emergency, and hand over to a person:
- **Phone-first identity.** `users.email` and `patients.email` are nullable. The first message from a number creates a contact, a patient user (no email, random unusable password), a patient row and a "self" care recipient. Mobile app registration still requires an email.
- **Seven new tables.** `whatsapp_contacts`, `whatsapp_conversations`, `whatsapp_messages` (metadata only), `care_recipients`, `care_relationships`, `consents` (append-only), `support_conversations`.
- **Webhook.** `GET/POST /api/v1/whatsapp/webhook`: signed over the raw body, deduplicated on the Meta message id, queued on ARQ, 200 fast; status callbacks recorded.
- **`MetaCloudClient`.** httpx sender for text, buttons, lists, CTA URL, location request, Flow and template messages, with timeout and retry.
- **Conversation engine.** A pure state machine: first contact, consent by buttons, STOP / STOP DATA / START, main menu, two-miss handover, emergency screen on every text, human handover, fixed reply to clinical-advice questions.
- **Fake Meta + end-to-end script** (`app/tests/fake_meta.py`, `scripts/whatsapp_local_e2e.py`).

Booking is W2: "Book a visit", "My bookings" and "Doctor home visit" reply "coming soon" and open a handover ticket. *(Update: since W2, "Book a visit" and "Doctor home visit" open the booking Flow when `WHATSAPP_BOOKING_FLOW_ID` is set and the contact has an Oakly account; with no Flow id they still behave as described here. "My bookings" stays a placeholder until W3.)*

## Why
The patient app is paused and Meta business verification is pending, so everything the WhatsApp channel needs that does not depend on Meta has to be built and proven locally first: identity without an email, consent that can be evidenced, a webhook that cannot be forged or replayed, an emergency path that cannot be skipped, and a state machine that can be tested exhaustively.

## How: decisions and tradeoffs
| Decision | Choice | Why / alternatives rejected |
|---|---|---|
| Where the work happens | Webhook verifies, records the inbound id and enqueues; the ARQ job `process_whatsapp_inbound_job` runs the engine. | PRD: reply 200 within 1 s. Doing the engine inline would make Meta's timeout depend on the database and the Graph API. |
| Dedupe | `INSERT ... ON CONFLICT (meta_message_id) DO NOTHING` at the webhook, plus ARQ `_job_id=wa:<id>`, plus a `processed_at` claim in the worker. | Three layers because each protects a different failure: Meta retries, a double enqueue, and a job retried after a crash. |
| Queue failure | If enqueueing fails, only the rows that did not reach the queue are deleted and the webhook returns 503. | Otherwise Meta's retry would be deduped away and the message lost. Deleting rows that did enqueue would orphan their jobs. |
| Atomicity | Claim, user creation, engine, consent rows, ticket and new state commit in one transaction; replies are sent after the commit. | A crash before the commit retries cleanly. A failed send never repeats a step, and a retry never double-sends. |
| Send retries | Retry 429 and 5xx (honour `Retry-After`, capped 10 s) and connect errors. A read timeout is NOT retried. | The Cloud API has no idempotency key; a retry after a read timeout can send the patient the same message twice. |
| Early status callbacks | A callback for an unknown message id creates a stub row that the send record then completes (upsert). | Meta can report "delivered" before our send call returns. |
| Consent storage | Append-only events: `decision` (`granted`/`withdrawn`) + `created_at`, latest row per type wins. A DB trigger and an ORM guard refuse UPDATE/DELETE. | The PRD's `granted_at`/`withdrawn_at` on one row needs an UPDATE, which defeats append-only. **Differs from PRD §10.** |
| Consent in W1 | Two interactive buttons (agree / no thanks). Agree records `terms` and `health_processing` with the policy version and the tap's message id. Marketing is never implied. | The three-checkbox Flow (WA-FR-5) arrives in W2; `channel` is `whatsapp_button` now. |
| Existing app account with the same phone | Not linked. The contact has `user_id = NULL` and `context.link_required = true`; consent, menu and handover still work. | WA-FR-3 requires a one-time-code confirmation. Auto-linking would hand someone else's account to whoever controls that number on WhatsApp. The OTP link is not built yet. |
| Handover tickets | At most one live handover per contact (partial unique index); an emergency escalates it to urgent instead of adding a second. `data_withdrawal` is a separate compliance ticket that never merges. | A compliance task must not be hidden by, or relabelled as, a handover. |
| Two misses | Opens the ticket and hands over immediately (WA-FR-41), rather than only offering a person (WA-FR-11 wording). | One ambiguous requirement; the safer reading. Easy to change in `engine._miss`. |
| Emergency | Runs first, on text only, in every state, even when opted out or automation is paused. The reply never echoes the user's words. Biased to false positives: no negation handling. | A missed emergency is worse than an unneeded reply. Lexicon is data in `lexicon.py`. |
| Copy | All text in `app/channels/whatsapp/copy.py`. Replies interpolate config values only, never user text. | WA-NFR-13 and WA-NFR-6. |
| Messages table | No column that could hold a body. A test asserts the column list and that no sentinel text appears in any WhatsApp table. | WA-NFR-6. |
| E.164 helper | Own small `app/core/phone.py` (Nigeria-aware) instead of adding the `phonenumbers` dependency. | No lockfile change for a country we launch in; revisit when other countries are added. |

Rejected: PyWa (the PRD mentions it) because the plan asks for a thin httpx client with our own retry and recording, and it keeps the fake Meta trivial; storing message text for handover transcripts (separate, shorter-retention record when the inbox needs it); processing inline in the webhook.

## What's involved
- **Channel module:** `app/channels/whatsapp/` - `engine.py` (pure state machine), `lexicon.py`, `copy.py`, `messages.py`, `meta_client.py`, `outbound.py`, `ingest.py`, `processor.py`, `identity.py`, `signature.py`, `queue.py`.
- **API:** `app/api/v1/endpoints/whatsapp.py`, registered in `app/api/v1/api.py` (hidden from the OpenAPI schema).
- **Services:** `app/services/consent.py`, `app/services/support.py` (`SupportService.resolve` is what the Admin inbox will call).
- **Models:** `app/db/models/{whatsapp,care,consent,support}.py`; `users.email` and `patients.email` nullable; `UserInDBBase.email` optional in responses (`app/schemas/user.py`).
- **Helper:** `app/core/phone.py` (`normalize_e164`, `phone_variants`, `mask_phone`, `contact_ref`).
- **Worker:** `app/worker.py` adds `process_whatsapp_inbound_job` (retry with 5/20/45/80 s delays, `max_tries = 5`).
- **Migration:** `app/alembic/versions/0004_whatsapp_foundations.py`.
- **Compose:** the API container now gets `REDIS_URL` (it enqueues) and waits for Redis, in `docker-compose.yml` and `docker-compose.prod.yml`; dev adds `host.docker.internal` for the fake Meta.
- **Tests and tooling:** `app/tests/fake_meta.py`, `app/tests/factories_whatsapp.py`, `app/tests/test_whatsapp_*.py`, `scripts/whatsapp_local_e2e.py`.

### New settings (`.env.example` has placeholders only)
| Variable | Default | Meaning |
|---|---|---|
| `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN` | empty | Cloud API sender. |
| `WHATSAPP_APP_SECRET` | empty | Webhook HMAC key. **Empty = every POST is refused.** |
| `WHATSAPP_VERIFY_TOKEN` | empty | Handshake token. Empty = handshake refused. |
| `WHATSAPP_GRAPH_URL` / `WHATSAPP_GRAPH_VERSION` | `https://graph.facebook.com` / `v23.0` | Tests and the e2e point the URL at a fake. Check the version against Meta's changelog before launch. |
| `WHATSAPP_HTTP_TIMEOUT_SECONDS`, `WHATSAPP_MAX_SEND_ATTEMPTS`, `WHATSAPP_RETRY_BACKOFF_SECONDS` | 5, 3, 0.5 | Sender behaviour. Worst case per message about 17 s, inside the worker's 55 s job timeout. |
| `WHATSAPP_POLICY_VERSION`, `WHATSAPP_PRIVACY_POLICY_URL` | `2026-10-01`, oaklycare.com/privacy | Recorded on every consent; shown in the consent message. |
| `WHATSAPP_EMERGENCY_NUMBERS` | `112` | Comma-separated. Add state lines only after they are verified (WA-FR-40). |
| `WHATSAPP_SUPPORT_PHONE` | empty | Optional phone line offered on decline. |

## Verification
- Baseline on `main` (`343978a`): 523 passed. After: **786 passed** (263 new; `poetry run pytest -q -p no:warnings`), plus `poetry check --lock`, the ruff bug-class lint, `scripts.ci_smoke` and `render_fe_guide --check` exactly as CI runs them.
- Tests written first (failing on import), then the code:
  - `test_whatsapp_phone.py`: normalisation table, variants, masking.
  - `test_whatsapp_engine.py`: transition table for every state and input, consent effects, menu ids, misses, STOP/STOP DATA/START, emergency in every state, lexicon positives (English and Pidgin) and negatives, copy length limits.
  - `test_whatsapp_webhook.py`: handshake; valid, invalid, missing, malformed and empty-secret signatures; byte tampering and re-serialisation; oversize; wrong phone number id; replay; queue failure and retry; source attribution; status callbacks (order, failure code, replay, unknown contact, mixed batch).
  - `test_whatsapp_processor.py`: first contact, phone-first user, existing-account no-link, consent grant/decline/review/withdraw/restore, append-only, STOP/START, menu placeholders, two misses, emergency (pause, urgent ticket, escalation, resolve), no body persisted, no health data or user words in outbound, no phone or text in logs, idempotent job, rollback on crash, blocked contact.
  - `test_whatsapp_client.py`: payload shapes, limits, 429/5xx retry with `Retry-After`, terminal 4xx, connect vs read timeout, token never logged, metadata recording, early status callback.
  - `test_whatsapp_schema.py` and `test_whatsapp_queue.py`: constraints, trigger, registration still needs an email, worker wiring and retry, and one message through a real Redis and a real ARQ worker.
- A deliberate mutation (signature check disabled) turned the signature tests red, so they are not vacuous.
- Migration: `app/tests/test_migrations.py` (single head; upgrade from empty equals the models; downgrade to base; upgrade again) passes.
- End-to-end on the Docker stack: `poetry run python -m scripts.whatsapp_local_e2e --up --down`. See the result block below.

### End-to-end result (Docker stack, fake Meta)
`poetry run python -m scripts.whatsapp_local_e2e --up --down` built the stack from scratch, ran, and tore it down (`docker compose down -v`). **23 of 23 checks passed** (now 30; see the addendum):

| Step | Checked |
|---|---|
| Handshake and signature | echo of `hub.challenge`; wrong verify token 403; missing signature 403; wrong signature 403; a refused webhook sends nothing |
| First contact | signed "Hi" accepted; reply goes to the right number as the two consent buttons; arrives inside 5 s |
| Replay | the same Meta message id is acknowledged and produces no second reply |
| Consent and menu | "I agree" returns the five-row menu; two `consents` rows (policy version, evidence = the tap's message id); a phone-first user with no email; "Help" returns the menu |
| Emergency | "my mother has chest pain and cannot wake up": reply has 112, does not echo the symptom, arrives inside 3 s; urgent `emergency` ticket open; automation paused; a follow-up "hello?" gets no reply |
| Privacy | none of the message text appears in any WhatsApp table; every bot reply is recorded as metadata |

The script found two bugs in itself while being written (reused message ids looked like replays; a placeholder `DATABASE_URL` leaked into `docker compose`), not in the channel.

## Operate / roll back
- Run locally: `docker compose up --build` (api, worker, db, redis). The API now needs Redis; the compose file handles it.
- The webhook is useless until `WHATSAPP_APP_SECRET` and `WHATSAPP_VERIFY_TOKEN` are set: that is the fail-closed default, not a bug.
- Logs: `docker compose logs worker` shows `whatsapp contact=<12-char ref> idle -> human`; the API logs `whatsapp webhook rejected` for bad signatures. Contacts appear only as a hash prefix, never a number. An urgent handover logs at ERROR (`whatsapp URGENT handover opened`): that is the line to wire to paging once alerting exists.
- Open handovers: `SELECT reason, priority, created_at FROM support_conversations WHERE status <> 'resolved' ORDER BY priority DESC, created_at;`
- Failed inbound jobs (after five tries) leave nothing in Redis (no result record, body key deleted, by design: see the review-fixes addendum). The dead letter is the database: the message row keeps `processed_at IS NULL`, and the worker log has the failure (message id only): `SELECT meta_message_id, created_at FROM whatsapp_messages WHERE direction='inbound' AND processed_at IS NULL AND created_at < now() - interval '10 minutes';`
- Roll back: `alembic downgrade -1` drops the seven tables and the consents trigger, and makes the email columns NOT NULL again after giving any NULL email a `wa-<id>@placeholder.invalid` address. All WhatsApp data is lost; nothing else is touched. Old code ignores the new tables.
- Deploy order: migration and API together (the API imports the new models); the worker must run the new code too or inbound jobs will sit in Redis with "unknown function".

## Review-fixes addendum (Opus security review, 2026-10-06)

| Finding | Fix | Where |
|---|---|---|
| Message text persisted in Redis (ARQ keeps job args in the result key for an hour and logs them) | The ARQ job carries only the Meta message id. Text, reply id and type go in `wa:inbound-body:<id>` with a 6 h TTL (`WHATSAPP_BODY_TTL_SECONDS`, 21600: it must outlive a worker outage); the worker reads it, deletes it when the job finishes for good (success or last try), refreshes the TTL on a retry. `keep_result = 0` on the worker and the function, `log_results = False`. If the key is missing: a row older than the TTL is marked `status = 'body_lost'`, the person gets "couldn't read your last message, please send it again" (no miss, no state change, even if it was an emergency), and an ERROR is logged with the contact ref; a younger row is skipped without being claimed (a race with an older job; the owning retry carries the text). A refused duplicate enqueue no longer deletes the body key. A test inspects every Redis key (any type) while queued and after success and after five failures: only the body key holds the text while queued, nothing afterwards, nor in logs. The phone number and profile name are no longer in the job at all. | `queue.py`, `app/worker.py` |
| `contact_ref` was an unsalted truncated SHA-256 (a phone number is a tiny search space) | HMAC-SHA256 keyed from `SECRET_KEY` with the domain label `oakly-contact-ref:`; still 12 hex characters. Rotating `SECRET_KEY` changes every ref (log correlation only). | `app/core/phone.py` |
| Emergency lexicon gaps | Phrase list widened (breathing, chest/heart, not waking, slumped, bleeding, sniper, hang myself, "i no wan live again", fits, foaming, dead/died/stabbed/accident...) and a small fuzzy match (difflib, 0.84) on single-word emergencies for typos (unconcious, siezure, sucide). Biased to false positives, but accident, bleeding, stroke and "dey fit" need an acute cue ("had an accident", "is bleeding", "having a stroke"), and fuzzy matching only covers typos starting un/se/si/su, so "after his accident", "bleeding gums", "post stroke care", "conscious" do not trigger. Captions on image, video and document messages are screened too (screen only; a caption never drives a menu choice). | `lexicon.py`, `engine.py`, `ingest.py` |
| Message lost after a 503 | `QueueRefused` when `enqueue_job` returns `None`; the body key is removed on any enqueue failure. With no kept results a job id is free again once the job has run, and the database row is the dedupe. Sequence tested against real Redis: write-then-raise, compensate, retry sees the orphan (503 again), orphan runs and finds no row, next retry is processed once. | `queue.py`, `ingest.compensate` |
| Body-size cap bypass with chunked transfer | The body is read from `request.stream()` with a running count and refused with 413 at 1 MB, before the signature is checked. | `endpoints/whatsapp.py` |
| Hostile but signed payloads gave 500 | `isinstance` guards on every level (entry, changes, value, messages, text, interactive, statuses); ids and types must be short printable strings (a NUL byte would make Postgres raise); a malformed item is skipped (logged by id) and the rest of the batch is processed. | `ingest.py` |
| Blocked contact got no emergency screen; failed emergency reply was silent; phone-only payer broke Paystack and responses; TRUNCATE on consents | A blocked contact still gets the emergency screen and nothing else. A failed emergency send (rejected, network, or recording error) sets `support_conversations.reply_failed_at` and logs `whatsapp EMERGENCY reply could not be delivered to contact <ref>` at ERROR. `PatientBase.email` and `UserProfileResponse.email` are optional (create schema still requires one). `paystack_email_for(user)` returns the real email, else `wa-<contact_ref>@pay.oaklycare.com` (used by booking creation and wallet deposit). Statement-level trigger on `consents` blocks TRUNCATE; the test teardown bypasses it with `session_replication_role = replica` for its own transaction. | `processor.py`, `app/services/paystack.py`, `booking.py`, `patient_wallet.py`, migration `0003` (edited in place: unmerged), `conftest.py` |

Operate: `SELECT id, contact_id, created_at FROM support_conversations WHERE reply_failed_at IS NOT NULL AND status <> 'resolved';` lists emergencies a person must phone. Paystack customers for phone-only payers show as `wa-<ref>@pay.oaklycare.com`; that address is not deliverable, so Paystack receipts for them go nowhere (the bot sends its own confirmations in W2).

Verification: full suite 889 tests green; `scripts/whatsapp_local_e2e.py --up --down` 30 of 30 (adds a photo caption emergency, a Pidgin self-harm phrase, and a dump of every Redis key and value showing no message text, phone number, profile name, body key or result record).

Follow-up: Retry-after-503 convergence relies on Meta retrying; a never-retried half-written enqueue costs one message (the user can write again).

## Local testing with a Meta developer test number + tunnel

> **NOT YET DONE. Owner instructions.** Nothing in W1 has run against real Meta. The automated tests and the Docker script use a fake Meta that mirrors Meta's documented payloads, so the real thing can still differ in small ways. This section is how to find out. Business verification is NOT needed for this.

What you need: a Meta developer account, a Meta developer app with the WhatsApp product, a tunnel to your laptop, and your own phone.

1. **Create the app.** developers.facebook.com > My Apps > Create App > type "Business" > add the **WhatsApp** product. Meta gives you a free **test phone number** and a temporary access token (valid 24 h; a System User token lasts longer).
2. **Add your own number as a recipient** (API setup page > "To" > Manage phone number list), and confirm the code WhatsApp sends you. The test number can only message numbers on that list.
3. **Collect four values** and put them in your local env file for the stack (never in git): Phone number ID (API setup), temporary Access token, **App secret** (App settings > Basic), and a **Verify token** you invent (any random string).
   ```
   WHATSAPP_PHONE_NUMBER_ID=...
   WHATSAPP_ACCESS_TOKEN=...
   WHATSAPP_APP_SECRET=...
   WHATSAPP_VERIFY_TOKEN=...
   ```
   Leave `WHATSAPP_GRAPH_URL` at its default so the stack talks to the real Graph API.
4. **Start the stack:** `docker compose up --build`. Check `curl http://127.0.0.1:8000/health`.
5. **Open a tunnel** to port 8000, for example `cloudflared tunnel --url http://127.0.0.1:8000` or `ngrok http 8000`. Copy the public `https://...` address.
6. **Register the webhook.** App dashboard > WhatsApp > Configuration > Webhook > Edit. Callback URL `https://<tunnel>/api/v1/whatsapp/webhook`, Verify token = your `WHATSAPP_VERIFY_TOKEN`. Meta calls the GET handshake; it must succeed. Then **subscribe to the `messages` field**.
7. **Try it from your phone.** Message the test number "Hi". Expect the consent message with two buttons. Tap "I agree" and expect the menu. Then try: `STOP`, `START`, `STOP DATA`, "menu", a gibberish message twice (handover), and an emergency phrase such as "I can't breathe" (emergency reply with 112 and an urgent ticket).
8. **Look at the result.**
   - `docker compose logs -f api worker`
   - `psql postgresql://oakly:oakly@127.0.0.1:5432/oakly -c "select state, automation_paused from whatsapp_conversations"` and `-c "select type, decision, evidence_ref from consents"`
9. **Capture the real payloads** (the point of this exercise): temporarily log the raw webhook body in a scratch copy, or use Meta's webhook test button, and compare against `app/tests/fake_meta.py`. Any difference is a bug in the fake; fix the fake and add a test. Remove the logging afterwards (real payloads contain phone numbers and message text).
10. **Things to watch for:** the 24-hour window (a business-initiated message outside it needs a template; W1 only replies inside it); the temporary token expiring (the sender then logs 401 / code 190); a 403 on the handshake means the verify token differs.

When this has been done, tick "Real-Meta check" in the master checklist and record any differences found here.

## Follow-ups
- **W2 (booking in chat) shipped** (see [the booking SOP](2026-10-06-whatsapp-booking.md)): booking Flow, quote, Paystack link, templates, idle expiry (WA-FR-12). **Still open from this list:** the consent Flow (three checkboxes) that replaces the buttons, and one-time-code linking of an existing app account (`context.link_required` marks the contacts waiting for it; they are still handed over when they try to book).
- Retention jobs (WA-NFR-9): message metadata 12 months, consents life + 6 years **[DECISION with counsel]**.
- Pidgin copy (`STRINGS["pcm"]`) and Pidgin intent keywords (Phase 2); the emergency lexicon already includes Pidgin.
- The emergency numbers per state are unverified (PRD warning); only `112` is configured.
- The ARQ pool the webhook opens is never closed explicitly (process exit does it). Add a lifespan hook if the API ever restarts workers in-process.
- Rate limiting on the webhook is not added (the repo has no limiter yet; Meta's source IPs and the signature are the gate).
- The Admin inbox and agent replies (A-series) will call `SupportService.resolve` and need a handover transcript record with its own 90-day retention.
