# SOP: Dojah backend adapter (KD1)

| | |
|---|---|
| **What shipped** | The caregiver KYC provider seam now speaks **Dojah** instead of Prembly: a new client (`app/services/dojah.py`), a rewritten seam (`kyc_seam.py`), a server-minted widget reference, the `x-dojah-signature` webhook, a reference-based reconcile, `DOJAH_*` config, a fake Dojah for tests, and both FE guides re-captured. K3 (ledger, worker), K4 (decision rules, engine, locks) and K5 (admin API) are unchanged in behaviour. |
| **Branch / commit** | `feat/kyc-dojah-adapter`, cut from `oakly/main` `6d7d315` (K1-K5 merged). Not pushed; the lead opens the PR. |
| **Migration** | **None.** Alembic head stays `0019_care_plan_admin_permissions`. Migration `0015` was edited in place (server default of `identity_verifications.provider`: `prembly` -> `dojah`; greenfield, no data). The Python-side default is what rows get, so an already-migrated database is unaffected. |
| **Spec** | [docs/plans/2026-10-30-caregiver-kyc-dojah-delta.md](../plans/2026-10-30-caregiver-kyc-dojah-delta.md) (sections 2 and 4), on top of [the base design](../plans/2026-10-27-caregiver-kyc-design.md) (sections 6-9, 12, 14, 16.1). |
| **Verified against real Dojah?** **No.** Everything was built against `FakeDojah`, whose shapes come from Dojah's public docs and from the Timp and Weysure parsers. The UNCONFIRMED list below is the work of the K8 live pass. |

## Why
The owner dropped Prembly (unresponsive) on 2026-10-30 and chose Dojah. Dojah's model is the one K3-K5 already assumed (hosted widget, signed webhook as a trigger only, server pull as the authority), and the owner's other projects already run it, so only the provider seam changes. The change also removes the weakest part of the Prembly design: the binding of a webhook to an attempt by e-mail or a client-reported session id (U6), which needed squatted-hint clearing and made discovery through an "undelivered" list necessary.

