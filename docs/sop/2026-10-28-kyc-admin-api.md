# SOP: KYC admin API (K5)

| | |
|---|---|
| **Date** | 2026-10-29 |
| **Branch** | `feat/kyc-admin-api`, cut from `oakly/main` `a0bd06f` (K1-K4 and care plans B1-B4 merged). **Not pushed, no PR.** |
| **Migration** | None (Alembic head stays `0019_care_plan_admin_permissions`). |
| **Spec** | [`docs/plans/2026-10-27-caregiver-kyc-design.md`](../plans/2026-10-27-caregiver-kyc-design.md) sections 7.2, 12.3, 13, 14 (K5), 16.1. Earlier slices: [K1](2026-10-27-kyc-foundation.md), [K2](2026-10-28-kyc-caregiver-api.md), [K3](2026-10-28-kyc-webhook.md), [K4](2026-10-28-kyc-decision-engine.md). |
| **Status** | Built and tested. The attempts it shows were decided by the real engine over a **fake** provider; **nothing here has met the real Prembly**. The console (K6) builds on this. |

## What shipped

| Piece | What it does |
|---|---|
| **Identity block** on `GET /admin/caregivers/{id}` | Replaces the three always-null legacy keys with `{status, review_reason, attempts_used, attempts_max, can_reset, attempts[], duplicate_of_caregiver_id}`. Each attempt shows scores, yes/no name/DOB match, ID type, reason codes, the provider's reference and `decided_via`. No ID number, image, session id, fingerprint or provider payload. Still an audited read |
| **Queue filter and badges** on `GET /admin/caregivers` | `?identity=needs_review\|stuck\|failed` (ANDed with `profile_status`), plus `needs_review_count` and `stuck_count` on every list response |
| **`identity-decision`** (existing route) | Now also takes an optional `expected_status` (409 `IDENTITY_CASE_CHANGED` if the case moved), resolves the attempt waiting for review (`passed`/`failed`, `decided_via=admin`), closes any open attempt (`expired`), writes `identity_verification_events` rows (`admin:<id>`) and puts the attempt ids in the audit row |
| **`identity-reset`** (new) `POST /admin/caregivers/{id}/identity-reset` | `{notes}`; `failed`, or `needs_review` with `attempts_exhausted` -> `pending` with a fresh set of attempts; audited; 409 `IDENTITY_RESET_NOT_ALLOWED` from any other state |
| **Counting rule** (`kyc_caregiver.attempts_used`) | The one helper K4 left: it now skips attempts marked `admin_reset` |
| **Admin FE guide** | Section 12.14, from live captures 600-604 (and 042 re-captured because the queue response gained two counts) |

## Why

K4 sends every ambiguous case to `needs_review` and never fails anyone by itself, so a person needs somewhere to look at the case and a way to decide it (K-D3). The design (12.3, 13) gives that person the provider summary instead of images, a queue bucket and badge for the console, and a way to hand a failed or exhausted caregiver another go. K4's follow-ups also asked K5 to close the attempt an admin decides on, write the event, and compute the duplicate link.

## How: decisions and trade-offs

