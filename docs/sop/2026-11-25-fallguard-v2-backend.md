# SOP: FallGuard v2.0 backend scoring (replaces the 24-question screener)

| | |
|---|---|
| **What shipped** | Server-side FallGuard v2.0 from the handover PDF: the questionnaire as versioned JSON, a pure scoring library (gate tier, Part B score, tiers, incomplete, Urgent, flags, reasons), the PDF's recommendation catalogue, immutable versioned results (migration `0033`), and the patient endpoints rewritten on top (`/questions`, new `/config`, `/submit`, `/history`). The 24-question seed script and the admin question editor are gone. PR: see the branch `feature/fallguard-v2-scoring`. |
| **Why** | The clinical lead's answers (7 Nov) replace the 24-question score with FallGuard v2.0. The PDF says it ships `fallguard_scoring.py`, `test_fallguard_scoring.py` and `questionnaire_v2_0.json`; the owner did not have them when this was first built, so a first version was rebuilt from the PDF's tables, rules and one worked example. **The owner then supplied the package (`fallguard_handover_package/`: README, `fallguard_scoring.py`, `test_fallguard_scoring.py`, `questionnaire_v2_0.json`), and this PR now vendors it unmodified**; our own re-implementation was deleted. |
| **How** | `app/services/fallguard_scoring.py` and `fallguard_data/questionnaire_v2_0.json` are the supplied files, byte for byte (`cmp` against the package). The adapter `app/services/fallguard.py` turns the app's `answers` list into the library's input, maps refusals to a stable 422, stores the result whole in one transaction, and returns 503 `SCORING_UNAVAILABLE` (no tier stored or returned) if anything fails (SR-5). See Decisions. |
| **Verification** | The package's own 32 tests (TV1-TV17, regression, 10,000-case properties, config parity) run as `app/tests/test_fallguard_scoring.py`; our hand-derived rule tests are `test_fallguard_rules.py` (Appendix A reproduces exactly) and `test_fallguard_recommendations.py`; `test_fallguard_submit.py` (API), `test_fallguard_v2_migration.py`; live captures in `docs/fe-captures/patient-fallguard/` (32 files, real HTTP against a local API and a throwaway Postgres). |
| **Precedence** | The package README applies: safety requirements SR-1 to SR-16, then the handover PDF, then the specification, then developer judgement. Where the reference code and a hand-derived expectation of ours disagreed, the reference won. |

## Decisions

| Decision | Why / alternative rejected |
|---|---|
| The reference `fallguard_scoring.py` and `questionnaire_v2_0.json` are **vendored unmodified**; the app's code adapts around them. | The package says the code "follows these rules exactly" and the PDF says the Python reference and tests define correct behaviour. Any edit would break the parity guarantee. Rejected: keep our own re-implementation (two sources of truth). `cmp` against `fallguard_handover_package/` is the check that they have not drifted. |
| The questionnaire is JSON released with the code, not database rows; the seed script and the admin question editor are removed (routes answer `410 fallguard_questionnaire_is_versioned`, `GET /admin/questions` stays read-only). | PDF SR-14: no hot edits. v2.0 loads with no manual script. |
| The adapter (`app/services/fallguard.py`) closes the reference's documented gaps: unknown ids and B1 are refused, red-flag answers must be booleans (the library only counts `true`, so `"yes"` would silently mean no), Part C and multi-select shapes are checked, a future date of birth is refused (the library would read a negative age as "under 60"), a non-dict body never reaches it. Library refusals are returned with the **library's own code** (`INVALID_ANSWER`, `EXCLUSIVE_ANSWER`, `MISSING_REQUIRED`, `INVALID_DATE`, `INVALID_RED_FLAG`), as PDF section 8 says. | Without these a bad body would be mis-scored or a 503. The library names only the first problem, so the API does too. |
| `app/services/fallguard_questionnaire.py` serves the supplied JSON (English, falling back per string) as the older `/questions` shape plus additive fields, and `/config`. `content_v2_0.json` (recommendation catalogue, Appendix C messages, welcome text) stays because the supplied JSON has no equivalent. | The supplied JSON has wording, labels, `explain` and R1-R7 but no recommendations or messages. |
| Result stored on `fallguard_assessments` (new nullable columns + two sealed JSON columns), with a **database trigger** that refuses an UPDATE of the score, tiers, versions, `urgent`, `incomplete`, `eligible` or `patient_id`. | PDF SR-8. The sealed blobs and the AI columns are left out on purpose (key rotation, late AI text). Rejected: a new `fg_result` table. The data model of the real files needed no column change, so `0033` is unchanged. |
| Answers and the full result are sealed (Fernet) in `answers_encrypted` and `result_encrypted`; both are in `scripts/reencrypt_health_data.py`. | PDF section 14. |
| `assessment_date` is the server's today in `HOME_TIMEZONE` (Africa/Lagos); the date of birth is stored with the answers. 10 submissions per hour per patient (429). 503 `SCORING_UNAVAILABLE` stores and returns no tier (SR-5). | PDF section 17 and SR-5. |
| The optional Gemini summary is **off by default** (`FALLGUARD_AI_ADVICE_ENABLED`), never gets free text or the date of birth, and is returned with `ai_review_status: PENDING_CLINICIAN_REVIEW`. | The PDF has no step that sends answers to an outside AI service (E4); the owner said AI text is clinician-reviewed. |
| Recommendation wording is the PDF's default English verbatim in `content_v2_0.json`. The catalogue is the only scoring-adjacent logic we own; its "fires when" conditions read item **points**, so an unknown Part B answer fires its entry (`UNKNOWN_PART_B_FIRES_ENTRIES`). | E3: wording changes only through the clinical lead; SR-7. |

