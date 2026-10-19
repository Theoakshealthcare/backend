# SOP: Admin identity, MFA and permissions (A0)

| | |
|---|---|
| **Date** | 2026-10-06 |
| **Branch** | `feat/admin-auth` (base `main` at `cff139a`) |
| **Migration** | `0003_admin_identity` (down_revision `0002_caregiver_mobile`) |
| **Plan / PRD** | [Admin console plan, A0](../plans/2026-10-06-admin-console.md), PRD 7.17 and 8.2 |
| **FE guide** | [`docs/fe-integration-guide-admin.md`](../fe-integration-guide-admin.md), 66 live captures in `docs/fe-captures/admin/` |
| **Status** | Verified locally (see the review-fixes addendum at the end for the current test count). Not pushed. The Opus review findings are fixed in the addendum; re-review the addendum's files before merge. |

## What shipped
Admins are now their own accounts with their own tokens, TOTP MFA and roles. One dependency, `require_permission("x.y")`, guards every admin route.

| Endpoint | Purpose |
|---|---|
| `POST /admin/auth/login` | Password step. `200` `{mfa_required, mfa_token}` or, if MFA is not enrolled, `{mfa_enrollment_required, mfa_token, otpauth_uri, secret}`. `401` `Invalid email or password`, `429` + `Retry-After` (password throttle or per-IP limit), `423` + `Retry-After` (account locked by wrong codes, only shown after a correct password). |
| `POST /admin/auth/mfa/verify` | Code step; completes enrolment on first success. `200` token pair + `admin`. |
| `POST /admin/auth/refresh`, `/logout` | Rotating refresh (reuse revokes all sessions); logout `204`. |
| `POST /admin/auth/invite/accept` | Set a password from a single-use invite. |
| `GET /admin/me` | The signed-in admin and their permissions. |
| `GET/POST /admin/admins`, `PATCH /admin/admins/{id}`, `POST .../reset-mfa` | Admin management (`admins.manage`). |
| `GET /admin/audit` | Audit viewer: filter by entity, admin, action, date; paginated (`audit.read`). |
| `python -m scripts.create_super_admin --email ...` | Bootstrap the first Super Admin. |

Also: the old `admins` table is gone; the seven roles and 33 permissions are seeded; every existing admin route now uses `require_permission`; the audit log, caregiver approval columns and skills reference `admin_users`.

## Why
Admin was "a patient-style user with a row in `admins`", checked by `get_current_admin` on some routes and by a never-true `user_type == "admin"` on the refund routes. There was no MFA, no roles, no way to create an admin except a script with hardcoded credentials, and one stolen app login was an admin login (AD-FR-81..84).

