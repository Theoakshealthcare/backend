# SOP: caregiver KYC foundation (K1)

> **Provider switched to Dojah on 2026-10-30 (slice KD1, [SOP](2026-10-30-kyc-dojah-adapter.md)).** `app/services/prembly.py`, `FakePrembly`, the `PREMBLY_*` settings and the `prembly` provider default are gone: replaced by `app/services/dojah.py`, `FakeDojah` (`app/tests/fake_dojah.py`) and `DOJAH_*`. The tables are unchanged (`provider_session_id` now holds the server-minted reference; `provider_reference` the same value for the admin console; `token` the derived dedupe key); migration `0015` was edited in place for the `dojah` server default. The rest of this document is the record of the original Prembly-era work.

| | |
|---|---|
| **Date** | 2026-10-27 |
| **Branch** | `feat/kyc-foundation`, based on `oakly/main` `5c5a48a` (design PR #24 merged, Alembic head `0014_refunds_v2`). Commits: `git log 5c5a48a..feat/kyc-foundation`. **Not pushed, no PR.** |
| **Migration** | `0015_caregiver_kyc` (down_revision `0014_refunds_v2`). The care-plans slice B1 chains `0016_care_plans_core` after it, so K1 merges first. |
| **Spec** | [`docs/plans/2026-10-27-caregiver-kyc-design.md`](../plans/2026-10-27-caregiver-kyc-design.md) sections 7 to 10 and the K1 row of section 14. Owner defaults for O1 to O5 accepted. |
| **Status** | Built and tested locally against **the public Prembly docs only**. Nothing here has met the real service; every shape marked UNCONFIRMED below is a guess behind a seam. No endpoint, webhook, decision engine or mobile code (K2 to K7). |

## What shipped

The database model, client and test fake that the rest of the KYC work stands on. Nothing is reachable by a caller yet: with no screen starting a session (K7b) the new tables stay empty.

- **Three tables.** `identity_verifications` (one row per attempt), `kyc_webhook_events` (the webhook ledger), `identity_verification_events` (append-only trail of system decisions).
- **`caregivers.identity_verification_status`** gets a CHECK and a new value, `needs_review`.
- **The Onfido era is gone.** `app/services/onfido.py`, every `ONFIDO_*` setting and the `onfido_*` columns are deleted, and so are the two client-posted JSON columns and `face_verification_status`.
- **`consents`** accepts the type `identity_verification`.
- **`app/services/prembly.py`**, an httpx client with an injectable transport: read a session, list undelivered sessions, and Prembly's error conventions mapped onto one exception.
- **`app/tests/fake_prembly.py`**, a fake Prembly in the `fake_meta` / `FakePaystack` mould, with webhook-signing helpers for K3.
- **Config names** (`PREMBLY_*`, `KYC_*`) with no secret values anywhere; `.env.example` lists them commented out.
- **Account erasure** knows the new tables.

## Why

K-D1 to K-D6 (owner, 2026-10-27): the provider's signed result, not the phone, must be the evidence for identity. Every later slice needs somewhere to record attempts and outcomes, a way to talk to Prembly and a fake to test against, and the dormant Onfido code and the columns that could hold client-posted images had to go before anything real is stored. Greenfield (no production data), so columns are dropped, not deprecated.

## How: decisions and trade-offs

| Decision | Why / alternative rejected |
|---|---|
| Store **no** provider-returned personal data: the attempt keeps scores, yes/no `name_match` / `dob_match`, reason codes, id type, Prembly's reference and cost, and a keyed fingerprint (`id_fingerprint`, owner O1: kept) | Doc 10.1. A test asserts the attempt table has no name/DOB/number/photo/payload column. The webhook ledger keeps `body_sha256`, never the body. |
| One active attempt per caregiver is a **partial unique index**, not application logic | Doc 7.1. A forced race cannot create two. Terminal attempts stack, so retries work. |
| `identity_verification_events` copies the `consents` pattern: ORM listeners plus a DB trigger for UPDATE/DELETE and a statement trigger for TRUNCATE | `admin_audit_log.admin_user_id` is NOT NULL, so a system actor cannot be written there (doc 5). |
| `reason_codes` has **no CHECK** (the set lives in `REASON_CODES` in the model) | K3/K4 are no-migration slices and may need another code; the status sets, which are the state machine, are checked. |
| The migration's **downgrade** deletes `identity_verification` consent rows with the append-only trigger switched off for that one statement, maps `needs_review` back to `in_progress`, and restores the dropped columns **empty** | The old consent-type CHECK cannot hold the new type. Do not downgrade once real verifications exist. |
| `PremblyClient` makes **one attempt per call** and raises `PremblyError(kind, ...)` with `retryable`, `outage` and `needs_ops_alert` flags; it never logs a body | Retry/back-off policy belongs to the callers (K3 job, K4 reconcile). `response_code` is read before the HTTP status because Prembly answers HTTP 200 for some failures. Mapping: `01` not_found, `02` unavailable, `03` and HTTP 402 wallet_empty (ops alert), `07` watchlisted (never retried), HTTP 429 rate_limited (waits `Retry-After`, else 60 s), 401/403 unauthorised, 5xx unavailable, timeout and network kinds. |
| The session id is validated (`^[A-Za-z0-9_.-]{1,128}$`) before it is put in a URL path | It can originate in the client's non-authoritative hint (doc 16.1 point 5). |
| Erasure expires any attempt still in flight (so a late webhook or reconcile meets a terminal attempt) and nulls `provider_session_id`, `provider_reference` and `id_fingerprint` on the caregiver's attempts and the `provider_session_id` on their ledger rows (matched by attempt **or** by session id, so an orphan row goes too); status, scores, timestamps and Oakly's own `reference` stay | Doc 10.3. Another caregiver's rows are untouched (tested). A withdrawn `identity_verification` consent row is appended if, and only if, they had granted one. |

### Deviations from the design doc (all small, all forced or deferred on purpose)

1. **Fallout of the drops, outside the "no endpoints" line.** Three callers read the dropped columns. The old `POST /caregiver/identity-verification/result` still answers 200 but no longer stores the posted JSON; it only moves `identity_verification_status` to `in_progress` (never over `verified`/`failed`) and bumps the step (`app/services/caregiver.py`). The admin `identity` block keeps its three keys with constant `null` so the response shape, the golden file and the admin console do not change in between (`app/services/caregiver_approval.py`). K2 turns the endpoint into 410 and K5 reshapes the block. No FE-guide change: no shape changed.
2. **No production boot refusal** for empty `PREMBLY_SECRET_KEY` / `PREMBLY_WEBHOOK_KEY` / `KYC_FINGERPRINT_KEY` (doc 9.3). Nothing reads them yet, so the refusal would only break a production boot. It belongs with K3, when the webhook goes live.
3. `identity_verification_events.verification_id` is NOT NULL as in the doc, so an admin decision on a caregiver who never started an attempt has no row there (it is still written to `admin_audit_log` as today).
4. `fake_prembly.py` has no stand-alone HTTP server (the doc mentions one for the capture script). K2's `capture_kyc_flow.py` adds it if it needs one.

## What's involved

| Area | Files |
|---|---|
| Migration | `app/alembic/versions/0015_caregiver_kyc.py` |
| Models | `app/db/models/identity_verification.py` (new); `app/db/models/caregiver.py` (CHECK, dropped columns); `app/db/models/consent.py` (`CONSENT_TYPES`) |
| Service | `app/services/prembly.py` (new); `app/services/onfido.py` deleted |
| Erasure | `app/services/account_erasure.py` (`_wipe_identity_verifications`, consent withdrawal) |
| Fallout | `app/services/caregiver.py` (`process_verification_result`), `app/services/caregiver_approval.py` (identity block) |
| Config | `app/core/config.py`, `.env.example` (names only) |
| Tests | `app/tests/fake_prembly.py` (+ fixture registered in `conftest.py`), `test_prembly_client.py`, `test_kyc_models.py`, `test_kyc_migration.py`, bottom of `test_account_deletion.py`; `test_access_caregiver_verification.py` and the Onfido lines of `test_account_deletion.py` adapted |

Config names: `PREMBLY_BASE_URL` (default `https://api.prembly.com`), `PREMBLY_SECRET_KEY`, `PREMBLY_ORG_ID`, `PREMBLY_WIDGET_ID`, `PREMBLY_WIDGET_KEY` (public), `PREMBLY_WEBHOOK_KEY`, `KYC_FINGERPRINT_KEY`, `KYC_MAX_ATTEMPTS` (3), `KYC_SESSION_TTL_HOURS` (24), `KYC_FACE_THRESHOLD` (85, a placeholder until K0), `KYC_FACE_FLOOR` (70), `KYC_POLICY_VERSION` (`kyc-2026-10`).

## Verification

- New and adapted tests (client, models, migration, erasure, the old result endpoint): 165 passed in the targeted run.
- Migration: single head; upgrade from `0014`; downgrade restores the old schema, maps data and re-arms the consents trigger; round trip; `test_migrations.py` (empty to head matches the models, down to base, up again) passes.
- Full local CI parity with the project's poetry env: `poetry check --lock` ok, `ruff check --select E9,F63,F7,F82 .` clean, `scripts.ci_smoke` OK (154 operations), both `render_fe_guide --check` exit 0, full pytest **2,773 passed, 1 skipped, 1 failed**. The one failure, `test_caregiver_client_profile.py::test_latest_vitals_is_an_empty_list_...`, is an existing flake unrelated to K1: it asserts `"84" not in str(body)` and the body holds random UUIDs that can contain "84". The file passes on rerun (27/27). Worth fixing separately (assert on the vitals field, not the whole body).
- One read-only Opus review of the migration, models and erasure: no blockers. Fixed in K1: the legacy result endpoint could pull a caregiver out of `needs_review` (now guarded, tested); erasure left in-flight attempts active (now expired, tested); the identity-consent withdrawal uses a new `KYC_POLICY_VERSION` (`kyc-2026-10`), not the WhatsApp version. The rest are in Follow-ups.
- **Not verified:** anything against real Prembly (sandbox or live). Until K0 and K8 the client and fake are built from the public docs.

## Operate / roll back

- Deploy: `alembic upgrade head` creates the tables and drops the columns. No new env value is required (all `PREMBLY_*` / `KYC_*` default to empty or the doc's defaults, and nothing reads the secrets yet).
- Roll back: `alembic downgrade 0014_refunds_v2` (see the downgrade row above). The dropped columns come back empty; that is acceptable only because nothing is stored in them in production (greenfield). Once a real verification exists, write a forward migration instead.
- `0016_care_plans_core` (B1) must chain after this revision; if B1 merges first, it will need `down_revision` rebased onto `0015_caregiver_kyc` (or the reverse), never two heads.

## Follow-ups

| Item | Owner / slice |
|---|---|
| **UNCONFIRMED vs real Prembly** (design section 15): the session-retrieval path and auth headers (Q3: the two doc pages differ, the client uses `.../sessions/{id}/` and `.../sessions/undelivered/`), the by-id response shape (the parser accepts `data`, `data.session` and `data.sessions[]`), pagination arithmetic (the docs' sample contradicts itself, so `has_more` is "a full page"), whether `payload` is returned, `overall_result` values other than `passed`, `metadata` echo (Q2), the signing key (Q4), sandbox host (Q7). The seam is `SESSION_PATH`, `UNDELIVERED_PATH`, `_extract_session`, `_extract_sessions` in `app/services/prembly.py`, and `app/tests/fake_prembly.py`. | K0 spike, then K3/K8 |
| Production boot refusal for the empty Prembly/fingerprint keys (doc 9.3) | K3 |
| Erasure now expires in-flight attempts, but writes no `identity_verification_events` row for it (the actor set has no system value). K4 should still ignore events for an erased caregiver. **Lock order:** erasure locks the caregiver row, then touches attempts; doc 8.3 has the worker lock the attempt, then the caregiver. Postgres would abort one side (no corruption), but K3/K4 must pick one order, e.g. lock the caregiver's attempts inside `_check_caregiver` | K3/K4 (named owner needed before K3 merges) |
| A webhook ledger row whose session id never reached any attempt (a lost client hint) cannot be tied to a caregiver, so erasure cannot scrub its `provider_session_id` (it holds no `oakly_ref`) | K3 |
| `caregivers.identity_verification_status` is nullable and the CHECK lets NULL through (`pending` is the default and the code never writes NULL) | accepted; tighten with NOT NULL if a migration is open anyway |
| Retention after erasure: the doc proposes dropping status, timestamp and reference after 12 months. `identity_verification_events` refuses DELETE by trigger, so a retention job needs a deliberate bypass like the downgrade's | owner O3 / a later job |
| `KYC_FACE_THRESHOLD` / `KYC_FACE_FLOOR` are placeholders (Q6) | K0 |
| The old `POST .../identity-verification/result` is now a no-op that returns 200 and discards the body; it goes to 410 in K2 | K2 (done: [`2026-10-28-kyc-caregiver-api.md`](2026-10-28-kyc-caregiver-api.md)) |
| Prembly pricing and the DPA remain owner items (checklist section 14) | owner |