## How: the changes that matter
| Area | Before (Prembly) | Now (Dojah) |
|---|---|---|
| Reference | Opaque `oakly_ref` hoped to be echoed; client reported a session id | The server mints `OKL-<uuid4 hex>` in `start_session`, stores it in `identity_verifications.provider_session_id` (and `provider_reference`) **before** the widget opens, and returns it as `widget.reference_id`. The webhook's `reference_id` and the pull key are that string. Binding is an exact match; the pulled answer must name it back (`dojah._extract`, `kyc_events.confirm_and_record`). No client hint, no e-mail binding, no squatted-hint logic. |
| Signature | base64 HMAC, key maybe public | `x-dojah-signature` = lower-case **hex** HMAC-SHA256 of the **raw body** keyed by `DOJAH_WEBHOOK_SECRET` (the dashboard's webhook "Secret", not the API key). Constant-time, empty secret or header never verifies, non-ASCII header is a plain mismatch. `x-dojah-signature-v2` is ignored (it does not bind the payload). |
| Dedupe | unique `token` header | No idempotency header exists. The ledger key (`kyc_webhook_events.token`, no migration) is `sha256(reference, status word, sha256(body))`. The same body twice is one event; a different status or body for the same reference is another. |
| Webhook content | verdict claimed in the body | Only `reference_id` and the status word are read; everything else (including `selfie_url`, `id_url`, `back_url`, `verification_pdf`) is dropped. The webhook can claim only a failure (`Failed`); `Completed` means "the flow ended", never "passed". The ARQ job carries `{event_id, claim, settled}` only. |
| Foreign reference | n/a | A signed event whose reference is not `OKL-` + 32 lower-case hex is acknowledged `200` and not recorded (the Dojah account may serve other widgets). An unusable body is `400`. |
| Pull verdict | `overall_result` words | `verification_status`: `Completed` passes only if `government_data.status` AND `selfie.status` are true and `selfie.confidence_value` (when a number exists) is at least `DOJAH_FACE_MATCH_THRESHOLD` (70). An explicit `false` is a fail; an **absent or non-boolean** result is `Evidence.incomplete` -> `needs_review` (`identity_data_missing`), so a drifting response shape never costs caregivers a try and never verifies anyone. `Failed` is a final fail. `Pending`, `Ongoing`, `Abandoned` and unknown words are **not final** (nothing recorded). Then the engine's own bands (`KYC_FACE_FLOOR` / `KYC_FACE_THRESHOLD`) apply to the same number. |
| Liveness | 0-1 score vs a floor | A boolean: `Evidence.liveness_passed` = `selfie.status`. `Policy.liveness_floor` is gone; `identity_verifications.liveness_confidence` stays NULL; `KYC_LIVENESS_FLOOR` remains in `Settings` only so an env file that sets it still loads (`extra="forbid"`). |
| Registry identity | name/DOB/number under `payload.verification_response` | `data.government_data.data.<bvn\|nin>.entity` with `first_name`, `middle_name`, `last_name`, `date_of_birth`, and the number under the type's own key. **Whitelisted at extraction** (`dojah.REGISTRY_FIELDS`): the BVN/NIN photo and every other field never leave the JSON parse. |
| Reconcile | step 3 drained Prembly's undelivered list and bound by echoed ref | Removed: every active attempt has a reference, so the cron pulls each quiet attempt by it. A `404` ("Dojah never heard of it", normal for a widget nobody opened) is an answer, not an error: the attempt expires at its TTL. Budget 35 s -> 20 s (one pull can take ~31 s with the client's 3 attempts). |
| Client | one attempt per call | `GET {base}/api/v1/kyc/verification?reference_id=` with `Authorization: <secret>` and `AppId`; 10 s timeout, 3 attempts, 0.5/1 s back-off, **only a 5xx or a transport error is retried**; error kinds (`wallet_empty`, `unauthorised`, `rate_limited`, `unavailable`, `not_found`, `rejected`, `bad_response`, `timeout`, `network`, `not_configured`) feed the same health and ops-alert hooks. |
| Config | `PREMBLY_*` | `DOJAH_BASE_URL`, `DOJAH_APP_ID`, `DOJAH_PUBLIC_KEY`, `DOJAH_SECRET_KEY`, `DOJAH_WIDGET_ID`, `DOJAH_WEBHOOK_SECRET`, `DOJAH_FACE_MATCH_THRESHOLD`; all `KYC_*` kept. Production refuses to boot without `DOJAH_WEBHOOK_SECRET`, `DOJAH_SECRET_KEY`, `KYC_FINGERPRINT_KEY`. |

Decisions and rejected alternatives:
- **Reuse `provider_session_id` for the reference** instead of a new column: no migration, the unique index already gives "one attempt per reference", and the erasure code already nulls it. The name is stale; the model comment says so.
- **`provider_reference` holds the same string** so the admin console (which never sees `provider_session_id`) can show it for a dashboard lookup, with no admin code change.
- **Mint with a fresh `uuid4`, not derived from the attempt's own `reference`** (the consent's evidence id): the two never need to be linked by anyone but us, and a leaked Dojah reference then says nothing about our internal id.
- **The webhook claims only `Failed`.** Computing a pass/fail claim from the webhook's own `data` block would flag a "disagreement" whenever the webhook and pull shapes differ (the webhook shape is unverified), sending every caregiver to review. A failure claim over a pulled pass is the one contradiction that is both detectable and meaningful.
- **`Abandoned` is not final**, so it never counts against the caregiver's three attempts; it ends at the 24-hour TTL (delta: "abandoned -> expired path"). A relaunch with the same reference is the unknown (below).
- **Retries live inside the client** (as in Weysure) and ARQ's retry sits on top; the worker's "count one provider error per event" rule is unchanged.
- **Rejected:** a hard allow-list of Dojah's source IP (`135.119.89.106`; it can change and our proxy setup would have to be trusted for it) - logged as a follow-up.

