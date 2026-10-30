# SOP: Change password (`POST /api/v1/auth/change-password`)

| | |
|---|---|
| **What shipped** | An authenticated endpoint for app users (caregivers and patients) to change their password. It needs the current password, applies a password policy, throttles wrong current passwords, **ends every session issued before the change**, and returns a fresh token pair so the caller stays signed in. Backend half of the caregiver app's "Change password" screen; the app screen is a separate PR. |
| **Branch / commit** | `feature/change-password`, cut from `oakly/main` `780563f`. Not pushed; the lead opens the PR. |
| **Migration** | **`0020_user_token_version`**: `users.token_version integer NOT NULL DEFAULT 0`. Alembic head moves `0019` -> `0020`. Another stream that also adds a revision after `0019` must renumber (one head only). |
| **FE guide** | `docs/fe-integration-guide-caregiver.md` section 17, rendered from live captures in `docs/fe-captures/caregiver/change-password/` (`scripts/capture_change_password.py`). |

## Why
The caregiver app has forgot/reset password but no way to change it while signed in. Reading the auth code first showed the brief's three assumptions did not hold for **app users**, so the feature had to supply them:

| Assumption in the brief | What the code actually had | What this change does |
|---|---|---|
| "Existing password policy" | None for app users (`UserCreate.password: str`, `PasswordReset.new_password: str`); only the admin console has one (12 characters). The app's register and reset screens check `length >= 8` on the client only. | A small server-side policy for this endpoint: 8 characters minimum, 72 bytes maximum (bcrypt ignores the rest), at least 5 distinct characters, and different from the current password. |
| "Existing lockout / rate limit" | App-user login and refresh have **none**. The admin login has a per-(email, IP) backoff in `admin_rate_limits`. | A per-**account** backoff for wrong current passwords, same shape as the admin password stage, stored in the same table (no new table). |
| "Revoke the caller's OTHER refresh tokens" | App-user access and refresh tokens are **stateless JWTs**: no session table, no token id, nothing to delete. | A `token_version` on the user, carried in each token as `tv`, checked on every request and refresh. A change bumps it. See How. |
| "Write an audit entry if the project audits auth events" | `admin_audit_log.admin_user_id` is a NOT NULL foreign key to `admin_users`; app-user events are not audited anywhere. | No audit row. The change is logged by user id only (INFO), a wrong current password and a throttled call as WARNING. Follow-up below. |

## How
| Area | Decision |
|---|---|
| Revoking sessions | `users.token_version` (int). `create_access_token` / `create_refresh_token` take `token_version` and put it in the token as `tv`. `get_current_user`, the WebSocket auth and `/auth/refresh` refuse a token whose `tv` differs from the user's current value (`token_version_is_current`). A token with no `tv` (issued before this shipped) counts as 0, so **the deploy signs nobody out**. A change adds 1: every token issued before it, **including the caller's own**, stops working. |
| Why the caller gets new tokens | Because the caller's own tokens die with the rest, the response carries a fresh pair (`access_token`, `refresh_token`, `token_type`, plus `detail`), signed with the new version. The app swaps them and stays signed in. Rejected: "keep the caller's current refresh token alive" (it would need a per-session id, i.e. a sessions table) and "sign the caller out too" (poor UX, and the brief says to keep the session). |
| Migration was unavoidable | The brief said no migration unless unavoidable. Revocation needs state somewhere. Redis (optional in this stack) loses state on a flush and would silently un-revoke; a timestamp column needs clock comparisons against second-resolution `iat` (a login in the same second as the change survives). An integer compared for equality is exact and cheap. |
| Error shape | `400`/`429` carry `detail = {"code", "message"}` like the identity-verification and care-plan routes (the rest of the API uses a string). Codes: `wrong_current_password`, `weak_password`, `same_password` (400), `too_many_attempts` (429 with `Retry-After`). |
| Order of checks | Lock the user row (`SELECT ... FOR UPDATE`, so concurrent changes and guesses serialise) -> throttle -> current password (400 and a counted failure) -> policy (`weak_password`) -> same-as-current (`same_password`) -> write. A wrong current password is reported before a weak new one. |
| Throttle | Key `cpw:<user id>` in `admin_rate_limits` (a generic counter store despite its name). `CHANGE_PASSWORD_FREE_ATTEMPTS=5`: the fifth wrong current password starts a wait of `CHANGE_PASSWORD_BACKOFF_SECONDS=30`, doubling each time the wait ends and another wrong one follows, capped at `CHANGE_PASSWORD_BACKOFF_MAX_SECONDS=900`. During a wait every call (even the right password) is `429` and does not extend it. A success deletes the row; a row untouched for an hour restarts at one. Only wrong current passwords count: `weak_password` and `same_password` do not (they are not guesses). The user row is locked **first**, and the throttle is checked and the failure counted (committed) while the lock is held, so a burst of concurrent guesses is checked and counted one at a time and cannot slip past the limit (security review finding, test `test_a_burst_of_guesses_cannot_get_past_the_limit`). |
| Pending reset link | A successful change clears `password_reset_token` / `password_reset_expires_at`, so a reset email sent earlier cannot overwrite the new password. |
| Secrets | Passwords and tokens are never logged (the log lines carry the user id only; a test asserts none of the passwords or tokens appear in captured logs). The success response is `Cache-Control: no-store`. The 422 handler's `NEVER_ECHO_FIELDS` gains `current_password` and `new_password`: a missing field's 422 otherwise echoes the whole request body, passwords included, as `input`. |
| Refactor | The three copies of the token-pair construction in `auth.py` became `_token_pair(user)`. `/auth/refresh` now decodes the claims itself (to read `tv`) instead of calling `verify_refresh_token`, which stays in `security.py` (tests use it). |