## How: decisions and tradeoffs
| Decision | Choice | Rejected |
|---|---|---|
| Token separation | Admin JWTs carry `aud="admin"` and are signed with a key **derived** from `SECRET_KEY` (`sha256("oakly-admin-jwt:" + key)`). The app decoder (python-jose, no audience) refuses any token with an `aud`, and the admin decoder (PyJWT) refuses tokens without it or signed with the app key. Both directions are tested. | The same key and only an `aud` claim: one forged claim away from confusion |
| JWT library | PyJWT for admin tokens, python-jose left alone for app tokens (as the task allowed). `pyjwt` and `pyotp` added to `pyproject.toml`. | Rewriting app-user auth |
| Immediate revocation | The access token carries `sid` (the login session). Each admin request checks that the session still has an unrevoked refresh token, so logout, disable, MFA reset and refresh-token reuse take effect **at once**, not in 15 minutes. Cost: one extra query per admin request. | Pure stateless access tokens: a disabled admin keeps working for 15 minutes |
| Permissions | Read from the database on every request (roles -> permissions), never from the token. A role change applies on the next call. | Permissions in the JWT (stale, bloated) |
| Refresh tokens | Opaque random string, only its SHA-256 stored, grouped by `family_id` (the session). Rotation sets `replaced_by_id`. Presenting a rotated token revokes **every** session of that admin and writes `admin.refresh_token_reuse`. A token revoked by logout is *not* treated as reuse. Idle timeout 30 minutes (each refresh moves it), session cap 12 hours. The row is locked `FOR UPDATE`, so two simultaneous refreshes cannot both win. | JWT refresh tokens (cannot be revoked); treating logout as theft |
| TOTP | `pyotp`, 30-second step, one step of drift. The secret is Fernet-encrypted with the existing key helpers. `mfa_last_step` stores the last accepted step, and a code from that step or earlier is refused (replay). The admin row is locked while verifying, so two simultaneous submissions of one code cannot both pass. | Redis "used code" cache (the DB lock is simpler and survives restarts) |
| Enrolment | First login of an un-enrolled admin generates and stores a *pending* secret (re-served until used), and the first correct code turns MFA on. Matches the contract table. | A separate enrolment endpoint (not in the contract) |
| Lockout | **Superseded by the review-fixes addendum.** Wrong passwords throttle per (email, IP); only wrong codes lock, for 15 min, 1 h, 24 h. Reset MFA also lifts a lock (the recovery path). | A hard lock on password failures (anyone could lock an admin out) |
| Per-IP limit | Fixed-window counters in the `admin_rate_limits` table (upsert + `RETURNING`), 30 calls per 60 seconds shared by login and verify; invite accept has its own bucket; refresh is limited per token (addendum). Redis is not required. The client IP honours `TRUSTED_PROXIES` (addendum). | Redis-only limiter |
| No account enumeration | Unknown email, wrong password, invited and disabled admins all answer `401 Invalid email or password`, and an unknown email still spends a bcrypt verification. | Distinct messages |
| Invites | 48-hour token, only its hash stored, cleared on accept (single use, row locked). The token is returned in the `201`; **nothing is emailed yet**. | Emailing from this PR (needs a console URL setting and a template) |
| Last Super Admin | Every change that could remove one takes a Postgres advisory lock (`pg_advisory_xact_lock`), then counts the *other* active Super Admins. Covers disabling and removing the role. A race test proves two Super Admins cannot remove each other at once. Self-disable is also refused. | A check without a lock (both pass) |
| One check | `require_permission(*perms)` (401 without a valid admin token, 403 naming the missing permission). `get_current_admin` and `user_is_admin` are deleted. | |
| Shared routes | Booking detail and live tracking, caregiver document download and prescription reads served admins before. They now use `get_user_or_admin(permission)`: an app token as before, or an admin token holding `bookings.read`, `documents.read` or `prescriptions.manage`. The two token kinds are told apart by the `aud` claim and verified by their own decoders. | Letting admin tokens through `get_current_user` |
| Permission catalogue | In code (`app/core/admin_permissions.py`) and as a frozen copy in the migration; `test_catalogue_in_code_matches_the_seeded_database` fails on drift, so a permission change always ships its own migration. Where the PRD says "suspend", "cancel-request" or "request", that is a narrower permission of its own (`caregivers.suspend`, `bookings.cancel_request`, `ledger.adjust_request`, `pricing.request`). | |
| Route to permission | caregiver reads `caregivers.read`; decisions, skills, approve, reject, request-changes `caregivers.approve`; suspend, reinstate `caregivers.suspend`; booking-settings writes `pricing.write`; prescription settings, patch, status, upload note `prescriptions.manage`; FallGuard admin `fallguard.write`; refund admin `refunds.approve`; `/booking/ws/stats` `dashboard.read`. | |

### Behaviour changes to know about
- An **app-user token on an admin route is now `401`** (it was `403`): it is not an admin credential at all. The existing tests that asserted `403` there were updated.
- An admin token on an app route is `401`, and on the app WebSockets it closes `4401`.
- Admins no longer reach `POST /prescription/create`, chat and notes, or the live-tracking WebSocket with an app token (they have no app token). See follow-ups.