## What is involved
| Path | Change |
|---|---|
| `app/services/dojah.py` (new) | Client, error kinds, whitelisted extraction (`DojahVerification`) |
| `app/services/kyc_seam.py` | Rewritten: `mint_reference`, `is_our_reference`, `verify_signature`, `dedupe_key`, `parse_event`, `observe`, assumptions D1-D14 |
| `app/services/kyc_events.py`, `kyc_reconcile.py`, `kyc_webhook.py`, `kyc_hooks.py`, `kyc_notify.py`, `kyc_queue.py`, `kyc_availability.py` | Dojah types, reference binding, reconcile without discovery, wording |
| `app/services/kyc_decision.py`, `kyc_engine.py` | `liveness_passed`, no liveness floor |
| `app/services/kyc_caregiver.py`, `app/schemas/caregiver.py`, `app/api/v1/endpoints/caregiver.py` | Minted reference, new widget block, `client-event` without `session_id` |
| `app/api/v1/endpoints/kyc_webhook.py`, `app/api/v1/api.py` | `POST /api/v1/kyc/dojah/webhook` (the old `/kyc/prembly/webhook` is gone) |
| `app/core/config.py`, `.env.example` | `DOJAH_*`, boot guard |
| `app/db/models/identity_verification.py`, `app/alembic/versions/0015_caregiver_kyc.py` | Provider default, comments |
| `app/services/prembly.py`, `app/tests/fake_prembly.py`, `app/tests/test_prembly_client.py` | Deleted |
| `app/tests/fake_dojah.py`, `test_dojah_client.py`, `test_kyc_seam.py` (new); `test_kyc_webhook/events/trust/reconcile/engine/decision/caregiver_api/admin_api/boot/models.py`, capture tests, golden shapes | New and ported |
| `docs/fe-integration-guide-caregiver.md`, `-admin.md`, `docs/fe-captures/{caregiver,admin}/` | Re-captured through the harness; `render_fe_guide --check` passes for both |

## Verification
Local checks run on this head (the full unit suite was **not** run; CI is the first full run):
- `poetry check --lock`, `ruff check --select E9,F63,F7,F82 .`, `python -m scripts.ci_smoke` (198 operations, as before), both `render_fe_guide --check`.
- All `app/tests/test_kyc_*`, `test_dojah_client.py`, `test_capture_kyc_*` (via `CAPTURE_KYC_*=1`), `test_response_shapes.py` (golden regenerated: only `identity_session.widget`), `test_account_deletion.py`, `test_money_kobo_and_config.py`, `test_trusted_proxies_warning.py`, `test_worker.py`.
- New coverage: signature (valid, wrong, missing, empty secret, v2-only, base64 instead of hex, upper-case/padded hex, non-ASCII header, tampered and re-serialised body, the API secret key is not the webhook secret); parse fixtures (pass, face fail, liveness fail, government fail, pending, ongoing, abandoned, NIN, BVN, malformed, missing blocks, out-of-range and non-numeric scores); dedupe and replay (same body, other status, other body, concurrent, lost job, failed requeue); foreign and unknown references; a pull answer about another reference decides nothing; wrong-but-signed body cannot make a pass; the pull stays the only thing that settles an attempt; the K4 property and race tests over the new fake; privacy (no photo, image link, name, DOB, number in tables, logs, the ARQ payload or the ledger).

## Review (Opus, read-only, scoped to webhook auth and dedupe, pull authority, reference binding, PII)
No findings on the signature, the dedupe key, the binding, double-settle, reference entropy, client input or PII. Applied from the review: (1) **major** - an answer naming another reference was `bad_response` (retryable AND an outage), which made the reconcile cron stop all pulls and all expiries behind one attempt on every run; it is now its own kind `reference_mismatch` (not retryable, not an outage), the attempt is debounced and ends as `error` at its TTL; (2) a refused enqueue no longer deletes a row whose job a concurrent twin delivery already queued; (3) a late webhook for an erased reference no longer leaves the reference in the ledger (orphan rows null it); (4) an absent result is review, not a failed try; (5) a pre-references active attempt whose id is not one of ours is re-minted on resume; (6) `client_event` locks the caregiver row first (it could deadlock with erasure; pre-existing); (7) the `httpx` logger is set to WARNING so the pull URL is not logged. (8) from the commit-time security scan: the reconcile pull, the webhook lookup and the bind check accept only references shaped like ones this server minted (`OKL-` + 32 hex), so an older client-chosen id is never pulled or bound and its attempt still expires; no data migration (greenfield). Not applied: relaunching the widget for an attempt already in `awaiting_result` returns the same reference (see follow-ups). Not re-reviewed (no second round: no security or double-settle hole remained).

