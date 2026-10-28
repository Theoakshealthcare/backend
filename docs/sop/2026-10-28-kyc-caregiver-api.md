# SOP: caregiver KYC API (K2)

| | |
|---|---|
| **Date** | 2026-10-28 |
| **Branch** | `feat/kyc-caregiver-api`, based on `oakly/main` `59d4029` (K1 merged, Alembic head `0015_caregiver_kyc`). Commits: `git log 59d4029..feat/kyc-caregiver-api`. **Not pushed, no PR.** |
| **Migration** | None. |
| **Spec** | [`docs/plans/2026-10-27-caregiver-kyc-design.md`](../plans/2026-10-27-caregiver-kyc-design.md) sections 6, 7, 10.2, 12.1 and the K2 row of section 14. Owner defaults for O1 to O5 accepted. K1 SOP: [`2026-10-27-kyc-foundation.md`](2026-10-27-kyc-foundation.md). |
| **Status** | Built and tested against the K1 models and a fake provider flag. **No Prembly call exists in this slice and none was made.** The widget, webhook and decision engine are K3, K4, K7. |

## What shipped

The five caregiver endpoints of design 12.1, under `/api/v1/caregiver/identity-verification`:

| Call | What it does |
|---|---|
| `GET /` | The caregiver's state: `status`, `attempts_used` / `attempts_max`, `active_verification_id`, `last_attempt_status`, `can_start`, `failure_hint`, `provider_available`, `review_eta_hours`, `policy_version`. |
| `POST /sessions` | Body `{consent: true, policy_version}`. Records the **separate** identity consent, enforces the attempt cap and one active attempt (a repeat returns the running attempt), moves `pending` to `in_progress`, returns `{verification_id, attempt_no, widget, state}`. `201` first time, `200` on a repeat. The widget block is the public widget id and key, the caregiver's email and name split, and `metadata.oakly_ref`. **No secret is ever returned.** |
| `POST /{id}/client-event` | A widget hint (`opened`, `completed`, `closed`, `error`, optional `session_id`). Advances the caregiver's own active attempt (`created` to `started` to `awaiting_result`) and keeps the session id as a hint. **Never touches `caregivers.identity_verification_status`.** |
| `POST /defer` | The outage path ("continue without for now"): only while the provider flag says unavailable; sets `profile_completion_step` to 3; identity stays as it is, approval stays blocked. |
| `POST /result` | Now **`410 Gone`**, reads no body. `VerificationResultRequest` and `CaregiverService.process_verification_result` are deleted (K1 had left it answering 200 as a no-op). |

Also: `app/services/kyc_availability.py` (the "is Prembly up" flag, read here, written by K4), a `kyc_availability` test fixture, KYC rows in the response-shape golden file, the capture script, and the caregiver FE guide section.

## Why

K-D5: the client must not decide identity. K1 already made the old result endpoint non-authoritative but it still stored nothing and said 200, an app could keep calling it, and no endpoint existed for a screen to start an attempt, record the consent, or survive a Prembly outage (K-C3). K2 gives K7a/K7b an API to build against and removes the last client-reported path.

## How: decisions and trade-offs