## What's involved
| Area | Files |
|---|---|
| Models, migration | `app/db/models/admin.py` (new), `app/alembic/versions/0003_admin_identity.py` (new), `app/db/models/{user,audit,caregiver}.py`, `app/schemas/user.py`, `app/services/user.py` (old `Admin` removed) |
| Catalogue and crypto | `app/core/admin_permissions.py`, `app/core/admin_security.py` (new), `app/core/config.py` (`ADMIN_*` settings) |
| Services | `app/services/admin_auth.py` (sign-in, MFA, sessions, lockout, limits), `app/services/admin_users.py` (invite, update, reset MFA, audit query) |
| API | `app/api/deps.py` (`require_permission`, `get_user_or_admin`, `is_admin`), `app/api/v1/endpoints/admin_auth.py`, `admin_users.py` (new), `app/schemas/admin_auth.py`, `app/api/v1/api.py`; rewired: `admin_caregivers`, `booking`, `booking_settings`, `caregiver`, `fallguard`, `patient`, `prescription`; `app/main.py` exposes `Retry-After` through CORS |
| CLI | `scripts/create_super_admin.py` |
| Docs and tooling | `docs/fe-integration-guide-admin.md`, `docs/fe-captures/admin/`, `scripts/capture_admin_flow.py`; `scripts/capture_caregiver_flow.py` now seeds an `admin_users` row and signs in through the real MFA endpoints; `.github/workflows/ci.yml` runs the second `render_fe_guide --check` |
| Tests | `test_admin_rbac.py`, `test_admin_auth.py`, `test_admin_users.py`, `test_admin_migration.py`, `test_create_super_admin.py`, `admin_helpers.py`; `factories.make_admin(db, roles=...)` creates an `admin_users` row; `auth_headers(admin)` opens a real admin session |

### New settings (all have defaults)
| Variable | Default | Meaning |
|---|---|---|
| `ADMIN_ACCESS_TOKEN_MINUTES` | 15 | Access token life |
| `ADMIN_MFA_TOKEN_MINUTES` | 5 | Time allowed between the password step and the code |
| `ADMIN_IDLE_TIMEOUT_MINUTES` | 30 | A refresh token unused this long dies |
| `ADMIN_SESSION_MAX_HOURS` | 12 | Absolute session cap |
| `ADMIN_INVITE_HOURS` | 48 | Invite validity |
| `ADMIN_MAX_FAILED_LOGINS` | 10 | Wrong codes before the account locks |
| `ADMIN_LOCKOUT_TIERS_MINUTES` | `[15, 60, 1440]` | Lock length per consecutive lock (last repeats) |
| `ADMIN_PASSWORD_FREE_ATTEMPTS` / `ADMIN_PASSWORD_BACKOFF_SECONDS` / `ADMIN_PASSWORD_BACKOFF_MAX_SECONDS` | 5 / 30 / 900 | Password throttle per (email, IP) |
| `ADMIN_AUTH_IP_LIMIT` / `ADMIN_AUTH_IP_WINDOW_SECONDS` | 30 / 60 | Per-IP limit (login, verify) |
| `TRUSTED_PROXIES` | empty | Proxies whose `X-Forwarded-For` is believed (CIDRs, comma-separated or JSON) |
| `ADMIN_TOTP_ISSUER` | `Oakly Admin` | Name shown in the authenticator app |

## Verification
- **Baseline:** 523 tests green on `main` before any change.
- **After:** `pytest -q -p no:warnings` **674 passed**. The 29 existing tests that went red during the work were all the intended `403 -> 401` change (an app token on an admin route) and were updated, and the old "admin row" tests were replaced with permission-based ones.
- **New tests (written against the contract table):**
  - permission matrix: every admin-token route (32 of them) against all seven roles (allowed vs `403` naming the permission), and the six shared routes the same way; the matrix is checked against the OpenAPI document so a new admin route cannot ship unlisted;
  - token audience both ways (app token on admin routes, admin token on app routes and an app WebSocket), forged tokens (app key, no audience, wrong type, expired, no live session), revocation by logout, disable and refresh reuse;
  - MFA: enrolment, a stable pending secret, wrong and malformed codes, drift of one step accepted and two refused, replay refusal within a step, and **two concurrent submissions of one code** (exactly one wins);
  - refresh rotation, reuse detection (all sessions, only that admin), idle timeout, session cap, disabled admin;
  - lockout at 10 with `Retry-After`, expiry, counter reset, lock from MFA failures, no reset by a correct password, the per-IP limit and its window rollover;
  - invites: create, single use, concurrent accepts (exactly one wins), expiry, weak passwords keep the invite alive;
  - admin management: roles, disable and re-enable, last Super Admin (and **two Super Admins removing each other at once**), reset MFA, the audit viewer filters and paging;
  - the CLI (creates active admin without MFA, never prints the password, refuses an existing email or weak password, prompt and env paths) and the migration (up, down, up; seed equals the code catalogue; downgrade keeps audit rows).
