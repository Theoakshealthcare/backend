# SOP: Gemini model setting + fallback, and bank account holder must match the profile name (DP2-05)

| | |
|---|---|
| **What shipped** | (1) `GEMINI_MODEL` setting (default `gemini-3.5-flash`) with a 404 fallback and a log line when advice fails. (2) A caregiver's payout account must be held by the person on their Oakly profile: otherwise `422` `bank_account_name_mismatch`. |
| **Branch / commits** | `fix/gemini-model-bank-name` (two commits: `fix(gemini): ...`, `feat(payouts): ...`). Not pushed, no PR. |
| **Migration** | None. |
| **FE guide** | [`docs/fe-integration-guide-caregiver.md`](../fe-integration-guide-caregiver.md) section 10.3 (new `422`). |

## Why
1. `app/services/gemini.py` hard-coded `gemini-2.5-flash`. With the owner's real key that model answers HTTP 404 (retired for the key), `gemini-3.5-flash` and `gemini-3.1-flash-lite` answer OK. Every Gemini call failed, and each caller swallowed the error with a canned fallback, so FallGuard advice was always "Unable to analyze" and nobody saw why.
2. DP2-05: the caregiver picks a bank and number, Paystack resolves the holder name, and we saved it whoever it belonged to. A stolen session could point payouts at a third party's account (the 24 h pause only delays this). The owner's rule, as recommended by the lead: the holder must be the caregiver.

## How
| Decision | Why / alternative rejected |
|---|---|
| `Settings.GEMINI_MODEL: str = "gemini-3.5-flash"`, one line in the existing Gemini block of `app/core/config.py`; `.env.example` documents it (commented). | `Settings` is `extra=forbid`, so it must be declared. Minimal edit so the later merge with `feature/ai-note-transcription` (also edits config) is trivial. |
| `GeminiService._generate_text` tries `GEMINI_MODEL`, then `FALLBACK_MODELS = ("gemini-3.1-flash-lite",)`, **only on a 404** (`exc.code == 404`, the google-genai `APIError` status). Other errors (429, 5xx, auth) are not retried on another model. | A 404 means "this model is gone for this key", a different model can help. Other errors are not model-specific; a retry would double load. No per-instance memory of the working model: a 404 round trip per call is cheap and avoids stale state. |
| `_log_failure` replaces the five `log.exception(...)` calls: `log.warning("Gemini <operation> failed: <ExceptionClass> (status=<code>)")`, no traceback. | The prompt and answers are health data and a traceback carries the exception text. The fallback results returned to callers are unchanged. |
| `FallGuardService.submit_assessment` wraps the AI call in `try/except` too: any failure stores "Unable to analyze" and `[]` and logs only the exception class. | The score is committed before the call; the submission must never fail because of the AI (the service already catches, this is the belt for a replaced or buggy service). |
| Name check lives in `bank_accounts.resolve()` (so it covers **both** `POST /bank-account/resolve` and `PUT /bank-account`), using `account_name_matches_profile`. The caregiver sees the refusal at the lookup step instead of confirming someone else's account and failing on save. | Rejected: check only in `save` (worse UX, same safety); fuzzy scoring like `kyc_matching.names_match` (needs two shared words and treats an initial as a match, too loose and too strict for banks). `kyc_matching.name_tokens` is reused for case, accent and punctuation folding. |
| Rule: drop one-letter tokens from the profile name; its first and last remaining word must both be words of the account name (any order, extra words allowed); a one-word profile needs that word; no usable words never matches. | Matches the owner's rule: `ADA OKAFOR NURSE` vs profile `Ada Nurse` passes; `A. OKAFOR` does not stand in for `Ada`; `Ada` is not a prefix match for `ADAEZE`. Known limit: a hyphenated or spaced variant of one name (`Mary-Jane` vs `MARYJANE`) is a mismatch, send to support. |
| `422`, `detail = {"code": "bank_account_name_mismatch", "message": "..."}`. Neither name is in the response; the log line holds the caregiver id and `****last4` of the account only. | Backend convention is `detail.code`. Names are personal data. |
| Profile name = `Caregiver.full_name`. | It is the name the caregiver signed up with and the one the KYC match already uses. |