| Decision | Why / alternative rejected |
|---|---|
| **No server-side Prembly call** at session creation | Design 6: the widget config is our own row plus the public widget id/key; the attempt's `reference` goes to the widget as `metadata.oakly_ref`. Calling Prembly here would add an outage dependency to a screen that can be served from our database. The K1 client is unused in K2. |
| One service, `CaregiverIdentityService` (`app/services/kyc_caregiver.py`), raising `KycError(status, code, message)`; the endpoints only translate it | Keeps the rules testable without HTTP and in one place for K4 to reuse (`state`, the active-attempt helpers). |
| Errors carry a machine `code`: `detail` is `{"code","message"}` on every identity error | The design names codes (`ATTEMPTS_EXHAUSTED`, `PROVIDER_UNAVAILABLE`); matching on prefixes of English strings (the rest of this API) is brittle. Same shape as the existing `unmet_for_approval` entries. The `403`, `401`, `410` and `422` keep their usual bodies, which the guide lists. |
| `attempts_used` counts every attempt except `expired` and `error` | Design 7.1. A closed or errored widget reuses the running attempt, so it cannot burn attempts. |
| `start_session` takes `SELECT ... FOR UPDATE` on the caregiver row; the partial unique index and `(caregiver_id, attempt_no)` unique remain the backstop | Two taps on "Start" create one attempt (tested with 4 parallel requests). Lock order: caregiver, then attempts. |
| `policy_version` must equal `settings.KYC_POLICY_VERSION`, else `409 POLICY_VERSION_MISMATCH`; the GET returns it | The append-only consent row records the version the person saw; the server must not let the client write an arbitrary string into evidence. |
| One consent row **per new attempt**, `evidence_ref` = the attempt's opaque reference; a repeat that reuses the attempt adds none | Each billed check has its own consent; the row ties to the attempt without the NIN or any provider data. |
| Sessions and defer need `profile_completion_step >= 2` | Step 1 is what the widget is pre-filled from and what K4 matches. Without the guard `defer` would let a Step 0 caregiver jump to step 3 and skip onboarding. |
| `client-event`: an attempt that is not yours and one that does not exist both give the same `404` | No oracle on attempt ids. A hint on a terminal attempt is accepted and ignored (clients retry; harmless). `closed`/`error` change nothing. Extra body fields are ignored, never stored (no client JSON, no images). |
| `provider_session_id` is UNIQUE; a hint whose id already sits on any attempt is silently not stored (savepoint + `IntegrityError`), the state hint still applies | An attacker cannot learn which session ids exist, cannot trigger a 500, and cannot bind a victim's session: K3/K4 bind from the signed `oakly_ref`, never from this hint (design 16.1 point 5). The latest hint wins while the attempt is active. |
| `provider_available` is a Redis key behind a FastAPI dependency; a Redis read error counts as **available** | An unreachable Redis must not lock caregivers out of onboarding; the sessions call then fails honestly if Prembly really is down. Tests swap the dependency for an in-memory fake. Rejected: an in-process flag (wrong with several API workers). |
| `can_start` also requires `provider_available`, and `POST /sessions` answers `503 PROVIDER_UNAVAILABLE` for an unconfigured widget (empty id or key) | A half-configured server must not hand out a half-built widget. |
| `failure_hint` only after the **latest** attempt failed cleanly: `lighting`, `document` or `mismatch_generic`; never for `needs_review` | Design 12.1 and 11.3: duplicate, watch-list, mismatch and disagreement reasons stay hidden. Scores, reasons, fingerprint and provider reference are not in any response (tested). |

### Deviations from the design doc

1. **Two extra state fields:** `last_attempt_status` (so the app can tell "try again" from "timed out, start again" from "error", states 6, 9, 11 of design 11.3) and `policy_version` (the app must send the version it showed). Both are additive.
2. **`POST /sessions` returns `{verification_id, attempt_no, widget, state}`**, and `201` vs `200` distinguishes a new attempt from a resumed one; the doc only said "returns widget config".
3. **Step guard** (`PROFILE_INCOMPLETE`, `>= 2`) and the decided-caregiver conflicts `UNDER_REVIEW` / `IDENTITY_REJECTED` are additions; the doc listed `409` for verified and exhausted only.
4. No rate limit on `POST /sessions` (design 16.1 point 12): the attempt cap bounds Prembly's cost, and edge limits are nginx's job. Noted below.

## What's involved

| Area | Files |
|---|---|
| Service | `app/services/kyc_caregiver.py` (new), `app/services/kyc_availability.py` (new), `app/services/prembly.py` (exports `SESSION_ID_PATTERN`), `app/services/caregiver.py` (`process_verification_result` deleted) |
| API | `app/api/v1/endpoints/caregiver.py` (five routes; the old one now 410), `app/schemas/caregiver.py` (`IdentityVerification*` models; `VerificationResultRequest` deleted) |
| Tests | `app/tests/test_kyc_caregiver_api.py` (new, 145 cases), `app/tests/test_access_caregiver_verification.py` (rewritten for the 410), `app/tests/test_response_shapes.py` and `golden/response_shapes.json` (`identity_state`, `identity_session`), `app/tests/fake_prembly.py` + `conftest.py` (`kyc_availability` fixture), `app/tests/test_capture_kyc_caregiver.py` (capture, skipped unless `CAPTURE_KYC_CAREGIVER=1`) |
| Docs | `docs/fe-integration-guide-caregiver.md` (section 4 "Identity verification", error catalogue, traps 11 and 12, polling, UX 12, verification table), `docs/fe-captures/caregiver/200`-`225-a-kyc-*.json`, this SOP, the checklist |

No new config: it uses the K1 names (`PREMBLY_WIDGET_ID`, `PREMBLY_WIDGET_KEY`, `KYC_MAX_ATTEMPTS`, `KYC_POLICY_VERSION`, `REDIS_URL`).

## Verification

