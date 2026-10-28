# SOP: Prembly webhook ingest and confirm (K3)

| | |
|---|---|
| **Date** | 2026-10-28 |
| **Branch** | `feat/kyc-webhook`, rebased onto `oakly/main` `11f7e48` (K2 #27 and care plans B2 #29 merged); two commits on top (the feature and a post-review race fix). **Not pushed, no PR.** |
| **Migration** | None (Alembic head stays `0015_caregiver_kyc`). |
| **Spec** | [`docs/plans/2026-10-27-caregiver-kyc-design.md`](../plans/2026-10-27-caregiver-kyc-design.md) sections 8, 14 (K3), 16.1, 17. K1 SOP: [`2026-10-27-kyc-foundation.md`](2026-10-27-kyc-foundation.md); K2 SOP: [`2026-10-28-kyc-caregiver-api.md`](2026-10-28-kyc-caregiver-api.md). |
| **Status** | Built and tested against the documented Prembly shapes and a fake. **Nothing here has met the real Prembly** (K0 spike and K8 live pass are still ahead). The endpoint is dark until K7b wires the widget. |

## What shipped

| Piece | What it does |
|---|---|
| `POST /api/v1/kyc/prembly/webhook` | Public, `include_in_schema=False`. Capped raw body (413) -> signature (401) -> `token` header (400) -> JSON with a usable `session_id` (400) -> ledger insert (duplicate token: 200) -> ARQ enqueue (failure undoes the insert, answers 500) -> `200 {"status":"ok"}`. No Prembly call and no decision in the request. |
| Signature | `x-prembly-signature` = base64 HMAC-SHA256 of the **raw** bytes keyed by `PREMBLY_WEBHOOK_KEY`, constant-time compare. Empty key, missing or empty header never verify. |
| Ledger | `kyc_webhook_events`: `token` UNIQUE inserted **before** enqueue (`ON CONFLICT DO NOTHING`), session id, sanitised event type, `body_sha256`. The body is **not** stored. Unsigned requests never write. |
| ARQ job `kyc_process_event` | Locates the attempt, **pulls the session from Prembly with the secret key** (K1 client, `include_payload=true`), binds the pulled session to the attempt, records the raw outcome. Retries a failed pull with a delay (`max_tries` 5); after the last try the ledger row is closed `error`. |
| What is recorded | On the attempt, **only once the pulled session is shown to belong to it**: `provider_session_id`, `face_confidence`, `liveness_confidence`, `provider_reference`, `provider_cost`, `completed_at`; plus the reason code `provider_disagreement` when the webhook and the pull contradict each other about a session that does belong. On the ledger: `outcome` (`applied`, `orphan`, `ignored_terminal`, `disagreement`, `error`), `processed_at`, `verification_id`. |
| What is **not** done | **No attempt status change and no caregiver status change.** `passed` / `failed` / `needs_review`, `caregivers.identity_verification_status`, step 3, notifications and the system-decision trail are K4. |
| Production boot refusal | `ENVIRONMENT=production` and any of `PREMBLY_WEBHOOK_KEY`, `PREMBLY_SECRET_KEY`, `KYC_FINGERPRINT_KEY` empty -> the app refuses to start (design 9.3, deferred from K1). |
| Shared body reader | `app/core/http_body.py` (`read_capped_body`), used by this webhook and the WhatsApp one. |

## Why

K-D5 and K-C1: the provider's result must reach Oakly over a server-to-server channel and a client or a forger must never decide. Prembly documents the webhook HMAC key as its **public** key, so the signature alone proves little. The webhook is therefore only the trigger and the dedupe record; the worker re-fetches the session with the secret key and only that pull is recorded. K3 builds and tests that trust boundary on its own so K4 (the decision engine) can sit on top of it.

## How: decisions and trade-offs

| Decision | Why / alternative rejected |
|---|---|
| **Lock order: always the caregiver row first, then its attempts (by id).** Implemented as `kyc_events.lock_caregiver_then_attempt`, the only way KYC code takes these locks | Erasure already locked user -> caregiver -> attempts; design 8.3 specified the worker as attempt -> caregiver, which deadlocks against it (K1 follow-up). The caregiver-first order is also what K2 uses and what K4 needs (it changes the caregiver in the same critical section). Erasure's attempt query now states `FOR UPDATE ... ORDER BY id` explicitly. Rejected: attempt-first (needs erasure to change and still deadlocks with K2's `start_session`, which locks the caregiver first). |
| The Prembly HTTP call is made with **no lock and no open transaction**; the attempt is re-read after the locks | A 10 s provider timeout must not hold a row lock, and an erasure or a human decision can land during the call. Locate (read) -> rollback -> pull -> lock caregiver, lock attempt -> re-check status -> write. |
| Binding: attempt from the signed body's `oakly_ref`, else the client's session hint; the pulled session must belong to it (its echoed `oakly_ref` equals the attempt's, or, when Prembly echoes none, the e-mail Prembly recorded is the caregiver's); a session already on another attempt is refused; a hint that conflicts with the webhook's session is refused | Design 16.1 point 5. Anyone able to sign (public key) can name any ref or session; the pull says whose session it is. Failures are a `disagreement`, not an error. |
| Webhook verdict vs pull verdict: both known and different -> `disagreement`, nothing recorded but the reason code | Design 8.3 step 3. K4 turns it into `needs_review`. |
| **A forged event cannot taint an attempt.** If the pulled session does not belong to the attempt (no ref/e-mail match, `not_found`, session already on another attempt) only the ledger row records `disagreement`; the attempt (located from attacker-controllable input) is not written at all, not even a reason code | Opus review minor #1: someone holding the public key who learns a victim's `oakly_ref` could otherwise push the victim into `needs_review` (K4). The pulled session replaces the client's earlier session hint instead of conflicting with it (review #2: the caregiver reopening the widget is normal). |
| A claimed verdict the pull does not yet show (`claim` set, session not final) is **retried**, not consumed; on the last try it is recorded as bound-only | Review #3: webhook and session read can lag each other. |
| A re-delivered `token` whose first row is **unprocessed** is enqueued again under the same ARQ job id (a still-queued job just refuses it); a processed duplicate is a plain 200 | Review #4: closes the window where the process dies between the insert and the enqueue and Prembly's retry would be deduped into silence. A failed re-enqueue answers 500 and leaves the existing row alone. |
| An attempt that already has a recorded result (`completed_at`) skips the pull on later events | Review #5: a replay flood against a completed attempt costs no Prembly call (K4 makes finished attempts terminal, which skips even earlier). |
| `confirm_and_record` refuses to overwrite a recorded result inside the lock (`completed_at` set -> return `applied`, write nothing); the unlocked check in `process_event` is only a cost saver | Automated post-commit security review (TOCTOU between that pre-check and the lock). |
| Session ids are validated with `fullmatch` (K2, K3 and the client) | Review #6: `^...$` with `match` accepted a trailing newline. |
| The ARQ job carries ledger id, session id, `oakly_ref` and the webhook's verdict only | The body is not stored (design 8.2), so the verdict has to travel; ids are opaque, no name/e-mail/registry data enters Redis or ARQ logs. `keep_result=0`. |
| All events with a usable `session_id` are handled alike, whatever their `event` name | Only `verification.completed` is documented. Dropping unknown names risks losing real completions; the pull is the trust anchor, so handling them is harmless. Names are stored sanitised (or NULL). |
| Retryable pull failure -> `KycRetry` -> ARQ `Retry(defer=...)`; last try closes the row `error`; non-retryable (wallet empty, unauthorised) -> `error` at once and an ERROR log "ops attention" | Retrying cannot fix an empty wallet. K4's reconcile cron finds attempts whose webhook/pull never completed. |
| `not_found` from the pull -> `disagreement` | A signed webhook for a session Prembly does not know is exactly the case the pull exists to catch. |
| Enqueue failure -> delete the row, **500** | Design 8.1: Prembly documents retry on 500 (Meta's code answers 503). Only the exception class is logged. |
| No attempt/caregiver status written | Scope split with K4. The pulled verdict (`pass`/`fail`) has no column (no migration), so it is returned in `Observation` for K4 to act on in the same locked section. |

### Lock-order proof

`app/tests/test_kyc_events.py` has three tests: (1) erasure holds the locks, the worker queues behind it and then finds the attempt expired, writing nothing (no session id is put back after erasure); (2) the worker holds its locks, erasure waits on the caregiver row, then erases what the worker bound; (3) a deterministic test records the `FOR UPDATE` statements and asserts caregivers before identity_verifications in both the worker and erasure (swapping either side fails it; checked by mutation).

### Deviations from the design doc

1. Enqueue failure answers 500 as doc 8.1 says (differs from the Meta endpoint it copies).
2. The `provider_disagreement` reason code is written by K3 (the doc places all reason mapping in K4); it is a raw observation, no status moves. A watch-listed (`07`) pull answer is *not* recorded on the attempt (the attempt is not proven to own that session); it closes the ledger row as `error` and is logged by the client.
3. The job carries the webhook's verdict (see above); the doc assumed the worker could re-read the body.
4. The boot refusal covers the three keys of doc 9.3 only, **not** `PREMBLY_ORG_ID` (see follow-ups).

## UNCONFIRMED seams (all in `app/services/kyc_seam.py` unless noted; resolve in K0, confirm in K8)

| # | Assumption | If wrong |
|---|---|---|
| U1 | The signing key is `PREMBLY_WEBHOOK_KEY` and may be the **public** key | The pull stays; if Prembly has a separate secret signing key the design lets the pull relax to reconcile-only (owner call, design 8.4). Change `webhook_signing_key()` |
| U2 | Signature is standard base64 of the digest, compared as text | Hex or URL-safe base64 would be refused: change `verify_signature` |
| U3 | `token` is unique per webhook and identical on a retry | Retries would be processed again: harmless (idempotent) but wasteful |
| U4 | Retry only on 500; no timestamp or replay window | A replay is bounded by state (terminal attempts ignore events, no pull for them) not by time. Revisit with Prembly's real retry schedule |
| U5 | `event` and `session_id` at the top level (also looked for under `data`); event names untrusted | Events without a usable id get 400 and Prembly may retry them forever: change `parse_event` |
| U6 | Whether `oakly_ref` is echoed in the webhook (`metadata`, `data.metadata`, `data.widget_info.user_ref`) and in the pull's `payload` | Not echoed: binding falls back to the session hint + e-mail match (tested). If the widget lets the user edit the e-mail, honest sessions would disagree: then settle the binding with Prembly |
| U7 | `verification.status` is the webhook's verdict (`VERIFIED` sample) | An unknown value gives no claim; no disagreement check |
| U8 | The pull's `overall_result` is `passed` / `failed` (`PASS_WORDS` / `FAIL_WORDS` are guesses) | An unknown value reads as "not final": nothing is recorded as a result |
| U9 | Scores in the pull's `payload`: `face_data.confidence` (0-100) and `biometric_results.liveness_confidence` (0-1); out-of-scale values are dropped, never clamped | Scores would be NULL until fixed (Q6) |
| U10 | `payload.verification.reference` is Prembly's reference | NULL until fixed |
| U11 | 1 MB body cap | Oversized real events would get 413 |
| (K1) | Session path/shape, pagination, whether `payload` is returned: `prembly.py` `SESSION_PATH`, `_extract_session` | Pull fails closed (`bad_response`/`not_found` -> `error`/`disagreement`), never a result |

## Operate / roll back

- **Deploy needs three env vars in production**, or the API **and** the worker refuse to boot: `PREMBLY_WEBHOOK_KEY`, `PREMBLY_SECRET_KEY`, `KYC_FINGERPRINT_KEY` (and `PREMBLY_ORG_ID` for the pull to work at all). Set them in `/etc/oakly/backend.env` **before** deploying this branch, even with placeholder-free real keys not yet issued (generate a random value for the fingerprint key; the other two need Prembly). Values never go in git or logs.
- Register `<API base>/api/v1/kyc/prembly/webhook` in the Prembly dashboard. Rate-limit the path per IP at the edge (nginx): a holder of the public key can otherwise write ledger rows and trigger pulls.
- A ledger row with `processed_at IS NULL` and an old `received_at` means the job was lost or is still retrying; `outcome = 'error'` means the pull failed for good (check the ERROR "ops attention" log lines for wallet/credential problems).
- Roll back: revert the branch. No migration, no data to undo; ledger rows are inert.

## What's involved

| Area | Files |
|---|---|
| Endpoint | `app/api/v1/endpoints/kyc_webhook.py` (new), `app/api/v1/api.py` (mount at `/kyc/prembly`) |
| Seam, ledger, queue | `app/services/kyc_seam.py`, `app/services/kyc_webhook.py`, `app/services/kyc_queue.py` (new) |
| Worker | `app/services/kyc_events.py` (new), `app/worker.py` (`kyc_process_event`) |
| Shared | `app/core/http_body.py` (new), `app/api/v1/endpoints/whatsapp.py` (uses it) |
| Config / erasure | `app/core/config.py` (`_require_kyc_keys_in_production`), `app/services/account_erasure.py` (attempt locks), `.env.example` |
| Tests | `app/tests/test_kyc_webhook.py`, `test_kyc_events.py`, `test_kyc_boot.py` (new); `fake_prembly.py` (`FakeKycEventQueue`, `kyc_queue`, `PRODUCTION_KYC_KEYS`); `test_money_kobo_and_config.py`, `test_trusted_proxies_warning.py` (production Settings now carry the three keys) |

## Verification

| Check | Result |
|---|---|
| K3 tests | `test_kyc_webhook.py` (endpoint: bad / missing / malformed signature, empty key, tampered and re-serialised body, missing and malformed token, non-event bodies, duplicate and replayed tokens, re-delivery after a lost job, enqueue failure and compensation, oversize and chunked oversize, unknown event types, no body / signature / token in logs), `test_kyc_events.py` (pull-confirmed record, no status changes, disagreement, forged binding, hint handling, orphan, terminal, replay, pull failures and retries, PII sentinels, the ARQ job, three lock-order tests), `test_kyc_boot.py`: 112 cases, all pass |
| Lock order | Race tests in both directions with real concurrent sessions (erasure vs worker) plus a statement-order test; the statement-order test was checked by mutation (swapping the worker's two locks fails it) |
| Full suite | `pytest` on the final head, rebased onto `oakly/main` `11f7e48` (K2 and care plans B2 merged, Alembic head `0017_care_plan_ticks_and_notes`, which includes the post-review race fix): **3310 passed, 4 skipped** (33m18s, under the shared lock, lock released). Earlier, before the rebase: 3022 passed on the pre-race-fix commit |
| CI parity (re-run on the rebased head) | `poetry check --lock` ok; `ruff check --select E9,F63,F7,F82 .` ok; `scripts.ci_smoke` OK (177 operations); both `render_fe_guide --check` exit 0 (no FE-facing shape moved) |
| Opus review | One read-only pass walking design 16.1: **APPROVE WITH FIXES, no blockers**, six minor findings, all six applied in this slice (see the decisions table) and covered by new tests. Not re-reviewed (no blocker remained) |
| **Not verified** | Anything against real Prembly: signature encoding and key, metadata echo, the pull's shape, scores' scale, retry behaviour (all UNCONFIRMED above). No real Redis/ARQ round trip either: the queue is faked in tests; `ArqKycEventQueue` mirrors the proven WhatsApp queue |

## Follow-ups

- **K4 must decide inside the locked section** (`confirm_and_record`, which returns the `Observation` with the pulled verdict): the verdict has no column. It must also treat a ledger row with `processed_at IS NULL` (lost job, or an unretried worker exception) and `outcome = 'error'` rows as work for the reconcile cron, and set the provider-availability flag / wallet alert from the ERROR "ops attention" lines' cause (`PremblyError.needs_ops_alert`).
- K4 should treat a `disagreement` ledger outcome (not only the `provider_disagreement` reason) as `needs_review`; a forged event only ever reaches the ledger.
- **Edge rate limit for `/api/v1/kyc/prembly/webhook`** (nginx, per IP) is assumed by the design and lives outside this repo: confirm it before go-live. A holder of the public key can write ledger rows; pulls are bounded by the "result already recorded" and terminal short-circuits, but a flood against fresh active attempts can still reach Prembly's 429 and, with K4's outage flag, show every caregiver "unavailable". A per-attempt pull debounce is the next step if the edge limit is not enough.
- Boot refusal does not cover `PREMBLY_ORG_ID` (doc 9.3 lists three keys); an empty org id only shows up as `not_configured` on the first pull. Consider adding it.
- Webhook-to-queue gap remains small but real: a crash between the ledger insert and the enqueue is recovered by Prembly's retry only if Prembly retries (U4); K4's reconcile (undelivered list) is the backstop.
- Worker exceptions other than a failed pull (a DB error in the locked section) are not retried by ARQ; the row stays unprocessed for the reconcile cron.
- Post-commit automated review also flagged the e-mail/echoed-ref binding as client-influenceable. Acknowledged, not changed: server-side session creation contradicts design 6 (no Prembly call at session creation, the widget creates the session), and K3 only records; the identity decision (name/DOB match, duplicate fingerprint, human approval) is K4. K0 should confirm whether the widget can alter `metadata` or the prefilled e-mail; if it can, bind more strongly there.
- Resolve U1-U11 after the K0 spike and K8 live pass; update `kyc_seam.py`, `prembly.py` and `fake_prembly.py` from the real captures.
- `datetime.utcnow()` is used for consistency with the codebase (deprecation warnings only).