## What changed when the reference arrived

Our first, hand-derived tests were written against our own re-implementation. Run against the reference, these expectations changed (the reference won each time):

| Was | Is (reference) |
|---|---|
| Gate reason codes for HIGH were placeholders (`GATE_H_TWO_FALLS`, `GATE_H_FAINTED`, ...). | `GATE_H_FALLS_2PLUS`, `GATE_H_INJURY`, `GATE_H_COULD_NOT_GET_UP`, `GATE_H_FAINTING`, `GATE_H_FALL_PLUS_OFTEN_UNSTEADY`; a HIGH gate lists only the HIGH rules that fired. |
| `red_flags_triggered` held only the items that caused Urgent. | It holds every item answered yes, so R1 with C5 = 0 is listed while `urgent` is false. |
| Red-flag answers `"yes"` / `"no"` were accepted and an unknown id or bad value raised. | The library counts only `True` (`"yes"` silently counts as no); the **API layer** now refuses non-booleans. An unknown red-flag id is `INVALID_RED_FLAG`. |
| The library named every missing required item. | `MISSING_REQUIRED` names the first one only. |
| Error codes were `fallguard_*`. | The library's codes (`INVALID_ANSWER`, ...) for library refusals; `fallguard_*` only for API-layer refusals. |
| Under 60 returned the full key set with nulls. | Fewer keys (`referral_message_key`, no `gate_reasons`, `part_b_points`, ...). |
| Unknown top-level keys, `B1`, `B20`, items in the wrong part and Part C values were refused by the library. | The library ignores them; the API layer refuses unknown ids and B1, and checks Part C and multi-select shapes. A future date of birth reads as "under 60" in the library and is refused by the API layer. |
| `min_age` etc. lived in our own JSON block. | `score_config` in the supplied JSON. |
| 35 items (A1 to C9). | 34: the supplied JSON has no C9, although the PDF's table lists it as "auto". |

Everything else our hand-derived tests expected (Appendix A, every tier cut-off, unknown scoring, incomplete, flags, top factors, age bands) agreed with the reference without change.

## PDF to code mapping

| PDF | Where it is implemented |
|---|---|
| s4 item table, config rules | `fallguard_data/questionnaire_v2_0.json` (supplied); the package's parity test and `test_the_supplied_questionnaire_has_the_pdfs_items_codes_and_points` |
| Max Part B = 36 | the package's `test_max_points` and `test_tv10_max_score_36` |
| s5 rules 1 to 12 | `fallguard_scoring.score_assessment` and its helpers (reference); `test_fallguard_scoring.py` (TV1-TV17) and `test_fallguard_rules.py` per rule |
| rule 13 immutability and versions (SR-8) | migration `0033` trigger, version columns |
| rule 14 override | `fallguard_scoring.apply_override` is vendored and tested; **no endpoint yet** (follow-up) |
| s6 red-flag logic | `fallguard_scoring.evaluate_red_flags`; the API layer enforces booleans |
| s7 recommendation catalogue | `fallguard_recommendations.applicable`, `content_v2_0.json`; `test_fallguard_recommendations.py` |
| s8 submit behaviour, error mapping, 503 (SR-5), rate limit, `GET /config` | `app/services/fallguard.py`, `app/api/v1/endpoints/fallguard.py`, `fallguard_questionnaire.py` |
| Appendix A example | `APPENDIX_A_INPUT` / `APPENDIX_A_OUTPUT` in `test_fallguard_rules.py` (copied from the PDF), reproduced exactly by the reference |
| Appendix B thresholds | `score_config` in the supplied JSON |
| Appendix B copy lint (SR-6) | `test_no_user_facing_string_matches_a_banned_pattern` |
| Appendix C messages | `content_v2_0.json` -> `messages` |

## Open questions for the clinician

The files answered the first group; the second group is what is still open.

**Answered by the supplied package (no longer open):** the three missing files; question wording and `explain` strings (Part B only has `explain`); R1 to R7; the HIGH gate-reason codes; whether R1 with C5 = 0 sets prompt attention (yes); what `red_flags_triggered` means (every item yes); unknown items can be top factors (yes); a skipped B14 does not set the pharmacist flag; under-60 behaviour; validation error codes; A1's look-back (12 months); hidden-item answers are ignored; whitespace C7 entries are empty; incomplete results are provisional and never lowered (README).

**Still open:**