- `test_kyc_caregiver_api.py`: IDOR (another caregiver's attempt and an unknown id give the same `404`; a patient token `403`), guards on all five routes (no token, refresh token as bearer, patient), consent required and recorded append-only with the right version/channel/evidence, stale policy version refused, cap and expired/error accounting, one-active-attempt including a 4-way parallel start, hint transitions and non-regression, terminal attempts inert, unsafe session ids `422`, session-id clash not stored and not an error, **the caregiver is unchanged by any hint whatever the body claims** (all five identity statuses x four events), defer only during an outage and unable to skip steps, a deferred caregiver still blocked from approval, the 410, no secret value in any response, no reason/score/fingerprint in any response, and a sequence test that no combination of calls verifies anyone.
- The response-shape golden file pins the state and session shapes.
- The capture run (`CAPTURE_KYC_CAREGIVER=1 poetry run pytest app/tests/test_capture_kyc_caregiver.py`, real app and Postgres) wrote 26 captures; the guide is rendered from them and `render_fe_guide --check` passes for both guides.
- **Not verified:** anything against real Prembly or its widget; the Redis flag against a real Redis (the fixture replaces it; the fail-open path is by inspection); behaviour on a device.
- Review: one read-only Opus pass on the session creation, hint handling and the 410: no blockers; fixed in K2 (the Redis flag is read once per request, `failure_hint` is only returned for `in_progress` caregivers, a docstring corrected); the rest are in Follow-ups. Full CI parity results are in the final report of the slice.

## Operate / roll back

- Deploy: no migration, no new env value required. Until K7b ships a widget nothing starts a session, so the new routes are dark. `POST /sessions` returns `503 PROVIDER_UNAVAILABLE` until `PREMBLY_WIDGET_ID` and `PREMBLY_WIDGET_KEY` are set on the server.
- Roll back: revert the commit. An older app that still calls `.../result` gets `410` with a message (the app never read the body of the old `200`). The consent and attempt rows written meanwhile stay (append-only by design) and are harmless.
- To simulate an outage by hand: set the Redis key `kyc:provider_unavailable` (any value, with a TTL); `DEL` it to clear.

## Follow-ups

| Item | Owner / slice |
|---|---|
| K4 writes the availability flag (`kyc_availability.get_provider_availability().mark_unavailable()`), sets `profile_completion_step = 3` and the caregiver status when a result arrives, and moves an exhausted caregiver to `needs_review` (today `can_start` is `false`, `status` stays `in_progress`, and the app shows the "attempts used up" row from the guide) | K4 |
| **Stale active attempts:** an attempt left in `created`/`started`/`awaiting_result` for longer than `KYC_SESSION_TTL_HOURS` is still returned by `POST /sessions` until the K4 expiry cron marks it `expired` | K4 |
| **Reset must give a fresh set of attempts** but `attempts_used` counts all non-expired/error attempts for the caregiver (`CaregiverIdentityService.state`, `start_session`). K5's `identity-reset` needs a marker (for example counting only attempts after the reset event, or marking old ones) and should change the one counting helper, not duplicate the rule | K5 |
| Lock order: K2 locks the caregiver, then touches attempts (and the attempt row alone in `client-event`). K1's follow-up asks K3/K4 to pick one order; the worker must lock the caregiver first (or use the attempt row only) to avoid a deadlock with this code | K3/K4 |
| **Opus review of K2: no blockers.** Carry-forward for K3/K4: the client's `session_id` hint is written to the UNIQUE `provider_session_id`, so someone who knows another person's session id can occupy it first. K3/K4 must never trust the hint (bind from the signed `oakly_ref`/email, as designed), and when K4 stores the confirmed id it must clear another attempt's hint rather than hit the unique constraint. Better still, move the hint to a non-unique column | K3/K4 |
| `start_session` would return a 500 if something other than K2 creates an attempt without first locking the caregiver row (K5 reset, K4); catch `IntegrityError` and re-read the active attempt when that happens, and keep "lock the caregiver before creating an attempt" as the rule | K4/K5 |
| With Redis **and** Prembly down together, the flag reads "available", so `defer` is refused (`409`) and the caregiver cannot continue; accepted (failing open is the safe direction) | accepted |
| A reused active attempt can be handed back while a worker is closing it; harmless because the app polls `GET` | accepted |
| No per-user rate limit on `POST /sessions` or `client-event`; the attempt cap bounds cost, and an edge limit is nginx's | ops |
| `defer` writes no `identity_verification_events` row (the table requires a `verification_id`); it is logged (`kyc deferred during an outage caregiver=...`) | accepted |
| Caregiver `full_name` is split on the first space for the widget; single-name caregivers send an empty `last_name`. Check against the real widget in K0/K8 | K8 |
| The captures for K3/K4-produced states are seeded rows; re-capture them against the real flow in K8 | K8 |
| Checklist counts in the snapshot were not recounted | lead |