| Decision | Why / alternative rejected |
|---|---|
| **Reset marks every existing attempt with the reason code `admin_reset`; `attempts_used` skips marked ones** | There is no column for "attempts granted from here", and the brief says no migration and to change only the counting helper, which receives attempt rows only. Reason codes are free text (no CHECK). The attempt history, the numbering (the next attempt is `max+1`) and the unique-active index all stay as they were. Rejected: a counter column or a `reset_at` on the caregiver (migration); counting events (changes the helper's inputs and its three callers); renaming statuses (CHECKed, and would falsify history) |
| **Reset only from `failed` and from an exhausted review** (`needs_review` whose open review attempt carries `attempts_exhausted`); anything else is failed first | Design 7.2: "a failed or exhausted caregiver". Opus review: allowing any `needs_review` would let a watch-listed or duplicate case leave the queue with no verdict. Resetting a verified caregiver would silently undo a verification with no decision on record; failing first is an audited, deliberate act. `pending`/`in_progress` have nothing to reset |
| **`expected_status` on the decision (optional, additive)** | Opus review: between the admin opening a stuck `in_progress` caregiver and clicking Verify, the worker can land a `watchlisted` review, which the decision would then resolve unseen and the audit row would name as if reviewed. The console sends `identity.status`; a mismatch is a 409 and nothing changes. Omitting it keeps the old behaviour |
| **Failing a provider-passed caregiver writes an event on the passed attempt and names it (`overridden_verification_id`) in the audit row** | Design 7.2: an override of a pass is audited with the attempt id. The attempt itself stays `passed` (the evidence is what it was). **The passed attempt keeps its fingerprint**, so that NIN still counts as claimed for the duplicate check and `duplicate_of_caregiver_id`: kept on purpose as a fraud signal, since the person did pass once; revisit if the owner wants a failed caregiver to free the number |
| **The caregiver app ignores wiped attempts for `last_attempt_status`** | Opus review: after a reset the app would get `pending` with the last attempt's old status and no screen for that combination. `last_attempt_status` is now `null` until a new attempt exists |
| **Reset does not verify, approve, reinstate or touch the step** | It only hands the case back. A suspended caregiver stays suspended; approval still needs `verified`. The caregiver is not notified (the app polls; a push is a follow-up) |
| **The admin decision resolves the attempt "waiting for review"** (newest `needs_review` attempt not marked `admin_reset`) to `passed`/`failed`, keeping its reason codes | The design says the decision "closes the open `needs_review` attempt (`decided_via=admin`)"; the only terminal statuses a person can give are `passed` and `failed`. Keeping the reason codes shows what the admin overrode. The marker check stops a reset-and-retried caregiver's old review attempt being picked up by a later decision |
| **A decision also closes any still-open attempt as `expired`** | K4 follow-up. `expired` is "nothing was decided about the person": uncounted, and the engine only decides active attempts, so a late provider result cannot touch it. (K4 already refuses to move a caregiver an admin decided; this also stops the attempt lingering until the cron) |
| **An admin `verified` raises the step to at least 3** | Same effect as the engine's pass: a caregiver left on the identity step moves on. Never lowers the step |
| **The decision keeps its old contract** (any state, `notes` required, auto-suspend on fail) | Existing tests and the console rely on it. Only the attempts and events are new |
| **Same lock order as K3/K4: caregiver row, then the caregiver's attempts by id** | A webhook worker or the cron can queue behind an admin action, or the reverse, but not deadlock. A worker that wins the lock first decides; the admin then sees the result and may override it. If the admin wins, the worker finds a caregiver it may not move and writes nothing |
| **`review_reason` is one code: the most serious on the open review attempt**, and `null` unless the caregiver is `needs_review` | The design lists one `review_reason`. The order (watch list, duplicate, disagreement, missing data, exhausted, face, name, DOB) puts what needs a person's judgement first; the full list stays on the attempt |
| **`duplicate_of_caregiver_id` from the keyed fingerprint**: another caregiver's `passed` attempt with the same fingerprint; always present (null when none) | K4 follow-up. Shown after a decision too. The number itself is never available |
| **Badges are per `profile_status` tab and independent of the `identity` filter** | A chip's badge always equals the `total` its filter returns on the same tab |
| **`stuck` = pending/in_progress caregiver with an open attempt older than the session TTL + 1 hour** | The cron expires old attempts within minutes; the hour of grace means the bucket lists the ones it could not close (a provider outage), not every session crossing 24 hours |
| **Scores are JSON numbers**, not strings | The `Numeric` columns would otherwise serialise as strings; the admin console wants to compare against `face_threshold` |
| **No new permission** | Design 12.3. Read stays `caregivers.read`, so Finance, Support, Operations and the Clinical Reviewer can all see the identity block and the duplicate link. Flagged below |

## What's involved

| Area | Files |
|---|---|
| New | `app/services/kyc_admin.py` (identity block, queue conditions and counts, `apply_decision`, `apply_reset`) |
| Changed | `app/services/caregiver_approval.py` (`list_applications` filter and counts, `get_detail` identity block, `decide_identity`, new `reset_identity`), `app/services/kyc_caregiver.py` (`RESET_MARK`, `attempts_used`), `app/api/v1/endpoints/admin_caregivers.py` (`identity` param, new route), `app/schemas/admin_caregiver.py` (`IdentityOut`, `IdentityAttemptOut`, `IdentityReset`, list counts) |
| Tests | `app/tests/test_kyc_admin_api.py` (new, 53 cases), `app/tests/test_admin_rbac.py` (matrix row), `app/tests/golden/response_shapes.json` (regenerated: only `admin_list` and `admin_detail.identity`), `app/tests/test_capture_kyc_admin.py` (new, capture run only) |
| Docs | `docs/fe-integration-guide-admin.md` section 12.14 and the permission table, `docs/fe-captures/admin/` 600-604 and 042, this SOP, `docs/checklist/MASTER_CHECKLIST.md` |

Endpoints (`/api/v1/admin/caregivers`): `GET ""` (+`identity`), `GET /{id}`, `POST /{id}/identity-decision`, **`POST /{id}/identity-reset`**. Permissions: read `caregivers.read`; decide and reset `caregivers.approve`. Finance holds read only.

## Verification

| Check | Result |
|---|---|
| New tests | `test_kyc_admin_api.py`: block shape and no legacy keys, no ID number/fingerprint/session id anywhere in the response, review reason priority (8 cases), duplicate link (its own pass is not a duplicate; failed/unfinished attempts do not count), queue buckets and per-tab counts, paging, `stuck` boundaries, decision closing the review and open attempts with their event and audit rows, step rule, unchanged decision contract, a late provider result cannot move an admin-decided caregiver, reset (fresh set against the real caregiver endpoints through to the cap, audit, event, open attempt closed, review attempt handed back, 409 from `verified`/`pending`/`in_progress`, does not reinstate), two resets at once have one winner, Finance/Operations/Support/Clinical/Analyst get 403 on both writes with nothing changed, Verification and Super Admin succeed, 401 without a token, a caregiver token cannot reach them |
| RBAC matrix | `test_admin_rbac.py` has the new route; it checks every role against it |
| Golden shapes | Regenerated; the diff is only the two intended shapes |
| Capture harness | `CAPTURE_KYC_ADMIN=1` run: 600-604 are real responses; attempts decided by `process_event` over `FakePrembly`; the stuck attempt is seeded. Both `render_fe_guide --check` exit 0 |
| Opus review | One read-only pass on the reset and permission code: **no blockers**, four findings, all applied (stale-decision guard `expected_status`, reset limited to failed or exhausted, audit/event for a pass override, app state after reset). Not re-reviewed. Confirmed fine: nobody without `caregivers.approve` can reach either write; reset cannot grant more than one fresh set and a later attempt always counts; lock order holds against the worker, the cron, erasure and `start_session`; closed attempts cannot be settled by a late provider result; no session id, fingerprint, number or payload in the response or audit |
| CI parity | `poetry check --lock` ok; `ruff check --select E9,F63,F7,F82 .` ok; `scripts.ci_smoke` OK (198 operations, with CI's dummy env); both `render_fe_guide --check` exit 0; Alembic head unchanged (`0019_care_plan_admin_permissions`). Full `pytest` (under the shared lock, 45 min): 3630 passed, 7 skipped, **1 failed**: `test_kyc_engine.py::test_only_three_places_in_the_app_write_the_caregivers_identity_status` (the reset wrote the status from the new `kyc_admin.py`). Fixed by moving that one line into `caregiver_approval.py` (already a listed writer; the trust guard stays as it was). The seven affected files were re-run after the fix: 314 passed. A second full run was not made |
| **Not verified** | Anything against the real Prembly (the fields the block shows, the score scales). Whether real data fills `provider_reference`, name/DOB match and ID type. A browser or the compose stack |

## Operate / roll back

- **Deploy:** no migration, no new setting. The admin console keeps working: the identity block changed shape (legacy keys removed), which the console did not render.
- **To look at a case:** the admin opens the caregiver (audited), reads `identity.attempts`, and uses `provider_reference` in the provider's own dashboard. Oakly stores no image.
- **To see what an admin did:** `admin_audit_log` (`caregiver.identity_decision`, `caregiver.identity_reset`; `after` lists the attempt ids resolved or wiped) and `identity_verification_events` (actor `admin:<id>`; reasons `admin_decision` / `admin_reset`).
- **Undo a reset:** there is no un-reset. The `admin_reset` markers sit in `identity_verifications.reason_codes`; removing them by hand would make the old attempts count again. Do that only deliberately.
- **Roll back:** revert the branch. No migration. Rows already marked or resolved stay (the marker is just an extra reason code; `attempts_used` on the older code would count those attempts again, so a caregiver who was reset could find the cap reached after a revert).

## Follow-ups

| Item | Owner / slice |
|---|---|
| **Read access is wider than the decision.** `caregivers.read` is held by Finance, Support, Operations and the Clinical Reviewer, so all of them see the attempts, scores, reason codes (`watchlisted`, `duplicate_identity`) and the duplicate link. The design chose this (no new permission). If the owner wants the sensitive parts for Verification and Super Admin only, gate `reason_codes` / `duplicate_of_caregiver_id` on `caregivers.approve` (a code change, no migration) | owner |
| The caregiver is not told when an admin resets or decides (no push or e-mail); the app learns on its next poll. Add a coarse notice like K4's if wanted | later |
| Reset leaves `profile_completion_step` alone. A caregiver reset at step 3 or later is `pending` again: K7a must route a `pending` caregiver with `can_start` back to the identity screen whatever the step | K7a |
| `provider_reference` is `null` in every capture: the fake session carries no `verification.reference`. Whether the real Prembly sends one (and so whether an admin can look the person up from it) is unconfirmed (K3 seam) | K0 / K8 |
| No timeline of events is returned (the `identity_verification_events` rows are not exposed). Add a read if the console wants a history view | K6 / later |
| `admin_reset` and `admin_decision` are new reason/event codes outside `REASON_CODES` (not a CHECK, as for `identity_data_missing` in K4). The console shows an unknown code as it is | K6 |
| An admin may still decide any state, including `verified` -> `failed` of an approved caregiver (auto-suspend). Pre-existing; unchanged | accepted |
| The caregiver FE guide does not mention that an admin reset puts a failed caregiver back to `pending` with `can_start` true (captured in the admin guide, 603-e/h/i) | next caregiver-guide pass |
| Real-provider fields (scores, name/DOB match, ID type, `provider_reference`) are shown as the engine stored them; K0/K8 will show whether they are filled | K0 / K8 |
