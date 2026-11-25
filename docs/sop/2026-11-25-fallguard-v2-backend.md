# SOP: FallGuard v2.0 backend scoring (replaces the 24-question screener)

| | |
|---|---|
| **What shipped** | Server-side FallGuard v2.0 from the handover PDF: the questionnaire as versioned JSON, a pure scoring library (gate tier, Part B score, tiers, incomplete, Urgent, flags, reasons), the PDF's recommendation catalogue, immutable versioned results (migration `0033`), and the patient endpoints rewritten on top (`/questions`, new `/config`, `/submit`, `/history`). The 24-question seed script and the admin question editor are gone. PR: see the branch `feature/fallguard-v2-scoring`. |
| **Why** | The clinical lead's answers (7 Nov) replace the 24-question score with FallGuard v2.0. The PDF says it ships `fallguard_scoring.py`, `test_fallguard_scoring.py` and `questionnaire_v2_0.json`; **the owner never had them**, so they were rebuilt from the PDF's own tables, rules and one worked example. |
| **How** | Pure functions in `app/services/fallguard_scoring.py` driven by `fallguard_data/questionnaire_v2_0.json`; the adapter `app/services/fallguard.py` turns the app's `answers` list into the library's input, maps refusals to a stable 422, stores the result whole in one transaction, and returns 503 `SCORING_UNAVAILABLE` (no tier stored or returned) if anything fails (SR-5). See Decisions. |
| **Verification** | `app/tests/test_fallguard_scoring.py` (pure; 150+ cases; the PDF's Appendix A reproduces exactly), `test_fallguard_submit.py` (API), `test_fallguard_v2_migration.py`; live captures in `docs/fe-captures/patient-fallguard/` (30 files, real HTTP against a local API and a throwaway Postgres). Details below. |

## Decisions

| Decision | Why / alternative rejected |
|---|---|
| The questionnaire is JSON released with the code, not database rows; the seed script and the admin question editor are removed (routes answer `410 fallguard_questionnaire_is_versioned`, `GET /admin/questions` stays read-only). | PDF SR-14: rules, weights and thresholds change only through a versioned release, no hot edits. This also means v2.0 loads with no manual script. Rejected: keep the DB table and seed it (a hot edit path; the v1 seed had already been found reversed in three places). |
| The scoring library has **no database and no framework imports**; it reads thresholds and points from the JSON. | PDF "pure functions, no dependencies"; a second implementation (the app's offline red flags) can then be tested with the same cases. |
| Every unclear reading is a **named constant** at the top of the module (`R1_WITHOUT_BLOOD_THINNER_RAISES_PROMPT_ATTENTION`, `TOP_FACTORS_INCLUDE_UNKNOWN_ITEMS`, `GATE_REASONS_INCLUDE_LOWER_TIER_RULES`, `RED_FLAGS_TRIGGERED_MEANS_CAUSED_URGENT`, `GATE_REASON_CODES`; `UNKNOWN_PART_B_FIRES_ENTRIES` in the recommendations module), always set to the more cautious reading. | The clinical lead's answer is then a one-line, versioned change. Rejected: guess silently. |
| Part A `answers` list stays the API shape (`question_id` + `answer`), with item ids (`"A1"`) and codes. The older fields of `/questions` and of the assessment response keep their names; everything new is additive (`result`, `recommendations`, versions). | The brief: keep shapes where possible. `question_id` is no longer a UUID (still a string); `total_score` is nullable (under 60). A v1 client cannot submit against v2.0 (its UUIDs are `fallguard_unknown_question`). |
| Result stored on `fallguard_assessments` (new nullable columns + two sealed JSON columns), with a **database trigger** that refuses an UPDATE of the score, tiers, versions, `urgent`, `incomplete`, `eligible` or `patient_id`. | PDF SR-8 (immutable results, versions stored). The sealed blobs and the AI columns are left out of the trigger on purpose: key rotation rewrites ciphertext and the optional AI text is written after the score. Rejected: a new `fg_result` table (a bigger expand migration for the same guarantee). The PDF's `fg_*` tables for consent, overrides, clinical tests, red-flag events, etc. are not part of this slice. |
| Answers and the full result are sealed (Fernet) in `answers_encrypted` and `result_encrypted`; both are in `scripts/reencrypt_health_data.py`. | PDF section 14: column-level encryption for answers and detail. Missing from the rotation script they would have been left on the old key. |
| `assessment_date` is the server's today in `HOME_TIMEZONE` (Africa/Lagos); the date of birth comes from the body, else the profile, and is stored with the answers so a later profile edit cannot change a past result. | PDF section 17. |
| Rate limit: 10 submissions per hour per patient, `429 fallguard_rate_limited`. | PDF section 17. |
| The optional Gemini summary is **off by default** (`FALLGUARD_AI_ADVICE_ENABLED`), never receives free text or the date of birth, and is returned with `ai_review_status: PENDING_CLINICIAN_REVIEW`. | The tier, reasons and advice are rule based. The PDF has no step that sends answers to an outside AI service, and a new use of data is a legal question (E4). The owner said AI text must be clinician-reviewed before launch. |
| Recommendation wording is the PDF's default English **verbatim** in `content_v2_0.json`; nothing is written or reworded by the build. | E3: clinical wording changes only through the clinical lead. |

## PDF to code mapping

| PDF | Where it is implemented |
|---|---|
| s4 item table, config rules | `fallguard_data/questionnaire_v2_0.json`; parity test `test_every_single_choice_item_has_the_pdfs_codes_and_points` and siblings |
| Max Part B = 36 | `scoring.max_part_b`; `test_thresholds_match_appendix_b_and_max_part_b_is_36` |
| s5 rule 1 eligibility, age | `calculate_age`, `score_assessment` (age < `min_age`: no score, no tier, `urgent` still decided) |
| rule 2 required answers | `_check_required_part_a` -> 422 `fallguard_missing_required` |
| rule 3 gate tier | `gate_tier` |
| rule 4 Part B total, unknown scores 1, multi-select bands | `_part_b_points` |
| rule 5 tiers 0-5 / 6-11 / 12-36 | `tier_from_score`, `scoring.intermediate_from`, `high_from` |
| rule 6 incomplete (> 3 unknown, B1 excluded, B14 NOT_SURE excluded) | `score_assessment`, `scoring.max_unknown_before_incomplete` |
| rule 7 final tier = higher of gate and score (SR-4) | `higher_tier`; 10,000-case property test |
| rule 8 urgent never changes the tier | `evaluate_red_flags` |
| rule 9 prompt attention | `score_assessment` |
| rule 10 consequence flag (missing C2 = not daily) | `score_assessment` |
| rule 11 pharmacist-review flag | `score_assessment` |
| rule 12 reasons, top factors | `gate_tier`, `score_assessment` |
| rule 13 immutability and versions (SR-8) | migration `0033` trigger, version columns |
| rule 14 override | **not built** (follow-up) |
| s6 red-flag logic | `evaluate_red_flags` (R1 and C5 table) |
| s7 recommendation catalogue | `fallguard_recommendations.applicable`, `content_v2_0.json` |
| s8 submit behaviour, 422 mapping, 503 `SCORING_UNAVAILABLE` (SR-5), rate limit | `app/services/fallguard.py`, `app/api/v1/endpoints/fallguard.py` |
| s8 `GET /config` | `GET /fallguard/config` |
| Appendix A example | `APPENDIX_A_INPUT` / `APPENDIX_A_OUTPUT` in `test_fallguard_scoring.py` (copied from the PDF) and `test_fallguard_submit.py` |
| Appendix B thresholds | `scoring` block of the JSON; parity test |
| Appendix B copy lint (SR-6) | `test_no_user_facing_string_matches_a_banned_pattern` |
| Appendix C messages | `content_v2_0.json` -> `messages` (served by `/config`) |

## Open questions for the clinician

Each is implemented as the more cautious reading behind the constant named. None changes anything in the PDF's own worked example.

1. **The three supplied-with-the-PDF files do not exist for us.** `fallguard_scoring.py`, `test_fallguard_scoring.py` (test vectors **TV1 to TV17**, 32 tests, a 10,000-case property test, a config-parity test) and `questionnaire_v2_0.json` were never supplied, and the PDF prints no vector except Appendix A. Every expected value in our tests comes from Appendix A, section 4's table or a hand derivation from a numbered rule; none is invented, but **TV1 to TV17 are not reproduced**. Please send the three files: running them against this build is the cross-check, and any disagreement is an E2.
2. **Question wording and "explain" strings are missing** (the PDF says they live in the JSON): the 35 items have `text: null` and `explain: null` (`wording_status: PENDING_CLINICAL_WORDING`); the API serves the PDF's table description as `question`. The app cannot ship real questions without them. For A1 the PDF gives no description and no look-back period (A7 says "last 4 weeks"); B3's subject is not given; A5 (unsteady), A6 (worry) and B12 (hearing) are inferred from `GATE_I_UNSTEADY`, `GATE_I_WORRY` and REC-HEARING.
3. **Red-flag items R1 to R7 are not worded anywhere.** Only R1 can be inferred (a blow to the head; it is paired with blood thinners). R2 to R7 have no meaning in the PDF, only the rule "any yes is Urgent". The Urgent screen cannot ship until they exist (SR-1).
4. **Gate-reason codes for the HIGH rules are not published**; only `GATE_I_ONE_FALL`, `GATE_I_UNSTEADY` and `GATE_I_WORRY` appear (Appendix A). Placeholders `GATE_H_TWO_FALLS`, `GATE_H_INJURY`, `GATE_H_COULD_NOT_GET_UP`, `GATE_H_FAINTED`, `GATE_H_FALL_AND_OFTEN_UNSTEADY` are used (`GATE_REASON_CODES`). Also unclear: when the gate is HIGH, should `gate_reasons` also list the INTERMEDIATE rules that fired? Implemented: no (`GATE_REASONS_INCLUDE_LOWER_TIER_RULES = False`).
5. **Section 6 vs rule 9.** Section 6 says R1 yes with C5 = 0 is "Prompt attention", but rule 9 defines the prompt-attention flag as `(A7 = 1 and A2 = 1) or A4 = 1` only. Implemented: R1 with C5 = 0 **also** sets `prompt_attention` (`R1_WITHOUT_BLOOD_THINNER_RAISES_PROMPT_ATTENTION = True`), never the tier.
6. **`red_flags_triggered`**: the items that caused Urgent, or every item answered yes? Implemented: the former (`RED_FLAGS_TRIGGERED_MEANS_CAUSED_URGENT = True`), so R1 with C5 = 0 is not listed.
7. **Top factors and unknown answers.** Rule 12 takes "the highest points above 0"; an unknown item scores 1, so it can be a top factor ("why this result: you did not answer"). Implemented: it can (`TOP_FACTORS_INCLUDE_UNKNOWN_ITEMS = True`). Is that what you want to show?
8. **Recommendation conditions on unknown answers** (`B4 >= 1`, `B13 = 1`, `B9 >= 1`, `B17` has any hazard, ...). Implemented on the item's points, so an unknown Part B answer fires the entry (`UNKNOWN_PART_B_FIRES_ENTRIES = True`), in line with SR-7. REC-AID fires when C3 is missing (a skipped item counts as 0). REC-ALCOHOL is `B16 = 2` exactly, so an unknown B16 does not fire it.
9. **A skipped B14** (unknown, not "not sure") scores 1 and counts as unknown but does **not** set the pharmacist-review flag (rule 11 names NOT_SURE only); REC-MEDS still fires through its points.
10. **Under 60.** Only `urgent` (and R1-with-C5 = 0 prompt attention) is computed; `consequence_flag`, `pharmacist_review_flag` and recommendations are false or empty. C5 is read from Part C if present, otherwise R1 yes is Urgent.
11. **Validation error codes** ("the library's code", section 8) are not published; ours are `fallguard_missing_required`, `fallguard_invalid_option`, `fallguard_unknown_question`, `fallguard_invalid_date_of_birth`, `fallguard_invalid_assessment_date`, `fallguard_invalid_payload`, plus the existing `fallguard_no_answers` and `fallguard_duplicate_question`. The PDF's error shape `{code, message, fields}` is kept inside the app's existing `{"detail": ...}` wrapper.
12. **Edges the PDF is silent on:** a duplicate code in a multi-select is rejected; an answer sent for a hidden item (A2, A3, A7 when A1 = 0; C3 when B2 is not 1) is ignored and counts as 0; whitespace-only C7 entries are not "non-empty"; C8 and C9 are validated loosely and never scored.
13. **Section 17 vs the specification.** The PDF itself flags that its safer "incomplete is provisional and never lowered" rule replaced the specification's gate-only rule and that the clinical lead must confirm at sign-off (Appendix E item 1). Implemented as the PDF says.
14. **The clinical-lead sign-off table (Appendix E) is blank**, and the per-state emergency number list is "to be supplied by operations": only the default 112 is configured. All wording in `content_v2_0.json` (recommendations, messages, welcome text with its WHO figures) is the PDF's default text, unsigned.
15. **No content version number exists in the PDF**; `2.0.0` is used for the content (and questionnaire and algorithm) version.
16. **Outside AI service.** Should answers ever go to Gemini? Off by default and flagged `PENDING_CLINICIAN_REVIEW` when on. Needs the owner and the data protection officer (E4).

## What's involved

- `app/services/fallguard_scoring.py` (new, pure), `app/services/fallguard_recommendations.py` (new, pure), `app/services/fallguard_data/{questionnaire_v2_0,content_v2_0}.json` (new).
- `app/services/fallguard.py` (rewritten: adapter, config view, 503/429 handling, optional AI), `app/api/v1/endpoints/fallguard.py`, `app/schemas/fallguard.py`.
- `app/db/models/fallguard.py`, `app/alembic/versions/0033_fallguard_v2_results.py`, `scripts/reencrypt_health_data.py`.
- `app/core/config.py` (`FALLGUARD_AI_ADVICE_ENABLED`, default false), `.env.example`, `deploy/env.example`.
- Removed: `app/scripts/seed_fallguard_questions.py`.
- Tests: `test_fallguard_scoring.py` (new), `test_fallguard_submit.py` (rewritten), `test_fallguard_v2_migration.py` (new). `scripts/capture_patient_fallguard.py` rewritten; `docs/fe-captures/patient-fallguard/` regenerated; `docs/fe-integration-guide-patient-fallguard.md` rewritten.

## Operate / roll back

- Deploy: run migration `0033` (expand only: nullable columns, a nullable `total_score`, a trigger). The old `fallguard_questions` and `fallguard_answers` tables stay, unused, until a later contract migration. Nothing needs seeding.
- **Chain:** `0033` is written on `0030_bank_name_review`. If the care-template and vitals slices merge `0031`/`0032` first, re-point `down_revision` at the last of them so `alembic heads` stays single (`test_single_head` fails otherwise).
- Roll back: `alembic downgrade 0030_bank_name_review` drops the trigger and columns and sets a NULL score (an under-60 row) to 0. The previous app build still works after a downgrade only if the 24-question rows and seed exist: none was seeded on staging, so there is nothing to restore.
- The app `v1` build cannot submit against v2.0 (its UUID question ids are refused). The patient app change is the next slice.
- Switches: `FALLGUARD_AI_ADVICE_ENABLED` (default false). There is no kill switch or version registry yet (the PDF's `fallguard.enabled` flag and `fg_version_registry` are follow-ups).

## Follow-ups (not built in this slice)

- The PDF's consent, access grants, override (`POST /assessments/{id}/override`, rule 14), Part D clinical tests, care plans, recommendation flags, `POST /red-flag-events`, check-ins and reminders, PDF export, version registry, feature flags and kill switch, go-live lock, audit log, caregiver module and admin console. The patient app screens (and a caregiver start-for-client flow) come after this backend is reviewed.
- `Idempotency-Key` on submit; the 24-hour duplicate-submission warning; `GET /assessments/{id}/result`; server-side drafts (30-day expiry); per-language strings (only English exists).
- A contract migration dropping `fallguard_questions` and `fallguard_answers`.
- A cross-check of this library against the PDF's missing `fallguard_scoring.py` and TV1 to TV17 (open question 1).
- `app/tests/test_admin_rbac.py::test_every_admin_route_is_in_the_matrix` fails in this worktree, which sits on `feature/bank-name-review`, and lists only the three `/admin/payouts/bank-reviews` routes from that branch (none of them FallGuard); this slice does not touch it.