- **Migration:** `test_migrations.py` (single head, models equal migrations, downgrade to base) passes; `test_admin_migration.py` covers `0003` alone.
- **Live:** `docker compose -p oakly-a0 up --build -d db api` with a temp env file ran `0001 -> 0002 -> 0003` on a fresh database, then `scripts/capture_admin_flow.py` made 66 real requests (all with the expected status). The capture script computes TOTP codes with `pyotp`. Idle timeout and invite expiry were simulated in the database. The stack was shut down with `down -v`. The caregiver capture script's new admin seeding was checked against the live stack as well (it returns `200` on an admin route); the whole caregiver capture was not re-run.
- CI reproduced locally: `poetry check --lock`, `ruff --select E9,F63,F7,F82`, `scripts.ci_smoke`, both `render_fe_guide --check`, and the full suite.

## Operate / roll back
- **First admin:** after deploying, `docker compose exec api python -m scripts.create_super_admin --email you@example.com` (password from a prompt or `OAKLY_BOOTSTRAP_PASSWORD`). The first sign-in enrols MFA.
- **Locked out, lost phone:** another Super Admin uses *Reset MFA*. If there is none, run the CLI with a new email.
- **Roll back:** `alembic downgrade 0002_caregiver_mobile`. It re-creates an empty `admins` table, clears the admin references on caregivers, documents and skills, and keeps audit rows (their foreign key is re-added `NOT VALID`). Admin accounts are lost, which is fine while there is no production data.
- **Proxy:** per-IP limits and the audit `ip` use the direct peer address unless that peer is in `TRUSTED_PROXIES`. Behind a reverse proxy set `TRUSTED_PROXIES` to the proxy's address or CIDR; otherwise every request looks like one IP.

## Follow-ups
- Invite email, and an endpoint to re-issue an expired or lost invite (today the email stays taken).
- MFA recovery codes; optional IP allowlist (AD-FR-87); four-eyes approvals (AD-FR-86).
- An admin view of live tracking and of chat and notes (A2).
- Prescription creation by admins moves to the console in v1.1.
- Opus review of `app/api/deps.py`, `app/services/admin_auth.py` and `app/services/admin_users.py` before merge. Points worth a hard look: the `sid` session check, the reuse-detection commit path, the advisory lock, and the shared-route dependency.

## Review fixes (Opus security review of this branch)

Seven findings, fixed with the tests written first. Migration `0003_admin_identity` was edited in place because it is not merged (`admin_users.mfa_lock_count`; `admin_rate_limits.blocked_until` and `updated_at`).

| # | Severity | Finding | Fix |
|---|---|---|---|
| 1 | High | The per-IP limiter used the direct peer, so behind a proxy all clients shared one bucket; `refresh` shared the login bucket, so login noise could block live sessions | New `app/core/client_ip.py` and `TRUSTED_PROXIES` (default empty = trust nobody). The client is the direct peer unless the peer is trusted, then the right-most `X-Forwarded-For` hop that is not trusted (an unparseable hop falls back to the peer). The limiter and the audit `ip` both use it. `refresh` left the IP bucket: it is limited per token hash, and only for tokens that exist, so junk tokens create no rows |
| 2 | Medium | Ten wrong passwords locked the account from anywhere (DoS), and lock state told known from unknown emails | Password failures throttle per (email-hash, IP): 5 free, then `429` with a wait of 30 s doubling to 15 min, never a lock. The same counter runs for unknown, invited, disabled and active emails (a bcrypt check is always spent), so statuses, bodies and `Retry-After` match at every attempt count. The throttled `429` is returned before the password is checked, so the right password gives no signal. The hard lock (`423`) is only applied after a correct password, and the MFA stage is the only thing that creates one. The login no longer holds a row lock across bcrypt |
| 3 | Low | The lock zeroed the failure counter, so TOTP guessing restarted for free | `admin_users.mfa_lock_count` escalates the lock: 15 min, 1 h, 24 h (`ADMIN_LOCKOUT_TIERS_MINUTES`); reset on a successful sign-in and by Reset MFA. Each lock writes an `admin.locked` audit row with `{stage, lock_count, minutes}` |
| 4 | Low | An `mfa_token` could be reused for 5 minutes | Single use. A successful verify inserts `mfajti:<jti>` into `admin_rate_limits` (primary key, so concurrent inserts collide) under the admin row lock; a later use gets `401 Invalid or expired MFA token` and does not count as a code guess. A wrong code does not spend the token. An `iat` check was rejected: `iat` has one-second resolution and would refuse a legitimate second sign-in in the same second |
| 5 | Low | Logout could miss a refresh in flight and leave its new token alive | `logout` takes `SELECT ... FOR UPDATE` on the presented token row, so it waits for the refresh to commit and its family update then revokes the new token |
| 6 | Chore | `render.yaml` | Deleted; references in SOPs, plans and the checklist now say local Docker |