## Operate / roll back
- **Deploy:** remove every `PREMBLY_*` line from the env files (`Settings` forbids unknown keys, so a leftover key fails boot), set the `DOJAH_*` names, and set `KYC_FINGERPRINT_KEY` as before. Register `<API base>/api/v1/kyc/dojah/webhook` for the KYC Widget service in the Dojah dashboard and copy that webhook's **Secret** into `DOJAH_WEBHOOK_SECRET` (not the API secret key). Names only in chat and logs, never values.
- **Mobile contract change:** `POST .../sessions` no longer returns `widget_key` / `metadata.oakly_ref`, and `client-event` ignores `session_id`. Nothing in the monorepo calls them yet (K7a is a mock); K7b must pass `widget.reference_id` to the widget unchanged.
- **Roll back:** revert the branch. No data migration is involved; attempts created after the change carry `OKL-` references and would simply expire (24 h) under the old code.

## UNCONFIRMED (the K8 live-pass list; each is a code comment in `kyc_seam.py`)
| # | Unconfirmed | Where it bites |
|---|---|---|
| D1 | The dashboard "Secret" is the HMAC key and the signature is lower-case hex over the raw body | Every webhook would be `401` |
| D2/D3 | Whether Dojah retries, on what schedule, and whether a session finished on another device (QR) fires a webhook | Covered by the reconcile cron either way; latency only |
| D4 | Webhook field names (`reference_id`, `verification_status`) and no `entity` wrapper | A different name means `400`; the defensive spellings are read |
| D5 | The widget accepts our `reference_id` as given, and the pull's `entity` echoes it | If not, every pull is `bad_response` and nothing settles (visible as `error` attempts and ops noise) |
| D6 | Status words and their meaning; whether a relaunch with the same reference after `Abandoned` is allowed | A caregiver could be stuck until the 24 h expiry |
| D7 | `selfie.confidence_value` name and 0-100 scale | A missing number falls back to `selfie.status`; the grey band never triggers |
| D8 | `selfie.status` is liveness AND match in one flag | A face mismatch with a high number would be reported as `liveness_failed` |
| D9/D10 | NIN answers carry `date_of_birth` and the number; which ID types the owner's flow accepts | Missing data means `identity_data_missing` -> review |
| D11 | No watch-list signal used (`aml.status` meaning unknown) | `watchlisted` cannot occur |
| D12 | Failure `message` keywords | Falls back to `provider_failed` |
| D14 | No price in the pull; sandbox base URL; how an empty wallet is worded (a 4xx mentioning balance is treated as `wallet_empty`) | `provider_cost` stays NULL; wallet alert may arrive as `rejected` |

## Follow-ups
- K7b: pass `widget.reference_id` unchanged; pre-fill `userData` from `widget.first_name/last_name/date_of_birth`; the Expo SDK 57 spike (delta section 3).
- K8 (above), then mark the FE guide rows verified live and replace `FakeDojah` shapes with the captures.
- Log (do not enforce) a mismatch with Dojah's published source IP once the proxy chain is trusted for it.
- K7b/app: once the state shows `checking` (`awaiting_result`) do not relaunch the widget; `POST .../sessions` still returns the same reference for that attempt, so a second run under one reference would be ambiguous (and possibly billed twice). Decide in K8 whether the backend should refuse or mint a new attempt.
- Rename `identity_verifications.provider_session_id` -> `provider_reference_id` and drop the duplicate `provider_reference` when a migration is next due anyway.
- Remove `KYC_LIVENESS_FLOOR` once no env file sets it.
- Consider expiring an `Abandoned` attempt early (after a short quiet period) if K8 shows a relaunch needs a fresh reference.