## What's involved
- `app/core/config.py` (`GEMINI_MODEL`), `.env.example`, `app/services/gemini.py` (`FALLBACK_MODELS`, `_model_chain`, `_generate_text`, `_log_failure`), `app/services/fallguard.py` (`submit_assessment`).
- `app/services/bank_accounts.py` (`BankNameMismatch`, `account_name_matches_profile`, `resolve(..., profile_name, ...)`), `app/api/v1/endpoints/caregiver.py` (`_bank_error`, `resolve_bank_account`).
- Tests: `app/tests/test_gemini_service.py` (+6), `app/tests/test_fallguard_submit.py` (+1), `app/tests/test_payout_bank_accounts.py` (+~30 parametrised cases), `app/tests/fake_meta.py` (the fake Paystack's default account name is now `TEST CAREGIVER <last4>` so the stock `Test Caregiver` passes), `scripts/capture_caregiver_flow.py` (the change-of-account step resolves to the profile's own name).
- Guide: `docs/fe-integration-guide-caregiver.md` section 10.3.

## Verification
- `pytest` (foreground, one at a time under the shared lock): `test_gemini_service`, `test_fallguard_submit`, `test_payout_bank_accounts`, `test_payout_hardening` = 166 passed; `test_settings_env_file`, `test_payouts`, `test_refunds_failures` = 130 passed.
- Gemini cases: model comes from settings; 404 on the first model then success on the fallback (calls observed in order); 429 does not use the fallback; both models 404 returns the no-advice result, logs `ApiError` and `status=404` only (the test plants a secret in the exception text and an answer in the prompt and asserts neither is logged, and no traceback is attached); a service that raises still gives a `200` FallGuard submit.
- Bank cases: match, reordered, extra middle name on either side, initial, third word, accents, single-name profile, and 7 refusals (someone else, first name only, last name only, initial only, single name, prefix, initials-only profile); a refusal returns the exact body on both endpoints, leaks neither name in the response or the log, calls no `/transferrecipient`, writes no row or event, and leaves an existing account and its pause untouched; a matching change of account still starts the 24 h pause.
- CI fast checks run locally with CI's dummy env: `poetry check --lock`, `ruff check --select E9,F63,F7,F82 .`, `python -m scripts.ci_smoke` (205 operations, one Alembic head `0024`), all seven `render_fe_guide --check` pairs. The full suite is left to CI.
- **Not verified:** no real Gemini call from the API (the owner's key was not read; the model names come from the lead's probe); no real Paystack name lookup; the `422` body is asserted by a test, not captured against the running stack.

## Operate / roll back
- Set `GEMINI_MODEL` in the environment to switch models without a deploy of code (e.g. if `gemini-3.5-flash` is retired too). Watch the logs for `Gemini model ... answered 404` and `Gemini ... failed: ... (status=...)`.
- Roll back the bank rule by reverting the `feat(payouts)` commit (no data change). Existing saved accounts are not re-checked: only a new save or resolve is.
- Support path for a legitimate mismatch (married name, nickname): support corrects the profile name, then the caregiver saves again. There is no admin override.

## Follow-ups
- Capture the `422` against the running stack in the next full `capture_caregiver_flow.py` run (append the call at the end so numbers do not move); that run also re-takes capture `178`, whose account name `PLACEHOLDER ACCOUNT 0001` the script no longer produces.
- Failure logging in other modules was not audited; only the Gemini service was changed.
- Existing saved accounts whose holder does not match the profile are not flagged; an admin report would need a re-resolve per account (Paystack lookups, rate limits).
- Hyphen/space variants of one name are a mismatch (see How). `feature/ai-note-transcription`'s `voice_notes/gemini_provider.py` has its own model constant: point it at `settings.GEMINI_MODEL` when that branch merges.