### Response-shape changes the console must know about
- Wrong passwords now `429` (with `Retry-After`, from the 6th attempt for that email and IP) instead of `423` after the 10th. `423` appears only after a correct password while the account is locked by wrong codes, and `mfa/verify` returns it too.
- `mfa/verify` with an already-used `mfa_token` is `401 Invalid or expired MFA token`: go back to the password step.
- `refresh` is no longer subject to the per-IP `429`. A refresh token presented repeatedly gets `429` on its own. Console rule, now in the guide: serialise refresh calls; two concurrent refreshes with one token look like theft and revoke every session.
- Lock `Retry-After` can now be up to 86400.

### Verification
- Baseline before the changes: 674 passed. After: see the final run in the hand-back (27 new tests in `app/tests/test_admin_auth_review.py` plus the rewritten lockout, rate-limit and concurrency tests in `test_admin_auth.py`).
- The logout race test was run with the `FOR UPDATE` removed and fails there (logout finishes while the refresh is in flight).
- Live: stack rebuilt from scratch with a temp env file, `scripts/capture_admin_flow.py` re-run (78 captures, sections 10 to 13 are new: throttle, MFA lock, single-use token, per-IP limit with refresh still working), guide re-rendered, stack removed with `down -v`.
- Not live: a second client IP and a proxy cannot be produced through the published docker port, so the IP-isolation and `TRUSTED_PROXIES` behaviour is covered by tests only, and lock escalation beyond the first tier is test-only.

### Follow-ups
- An attacker who knows an admin's password can still lock that admin out by sending wrong codes (up to 24 h per lock). Reset MFA by another Super Admin is the recovery; consider alerting on `admin.locked`.
- A password-spraying attacker with many IPs is limited per (email, IP) only. Consider a per-email global ceiling if that shows up in logs.

## Test fix: per-IP limit flake at a minute boundary (2026-10-12)

| | |
|---|---|
| **Branch** | `test/admin-auth-pin-rate-limit-window` |
| **Scope** | Tests only; the production limiter is unchanged |

- **Symptom:** `test_spoofed_xff_from_an_untrusted_peer_does_not_dodge_the_limit` failed once in CI with `[401, 401, 401, 401, 401] == [401, 401, 401, 429, 429]`.
- **Root cause:** `AdminAuthService.rate_limit` (`app/services/admin_auth.py`) uses a fixed window aligned to the epoch (`ADMIN_AUTH_IP_WINDOW_SECONDS` = 60). A burst that crosses a minute boundary starts a new window, the counter resets, and the expected `429`s never come. This is the designed behaviour of a fixed window, so the tests were wrong, not the limiter.
- **Fix:** a shared `pinned_ip_window` fixture (`app/tests/admin_helpers.py`, registered in `conftest.py` like `admin_clock`) sets the window to 10^9 seconds (the current window runs 2001 to 2033), so a burst cannot cross it. Applied to all seven tests that expect a `429` from this limiter:

  | File | Tests |
  |---|---|
  | `test_admin_auth_review.py` | spoofed XFF; noisy client behind a trusted proxy; refresh not limited by login traffic; refresh limited per token |
  | `test_admin_auth.py` | `test_per_ip_rate_limit`; `test_rate_limit_covers_login_and_verify_but_not_refresh`; `test_rate_limit_window_rolls_over` |

- **Retry-After checks:** "refresh limited per token" only asserts `>= 1`, which still holds. `test_per_ip_rate_limit` used to assert `<= window + 1`, which would be vacuous with the pinned window, so it now asserts the exact value (time left in the window + 1, within 2 s). That is a tighter check than before. The rollover test shifts `window_start` back an hour, which still lands in a different window row, so it keeps working.
- **Rejected:** freezing `datetime.utcnow` in `app.services.admin_auth`. It works, but patches a module global that other code in the service also reads; one setting is smaller.
- **Verification:** a throwaway test forced `utcnow` across a minute mid-burst. Without the fixture the spoofed-XFF test failed exactly as in CI; with it, the test passed. Then the two admin-auth test files and the full CI step set passed locally.
