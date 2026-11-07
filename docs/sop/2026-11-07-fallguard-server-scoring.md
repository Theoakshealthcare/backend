# SOP: FallGuard scores on the server, bad submissions are a 422

| | |
|---|---|
| **What shipped** | `POST /api/v1/fallguard/submit` now looks every answer up in the stored questionnaire and computes `total_score` itself. The `score` the client sends is accepted (older patient builds still send it) and **ignored**. An empty list, a repeated question, an unknown or inactive question and an answer that is not one of the question's options are each a `422` with a stable `detail.code`, never a 500. |
| **Branch / commits** | `feature/fallguard-score-payment-link` (cut from `oakly/main` `470bb3a`), the FallGuard commit. Not pushed; the lead opens the PR. |
| **Migration** | None. |
| **FE guide** | [`docs/fe-integration-guide-patient-fallguard.md`](../fe-integration-guide-patient-fallguard.md), every example rendered from [`docs/fe-captures/patient-fallguard/`](../fe-captures/patient-fallguard/) (`scripts/capture_patient_fallguard.py`). |

## Why
Found while wiring the new patient app against the live API (patient-wiring-b): the server summed whatever scores the client sent, so a tampered `score: 99` was stored and kept as the assessment's `total_score`; an empty answer list was accepted as a score of 0; an unknown `question_id` hit a foreign-key error and returned a 500. The score feeds the AI summary and the history shown to the family, so it must not be client-controlled.

## How
| Decision | Why / alternative rejected |
|---|---|
| `FallGuardService.score_answers` resolves each answer: the question must exist **and be active**, and `answer` must equal (after `.strip()`) the `text` of one of its stored options; the stored option's `score` is used and the stored option text is what is saved (encrypted) as the answer. | Server-owned truth. Rejected: re-computing from the client's score and "checking" it, which keeps a trusted-client shape. |
| Exact text match, no fuzzy matching. | The app only ever sends text it received from `GET /questions`. A hyphen for the en dash in `65–74` is a 422, on purpose: silent guessing would score the wrong option. |
| Errors are `detail = {"code", "message"[, "question_id"]}` (the convention of the cancel and KYC endpoints): `fallguard_no_answers`, `fallguard_duplicate_question`, `fallguard_unknown_question`, `fallguard_invalid_option`. All four are 422. A malformed body (missing `question_id`) stays FastAPI's own 422 with a *list* `detail`. | Stable codes the app can switch on. |
| `score` stays in the request model as `Optional[int] = None`, documented as ignored. | The shipped patient clients send it and previously required it; dropping the field would break nothing, but keeping it accepted costs nothing and avoids a client change. It is optional now so the next app build can stop sending it. |
| The assessment and its answers are written in **one transaction** (`flush`, then a single `commit`). Before, the assessment was committed first and its answers second. | A failure between the two could leave a scored assessment with no answers. |
| Validation happens before anything is written, so one bad answer rejects the whole submission and stores nothing. | |
| Completeness is **not** required: a submission may answer fewer than all active questions. | The brief did not ask for it and the shipped app always sends all of them; see Follow-ups. |
| The AI summary path is untouched except that it is now told the server's score and the canonical answers. It stays optional: without a Gemini key `analyze_fallguard_assessment` returns `Unable to analyze` and `[]`. | |

## Seeded values (reported, **not changed**)
`app/scripts/seed_fallguard_questions.py` (24 questions, maximum possible total 40). The designs' Q23/Q24 suspicion is correct, and a third question has the same shape:

| Q | Question | Stored option scores | Reading |
|---|---|---|---|
| 23 | Do you eat meals with protein, vegetables and carbohydrates at least twice daily? | `Yes` **1**, `No` **0** | Reversed: the healthy answer adds risk. |
| 24 | How often do you drink alcohol? | `Never` **1**, occasionally, regularly and daily all **0** | Reversed: never drinking adds risk, daily drinking adds none. |
| 21 | Do you take medications that make you feel drowsy or dizzy? | `Daily` **0**, `Occasionally` 1, `Rarely or never` 1 | Reversed (found while reading the seed): daily drowsy medication scores lowest. |

Other oddities, also unchanged: Q10 has two overlapping top options (`Two or more conditions` and `Three or more conditions`, both 2); Q16, Q17, Q20 and Q22 give the same score to their last two options. With server-side scoring these values now decide every total, so **the owner (clinical lead) needs to confirm the intended values before the test build is relied on**. Changing them is a data change (edit the rows through `PUT /fallguard/admin/question/{id}` with `fallguard.write`, and the seed file for new environments); no code change is needed, and old assessments keep the scores stored with them.

## What's involved
| Path | Change |
|---|---|
| `app/services/fallguard.py` | `FallGuardSubmissionError`, `score_answers`, single-transaction `submit_assessment` |
| `app/schemas/fallguard.py` | `FallGuardAnswerInput` (score optional, ignored); `FallGuardAssessmentCreate` uses it |
| `app/api/v1/endpoints/fallguard.py` | maps the submission error to a 422 object; documents the codes |
| `app/tests/test_fallguard_submit.py` (new) | tests, including every seeded option round-tripping |
| `scripts/capture_patient_fallguard.py` (new), `docs/fe-captures/patient-fallguard/`, the FE guide | live evidence |

## Verification
- **Tests** (`app/tests/test_fallguard_submit.py`): the total is the sum of stored option scores; a client score of 99, -50 or 10^9 changes nothing (total, stored answer and the score passed to the summary all use the server's value); the `score` field may be omitted; empty list, duplicate question, unknown question, inactive question, an option text that is wrong, empty, differently cased or uses a hyphen: all 422 with the stable code and **nothing stored**; one bad answer rejects the whole submission; whitespace around an answer is tolerated and the canonical text is stored; malformed body; history shows the server-computed result; the summary falling back still stores the score; caregiver and anonymous callers refused; **every option of all 24 seeded questions** submits and scores exactly its seeded value, even with `score: 99` sent. Gemini is the in-file fake; the existing `test_gemini_service` is unchanged.
- **Neighbour modules re-run** (touch FallGuard, the Gemini service or the response shapes): `test_admin_rbac`, `test_secrets_encryption`, `test_gemini_service`, `test_response_shapes` and 20 more modules in one 896-test run: all pass. No Opus review: no money or auth code changed.
- **Live**: 15 captures against the running API (Postgres, uvicorn from this checkout; see the FE guide's verification table). The AI summary was **not** exercised live (no Gemini key in the scratch stack); `ai_summary` in the captures is the service's own fallback.

## Operate / roll back
Nothing to run or configure. Roll back by reverting the commit: the endpoint trusts the client's score again. Assessments stored by the new code are valid under the old code too (same columns).

## Follow-ups
- **Owner decision:** confirm or correct the seeded option values (Q21, Q23, Q24 above; Q10 overlap).
- No completeness rule: answering one question stores a low total. If partial submissions should be refused, require every active question (small change in `score_answers`; the FE guide would need a new code).
- There is no risk band server-side (the app derives its band from the total); if the band is to be authoritative it belongs next to `score_answers`, with the thresholds agreed with the owner.
- The seed script is not idempotent (running it twice duplicates the questionnaire) and two copies exist (`app/scripts/` and `scripts/`, differing only by `load_dotenv`); `GET /questions` would then return 48 questions.
- Admin question create/update still trusts the options it is given (`score` is any integer) and `update_question` raises a bare `Exception` for an unknown id (500); the admin question editor (AD-FR-68) is the place to harden it.