1. **Appendix E sign-off is blank.** The clinical lead has confirmed none of: the gate rules and tier floor, R1-R7 and the Urgent logic, Part B items and cut-offs, the recommendation and message wording, the service targets, the state emergency numbers, the welcome text. Every string is `PENDING_CLINICAL_SIGN_OFF`. Not launchable until signed (go-live lock is not built either).
2. **No state emergency-number list** ("to be supplied by operations"): only the default `112` is configured.
3. **No sentence for each gate-reason code** and no "explain" for the gate reasons: the package gives `explain` for Part B items only, yet the PDF's result screen says gate reasons are shown "in words". The app needs a sentence per code.
4. **The PDF table lists `C9` (completer role, language, mode, "auto")**, the supplied JSON has no C9. We follow the JSON (34 items); the assessment record that would carry it is a later slice.
5. **Recommendation conditions on unknown answers** (`B4 >= 1`, `B13 = 1`, ...): read on points, so an unknown answer fires its entry (`UNKNOWN_PART_B_FIRES_ENTRIES = True`); REC-AID fires when C3 is missing; REC-ALCOHOL needs `B16 = 2` exactly. The supplied files do not cover the catalogue.
6. **No content version number** exists; `2.0.0` is used (and the questionnaire and algorithm versions come from the files).
7. **Outside AI service.** Should answers ever go to Gemini? Off by default, flagged `PENDING_CLINICIAN_REVIEW` when on. Needs the owner and the data protection officer (E4).
8. **Where the reference is looser than the PDF (closed in the API layer, for the clinical lead to know):** the library counts only a JSON `true` as a red-flag yes (the PDF says each is "yes or no"); it never checks Part C, unknown ids, a future date of birth, or duplicate codes in a multi list (duplicates count once); and a skipped B14 scores 1 and counts as unknown but does not raise the pharmacist flag. We kept the reference's behaviour except where it could silently under-triage (red flags) or mis-score (future date of birth), which the API layer refuses.
9. **The reference lists override reason codes** (`RECENT_FALL_NOT_CAPTURED`, ... `OTHER`) that the PDF's section 8 does not list; confirm them before the override endpoint is built.

## What's involved

- `app/services/fallguard_scoring.py` and `app/services/fallguard_data/questionnaire_v2_0.json` (the supplied files, unmodified), `app/services/fallguard_questionnaire.py` (new: serves the JSON and the config), `app/services/fallguard_recommendations.py` (new, pure), `app/services/fallguard_data/content_v2_0.json` (new).
- `app/services/fallguard.py` (rewritten: adapter, config view, 503/429 handling, optional AI), `app/api/v1/endpoints/fallguard.py`, `app/schemas/fallguard.py`.
- `app/db/models/fallguard.py`, `app/alembic/versions/0033_fallguard_v2_results.py`, `scripts/reencrypt_health_data.py`.
- `app/core/config.py` (`FALLGUARD_AI_ADVICE_ENABLED`, default false), `.env.example`, `deploy/env.example`.
- Removed: `app/scripts/seed_fallguard_questions.py`.
- Tests: `test_fallguard_scoring.py` (the package's own suite, two lines changed: import and JSON folder), `test_fallguard_rules.py` and `test_fallguard_recommendations.py` (new, hand-derived), `test_fallguard_submit.py` (rewritten), `test_fallguard_v2_migration.py` (new). `scripts/capture_patient_fallguard.py` rewritten; `docs/fe-captures/patient-fallguard/` regenerated; `docs/fe-integration-guide-patient-fallguard.md` rewritten.

## Operate / roll back

- Deploy: run migration `0033` (expand only: nullable columns, a nullable `total_score`, a trigger). The old `fallguard_questions` and `fallguard_answers` tables stay, unused, until a later contract migration. Nothing needs seeding.
- **Chain:** `0033` follows `0031_care_templates_docs_space` (the vitals-flags slice added no migration). If another migration merges first, re-point `down_revision` at it so `alembic heads` stays single (`test_single_head` fails otherwise).
- Roll back: `alembic downgrade 0031_care_templates_docs_space` drops the trigger and columns and sets a NULL score (an under-60 row) to 0. The previous app build still works after a downgrade only if the 24-question rows and seed exist: none was seeded on staging, so there is nothing to restore.
- The app `v1` build cannot submit against v2.0 (its UUID question ids are refused). The patient app change is the next slice.
- Switches: `FALLGUARD_AI_ADVICE_ENABLED` (default false). There is no kill switch or version registry yet (the PDF's `fallguard.enabled` flag and `fg_version_registry` are follow-ups).

## Follow-ups (not built in this slice)

- The PDF's consent, access grants, override (`POST /assessments/{id}/override`, rule 14), Part D clinical tests, care plans, recommendation flags, `POST /red-flag-events`, check-ins and reminders, PDF export, version registry, feature flags and kill switch, go-live lock, audit log, caregiver module and admin console. The patient app screens (and a caregiver start-for-client flow) come after this backend is reviewed.
- `Idempotency-Key` on submit; the 24-hour duplicate-submission warning; `GET /assessments/{id}/result`; server-side drafts (30-day expiry); per-language strings (only English exists).
- A contract migration dropping `fallguard_questions` and `fallguard_answers`.
