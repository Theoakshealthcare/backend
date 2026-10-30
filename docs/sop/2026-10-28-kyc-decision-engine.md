# SOP: KYC decision engine (K4)

> **Provider switched to Dojah on 2026-10-30 (slice KD1, [SOP](2026-10-30-kyc-dojah-adapter.md)).** The provider is Dojah. `Evidence.liveness_confidence` became `liveness_passed` (boolean) and `Policy.liveness_floor` / the `KYC_LIVENESS_FLOOR` rule are gone; `watchlisted` is never set by the seam (no Dojah signal). The decision rules, locks, advisory lock and notices are unchanged. The reconcile cron lost its "discover via the undelivered list" step (every attempt has a reference to pull). The rest of this document is the record of the original Prembly-era work.

| | |
|---|---|
| **Date** | 2026-10-28 |
| **Branch** | `feat/kyc-decision-engine`, cut from `oakly/main` `650afbc` (K1 #25, K2 #27, K3 #30 and care plans B1-B4 merged). **Not pushed, no PR.** |
| **Migration** | None (Alembic head stays `0019_care_plan_admin_permissions`). |
| **Spec** | [`docs/plans/2026-10-27-caregiver-kyc-design.md`](../plans/2026-10-27-caregiver-kyc-design.md) sections 7, 8.3, 9.1, 14 (K4), 16.1, 17. Earlier slices: [K1](2026-10-27-kyc-foundation.md), [K2](2026-10-28-kyc-caregiver-api.md), [K3](2026-10-28-kyc-webhook.md). |
| **Status** | Built and tested against the documented Prembly shapes and a fake. **Nothing here has met the real Prembly** (K0 spike and K8 live pass are ahead). The caregiver endpoints stay dark until K7b wires the widget. |

## What shipped

| Piece | What it does |
|---|---|
| **Decision rules** (`app/services/kyc_decision.py`) | A pure function `decide(evidence, profile, duplicate, disagreement, attempts_used, policy)` -> `passed` / `failed` / `needs_review` plus reason codes and the name/DOB yes-no results. It knows nothing about Prembly. |
| **Matching** (`app/services/kyc_matching.py`) | Name match (case, accents, word order, a missing middle name, initials; at least two shared words), date-of-birth parse and compare (ISO and day-first formats), the NIN/BVN keyed fingerprint. "Could not compare" is `None`, never a match. |
| **Engine** (`app/services/kyc_engine.py`) | `decide_and_apply`: applies a decision to the attempt (status, reason codes, name/DOB match, id type, fingerprint, threshold, `decided_via=provider`), the caregiver's identity status and step, and an `identity_verification_events` row; the duplicate check under an advisory lock; `close_unanswered` for expiry. |
| **Wiring** (`app/services/kyc_events.py`) | K3's locked section (`confirm_and_record`) now decides when the pulled session is final. `settle_session` is the same step for a session found without a webhook (the cron). |
| **Reconcile cron** (`app/services/kyc_reconcile.py`, `app/worker.py` `kyc_reconcile_job`) | Every 10 minutes: re-run unprocessed webhook rows, pull quiet attempts that carry a session id, bind sessions Prembly never delivered (by echoed ref only), expire attempts it knows have no result. |
| **Provider health and alert** (`kyc_availability.py`, `kyc_hooks.py`, `kyc_notify.alert_ops`) | Three outage errors in a row flag the provider unavailable for 15 minutes; an empty wallet or refused key flags at once and alerts ops. |
| **Notifications** (`app/services/kyc_notify.py`) | Push + e-mail to the caregiver after the decision commits: "verified", "try again", "under review". |
| **Seam additions** (`app/services/kyc_seam.py`) | `Observation.evidence`: the registry name, DOB, number, id type, watch-list flag and failure reason read from the pulled session (U12-U15). |
| **Lock helper** (`app/services/kyc_locks.py`) | `lock_caregiver_then_attempt` moved here so the engine, the worker and the cron share it (re-exported from `kyc_events`). |

## Why

K-D3 and K-D5: the provider's pull-confirmed result is the only evidence, failures go to the admin review queue and nothing is auto-rejected. K3 recorded what the pull said; K4 turns it into the attempt's outcome and the caregiver's status (design 7.2), with the safety rules the design asks for: a caregiver is never `verified` without a pull-confirmed pass, never `failed` by a machine, and no event ever overwrites a human decision. The owner is unsure Prembly will stay, so the engine takes plain values from the seam and holds no provider logic.

## How: decisions and trade-offs

| Decision | Why / alternative rejected |
|---|---|
| **Decide inside K3's locked section** (caregiver row, then attempt; `confirm_and_record` -> `decide_and_apply`) | The pulled verdict has no column (no migration), so it cannot be decided later from the row. One transaction also means the attempt, the caregiver, the event row and the ledger row move together or not at all. Rejected: a separate job that re-pulls (extra Prembly call and a window where the result is recorded but undecided). |
| **Precedence:** risky -> review; hard failure -> failed (retry) or review when exhausted; soft -> review; else passed | Design 7.3. A watch-list hit, a duplicate or a webhook/pull disagreement must never become a retryable failure. A hard failure wins over soft mismatches and reports only itself (a different person's face makes the name irrelevant). |
| **Missing data cannot pass:** face score, registry name or DOB absent or unreadable, or the profile has no DOB -> `needs_review` / `identity_data_missing` | The alternative is verifying someone on evidence we did not see. Cost: if real Prembly does not return the registry name/DOB/score where the seam looks (U12, U9), every caregiver lands in review until the seam is fixed. That is visible (one reason code) and safe. |
| **Name rule:** every word of the shorter name must appear in the longer, each word used once, at least two shared | Order, case, accents, a missing middle name and initials are normal; one shared word ("Amina") is too weak; a married name is a mismatch a person resolves. No fuzzy matching: a typo in Step 1 goes to review, which the design accepts. |
| **Grey-band face (70 to 85) is review even when the provider says "failed"** | Ambiguous, so a person looks. Below the floor is a clean failure even if the provider says "passed". |
| **Liveness floor** `KYC_LIVENESS_FLOOR` (default 50, placeholder): below it is a clean failure, absent is fine | The design says "liveness ok" with no pass rule (Q6). A provider pass already includes liveness in the SDK. |
| **Provider moves a caregiver only from `pending` / `in_progress`;** nothing at all is written otherwise (not even the session binding) | Design 7.2: `verified` / `failed` are human or final, `needs_review` waits for a person. The attempt stays active and is expired by the cron. |
| **Attempt stays unchanged and the pull is debounced for a decided caregiver** | A stuck attempt for a decided caregiver would otherwise be pulled every 10 minutes until it expires. |
| **Duplicate check only for a provider PASS, under `pg_advisory_xact_lock(7204101, hashtext(fingerprint))`, taken after the row locks and held to commit** | Two caregivers presenting one number at once are serialised; the second sees the first's `passed` row. Nothing waits for a row lock while holding it, so it cannot deadlock with the row-lock order. A mutation test (lock removed) fails two tests. Rejected: a unique index (needs a migration and cannot express "another caregiver, only passed rows"). |
| **No number in the payload -> no fingerprint, no duplicate check, the pass stands** | Design 9.1 says NULL fingerprint is allowed (Q1 unconfirmed). Gap listed below. |
| **Squatted hint cleared only when Prembly echoes our own `oakly_ref`** and the other holder is an active attempt with no result (SKIP LOCKED) | The K2 follow-up: a client hint is unverified, so someone who learned a session id could occupy the unique column and block the real owner. An echoed ref is the strongest binding; an email-only binding is still refused. SKIP LOCKED means this path never waits, so it cannot deadlock with the holder's own worker. |
| **Reconcile order:** lost webhook rows, quiet attempts with a session id, undelivered list, expiry; 35 s budget; first outage error stops the pulls; each row is guarded so one failure does not stop the run | The ARQ `job_timeout` is 55 s and one Prembly call can take 10 s. An idle system makes no call. Rejected: a per-attempt cron job (many ARQ jobs for little). |
| **Discovery binds a session nobody named by the echoed `oakly_ref` only, never by e-mail** | Automated security review: anyone with the public widget key can start a session under any e-mail, so an e-mail match would let a stranger's deliberately failed session use up a victim's attempts. The webhook and hint paths still use the e-mail as a second check because something named the session there (K3). If Prembly does not echo the ref (U6), discovery finds nothing and the webhook/hint path is the only route. |
| **Expiry only when the run KNOWS there is no result:** an attempt with a session id must have been pulled and answered this run; one without only after discovery ran to its end; never during an outage. `error` (not `expired`) when Prembly refused or could not find its session | Opus review: expiring on age alone would throw away a billed result when the provider was down, the budget ran out or the debounce was still running. Design 7.3: attempts stay `awaiting_result` during an outage. Neither status counts toward the cap or touches the caregiver. |
| **Health through injected `KycHooks`** (inert by default, `production()` in the ARQ jobs) | Unit tests need no Redis, Expo or SMTP; the production wiring is covered by a test. A forgotten hook would silently lose notifications, hence the explicit wiring tests. |
| **Ops alert:** one per kind per hour (Redis `SET NX EX`), ERROR log always, e-mail to `KYC_ALERT_EMAIL` when set | Owner question O5 (who is told) is still open; with no address the existing ERROR log line is the alert. A throttled alert beats one per pull. |
| **Step 3 set only for `verified` and `needs_review`** | Design 6 step 6: "try again" keeps the caregiver on the identity step. `max(step, 3)`: never moves a caregiver back. |
| **Notices are sent after the transaction commits and never carry a reason** | A decision that rolled back must not notify; a reason would tell a fraudster what tripped. |

### Deviations from the design doc
1. The doc lists the decision as worker step 4 inside the webhook flow only; the cron reaches the same code through `settle_session`, with no webhook claim to compare.
2. New reason code `identity_data_missing` (not in the doc's list; the set is not a CHECK).
3. Three failed attempts end in `needs_review` for the caregiver, so K2's `ATTEMPTS_EXHAUSTED` state is no longer reached by the engine (kept as a defensive 409; the FE guide says so).
4. New settings `KYC_LIVENESS_FLOOR` and `KYC_ALERT_EMAIL` (design 9.3 lists neither).

## UNCONFIRMED seams added by K4 (all in `app/services/kyc_seam.py`; see also U1-U11 in the [K3 SOP](2026-10-28-kyc-webhook.md))

| # | Assumption | If wrong |
|---|---|---|
| U12 | The registry's name, date of birth and ID number are in the pull's `payload.verification_response` (`full_name` or first/middle/last, `birthdate` / `dateOfBirth` / `dob`, `number`). The session's own top-level `full_name` is NOT used (it is what the widget was pre-filled with) | Every pass goes to review (`identity_data_missing`) until `_registry_name`, `_registry_dob` and the `number` read are fixed |
| U13 | `payload.id_type` (or `verification_response.id_type`) is one of our five types | `id_type` stays NULL; the fingerprint uses `unknown:` as its type prefix (a NIN and a BVN with the same digits would then collide) |
| U14 | `verification_response.watchListed` is `true` for a watch-listed person | No watch-list escalation from the payload (a `07` code on the pull is still logged by the client) |
| U15 | `failure_reason` is free text; a few keywords map to our codes | Clean failures are all `provider_failed` and the "try again" hint is the generic one |
| (K3 U9) | Face score 0-100, liveness 0-1, dropped when out of range | A dropped face score now means `identity_data_missing` for a PASS, so a wrong scale sends everyone to review rather than passing anyone |

## Operate / roll back

- **Deploy:** no migration. The worker now registers `kyc_reconcile_job` (every 10 minutes). Optional new env: `KYC_LIVENESS_FLOOR` (default 50), `KYC_ALERT_EMAIL` (default empty = log only). The production boot refusal (three keys) from K3 is unchanged. Nothing starts an attempt until K7b ships the widget, so the engine is dark in production.
- **Read the state:** `identity_verification_events` is the audit trail of system decisions (actor `provider` for a webhook, `reconcile` for the cron). An attempt `needs_review` shows its reasons in `identity_verifications.reason_codes`. A ledger row with `processed_at IS NULL` and an old `received_at` is picked up by the cron; `outcome = 'error'` means the pull failed for good.
- **Simulate an outage by hand:** the Redis keys `kyc:provider_unavailable` (flag), `kyc:provider_errors` (streak) and `kyc:alert:<kind>` (alert throttle); `DEL` to clear.
- **Roll back:** revert the branch. No migration; decided attempts and their events stay (append-only by design) and are inert.

## What's involved

| Area | Files |
|---|---|
| Rules, matching, engine | `app/services/kyc_decision.py`, `kyc_matching.py`, `kyc_engine.py`, `kyc_locks.py` (new) |
| Wiring | `app/services/kyc_events.py` (decides in the locked section; `settle_session`; `_claim_session`), `kyc_reconcile.py` (new), `kyc_hooks.py` (new), `kyc_notify.py` (new), `kyc_availability.py` (error streak, alert throttle), `kyc_seam.py` (evidence), `kyc_caregiver.py` (`attempts_used` helper), `app/worker.py` (job, cron, production hooks) |
| Config | `app/core/config.py` (`KYC_LIVENESS_FLOOR`, `KYC_ALERT_EMAIL`), `.env.example` |
| Tests | `app/tests/test_kyc_decision.py`, `test_kyc_engine.py`, `test_kyc_reconcile.py`, `test_kyc_trust.py` (new); `test_kyc_events.py` (adapted: a final pull now decides); `fake_prembly.py` (`script_clean_pass`, registry fields, health/alert fake); `test_capture_kyc_caregiver.py` |
| Docs | `docs/fe-integration-guide-caregiver.md` (identity section, verification table), `docs/fe-captures/caregiver/` (226-231 new; 210, 213, 215 engine-driven), this SOP, the checklist |

## Verification

| Check | Result |
|---|---|
| Rules | `test_kyc_decision.py`: name/DOB/fingerprint tables, every decision case, 4,000-case seeded fuzz (a pass needs every check, the caregiver is never `failed`, every outcome reached) |
| Engine against Postgres | `test_kyc_engine.py`: pass, clean failure, three failures end in review, expired/error attempts uncounted, each ambiguity to review with its reason, watch list, duplicate NIN across two accounts (and not across types, not against unfinished attempts), human decisions untouched (`verified`, `failed`, `needs_review`), an admin decision that lands while the worker waits for the lock wins, forged/unbound sessions decide nothing, squatted hints, no registry data in any column of any table or any log line (sentinel scan), provider flag and wallet alert, notices coarse and non-raising, a structural test that only three places write the caregiver's identity status |
| Cron | `test_kyc_reconcile.py`: all four steps, debounce, budget, outage stops the run, wallet alert, expiry before a late result is decided, ARQ registration and production wiring |
| Forced races | `test_kyc_trust.py`: webhook job vs cron on one attempt decide exactly once (4 rounds, barrier); two accounts one NIN at once: exactly one passes (5 rounds); a third session holds the number's advisory lock and both deciders are seen queued on it; different numbers do not wait |
| Property | `test_kyc_trust.py`: 4 seeds x 3 sequences x 10 random steps (events, forged events, replays, reconciles, expiries, admin decisions, new attempts) with five invariants checked after every step: never `verified` without a passing session scripted for that caregiver or an admin; `failed` only via an admin; an admin's word is final; a passed attempt passed every check; one identity never passed on two accounts |
| Mutation checks | Removing the human-decision guard, the advisory lock, or the ownership check each fails tests |
| Opus review | One read-only pass walking design 16.1 on approval trust and PII: **APPROVE WITH FIXES, no blockers.** Applied: expiry only when the run knows there is no result, a flush guard on the session binding, per-row guards in the cron, no restamping of a closed ledger row, one provider error counted per event, initials-only names never match, and the commit/doc items. The rest are in Follow-ups. Not re-reviewed |
| Capture harness | `CAPTURE_KYC_CAREGIVER=1` run: 226-231 and the engine-driven 210/213/215 are real engine output over the fake; both `render_fe_guide --check` exit 0 |
| CI parity and full suite | On the final code head (`6faadd4`): `poetry check --lock` ok; `ruff check --select E9,F63,F7,F82 .` ok; `scripts.ci_smoke` OK (197 operations, with CI's dummy env); both `render_fe_guide --check` exit 0; full `pytest`: **3577 passed, 6 skipped** (34m02s, under the shared lock, lock released). Alembic head unchanged: `0019_care_plan_admin_permissions` |
| **Not verified** | Anything against real Prembly (U1-U15). Redis calls are exercised with a fake client only; no real Redis, ARQ, Expo push or SMTP round trip. The decision thresholds (face 85/70, liveness 50) are placeholders until K0 |

## Follow-ups

| Item | Owner / slice |
|---|---|
| **K5** must close an open attempt when an admin decides, write the event row, and make `identity-reset` give a fresh set: change `kyc_caregiver.attempts_used` (the one counting helper). K5 should also compute `duplicate_of_caregiver_id` from the fingerprint (a `passed` attempt of another caregiver with the same fingerprint) | K5 |
| An active attempt of a caregiver an admin decided stays active until the cron expires it (no event row is written for that). K5's decision handler should close it | K5 |
| `identity_data_missing` for every pass if the real payload does not carry the registry name/DOB/score where U12/U9 look; first K0 capture settles it, then fix `_registry_name` / `_registry_dob` / the score reads in the seam | K0 / K8 |
| No NIN/BVN number in the payload means no duplicate check (design 9.1); decide with the owner whether a pass without a fingerprint should go to review instead | owner O1 |
| The undelivered-list drain re-reads the first 3 pages every run and nothing acknowledges them: sessions beyond page 3 are never seen and unmatched sessions are pulled again every 10 minutes. Keep a cursor or remember seen ids (Redis, TTL) once real volumes are known | K8 |
| An e-mail-only binding (webhook or hint path, U6 unconfirmed) rests on a value whoever opens the widget chooses, and the session time is not compared with the attempt's (an old unbound pass could decide a new attempt after an admin reset). Once Q2 settles the echo: require the echoed ref, or compare `started_at` with the attempt's `created_at` (timezone of Prembly's timestamps unknown), or send e-mail-only binds to review | K0 / K8 |
| A forged webhook claim that contradicts the pull sends a real pass to `needs_review` (design 7.3, only reachable by someone who knows the session id and, under U1, holds the public signing key). Spec-conformant and a person resolves it; once Q4 confirms a secret signing key keep it, otherwise consider recording the disagreement without forcing review | K0 / owner |
| `record_success` clears the provider flag on any good pull, including a wallet-empty flag another path set; the alert throttle is its own key so the alert stays hourly | accepted |
| A missing liveness score and a missing ID number still allow a pass (documented above); `/auth/profile` still lets a caregiver change `date_of_birth` after being verified (outside K4) | owner O1 / later |
| `KYC_ALERT_EMAIL` is empty by default: until the owner names a recipient (O5) the wallet-empty alert is an ERROR log line only | owner O5 |
| Notifications are best effort and not retried: a crash between the commit and the send loses one (the app polls). An outbox would fix it if it matters | later |
| The Redis counter is incremented per failed pull, including the K3 ARQ retries of one event, so one event retried five times can flag the provider by itself. Acceptable (a provider failing five pulls in a row is down); revisit with real retry numbers | K8 |
| A watch-listed `07` answer on the pull is still a ledger-only `error` (K3), not a `watchlisted` review: the attempt is not proven to own that session. A `watchListed` flag in a pulled, bound payload does reach review (U14) | accepted |
| Face threshold / liveness floor are placeholders (Q6) | K0 |
| `datetime.utcnow()` is used for consistency with the codebase (deprecation warnings only) | accepted |