## What is involved
| Path | Change |
|---|---|
| `app/alembic/versions/0020_user_token_version.py` (new) | Add / drop `users.token_version` |
| `app/db/models/user.py` | `token_version` column |
| `app/core/security.py` | `tv` claim, `token_version` parameters, `token_version_is_current` |
| `app/api/deps.py` | HTTP and WebSocket auth refuse a stale `tv` |
| `app/services/change_password.py` (new) | Policy, throttle, the change itself |
| `app/api/v1/endpoints/auth.py`, `app/schemas/user.py` | The route, request / response schemas, `_token_pair`, refresh check |
| `app/core/config.py` | `CHANGE_PASSWORD_FREE_ATTEMPTS`, `_BACKOFF_SECONDS`, `_BACKOFF_MAX_SECONDS` |
| `app/main.py` | `NEVER_ECHO_FIELDS` |
| `app/tests/test_change_password.py`, `test_user_token_version_migration.py` (new), `test_admin_care_plans_migration.py` | 25 + 2 tests; the old "0019 is the head" assertion became "0019 follows 0018" |
| `scripts/capture_change_password.py` (new), `docs/fe-captures/caregiver/change-password/` | Live capture |
| `docs/fe-integration-guide-caregiver.md`, `docs/checklist/MASTER_CHECKLIST.md` | Guide section 17 (+ rows in sections 11 and 14), checklist |

## Verification
- **Fast checks:** `poetry check --lock`, `ruff check --select E9,F63,F7,F82 .`, `python -m scripts.ci_smoke` (199 operations), both `render_fe_guide --check`.
- **Tests:** `test_change_password.py` (wrong current, weak new in five shapes, same as old, success then the old password fails and the new one works, this phone and another phone's access **and** refresh tokens revoked, caller stays signed in on the returned pair, a second change revokes the first's pair, a stale in-session copy of the user cannot lose a concurrent change, a token with no `tv` still works until the change, throttle (wait, 429 even with the right password, `Retry-After`, wait ends, success clears, isolated per user, weak/same do not count), reset link cancelled, no password or token in logs, no echo in 422, inactive user, patients too, `tv` on login and refresh tokens).
- **Migration:** `alembic upgrade head`, `downgrade -1`, `upgrade head` on a scratch Postgres 16: single head `0020`, column `integer NOT NULL DEFAULT 0`, gone after the downgrade.
- **Live:** `scripts/capture_change_password.py` against a real uvicorn process and Postgres: 21 captures, all as expected, no JWT or password in the files or in the server log.
- **Full suite** (run once locally because `app/api/deps.py` and token creation are shared by every authenticated route): 3929 passed, 7 skipped, **1 failed**: `test_caregiver_mobile_migration` seeded users through the ORM at the `0002` schema, which the new column broke; it now inserts users with raw SQL (as it already did for bookings). The same run also forced `test_admin_care_plans_migration` to stop asserting that `0019` is the head. Those two files, `test_change_password.py`, the new migration test and `test_migrations.py` were re-run after the fixes (39 passed); the rest of the suite was not run a second time, so CI is the backstop for the final head.
- **Not verified:** a real phone or the app (the screen is built in the next PR), a deployed environment, the doubling and cap of the wait beyond the first 30 seconds (tests only).

## Operate / roll back
- **Deploy:** run the migration; no new secret. Nobody is signed out by the deploy. Tune the wait with the three `CHANGE_PASSWORD_*` settings if needed.
- **Support:** to sign a user out everywhere without a password change, add 1 to `users.token_version` for that user. To clear a throttle: `DELETE FROM admin_rate_limits WHERE key = 'cpw:<user id>'`.
- **Roll back:** revert the code, then `alembic downgrade 0019` if wanted. The old code ignores the `tv` claim, so tokens issued meanwhile keep working, **and tokens that an earlier change had revoked work again**: after a rollback, any account that changed its password should be asked to change it again.

## Follow-ups
- **Password reset should also bump `token_version`**, so resetting through the emailed link ends other sessions (one line in `UserService.reset_password`). Left out to keep this PR to the brief.
- **A server-side password policy for `register` and `reset-password`** (today only this endpoint has one). Their 422 also still echoes the password in `input`.
- **Lockout or backoff on app-user login and refresh** (none today; the throttle here only protects the change endpoint).
- **Audit and notify:** an app-user audit table (the admin one cannot hold them), and an email or push "your password was changed".
- **A token id (`jti`) and a sessions list** would allow "sign out this device" and keeping the caller's own refresh token; not needed for this feature.
- Tokens have no unique id: two logins in the same second give the same token. Harmless for this feature; it is why the capture script waits 1.1 s between two logins.
