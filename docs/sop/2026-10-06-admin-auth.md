# SOP: Admin identity, MFA and permissions (A0)

| | |
|---|---|
| **Date** | 2026-10-06 |
| **Branch** | `feat/admin-auth` (base `main` at `cff139a`) |
| **Migration** | `0003_admin_identity` (down_revision `0002_caregiver_mobile`) |
| **Plan / PRD** | [Admin console plan, A0](../plans/2026-10-06-admin-console.md), PRD 7.17 and 8.2 |
| **FE guide** | [`docs/fe-integration-guide-admin.md`](../fe-integration-guide-admin.md), 66 live captures in `docs/fe-captures/admin/` |
| **Status** | Verified locally (674 tests pass, live stack run). Not pushed. **Needs the Opus review (auth and permission code) before merge.** |

## What shipped
Admins are now their own accounts with their own tokens, TOTP MFA and roles. One dependency, `require_permission("x.y")`, guards every admin route.

| Endpoint | Purpose |
|---|---|
| `POST /admin/auth/login` | Password step. `200` `{mfa_required, mfa_token}` or, if MFA is not enrolled, `{mfa_enrollment_required, mfa_token, otpauth_uri, secret}`. `401` `Invalid email or password`, `423` + `Retry-After` after 10 failures, `429` per IP. |
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
| Lockout | 10 failures (password or code) lock the account for 15 minutes. The 10th answers `401`, the 11th `423` + `Retry-After`. Only a completed sign-in resets the counter, so a correct password cannot be used to keep guessing codes. Reset MFA also lifts a lock (the recovery path). | Resetting the counter on a correct password |
| Per-IP limit | Fixed-window counters in the `admin_rate_limits` table (upsert + `RETURNING`), 30 calls per 60 seconds shared by login, verify and refresh; invite accept has its own bucket. Redis is not required. IP is the direct peer, as in the audit service. | Redis-only limiter |
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
| `ADMIN_MAX_FAILED_LOGINS` / `ADMIN_LOCKOUT_MINUTES` | 10 / 15 | Lockout |
| `ADMIN_AUTH_IP_LIMIT` / `ADMIN_AUTH_IP_WINDOW_SECONDS` | 30 / 60 | Per-IP limit |
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
- **Proxy:** per-IP limits and the audit `ip` use the direct peer address. Behind a reverse proxy every request would look like one IP; configure trusted proxy headers before launch.

## Follow-ups
- Invite email, and an endpoint to re-issue an expired or lost invite (today the email stays taken).
- MFA recovery codes; optional IP allowlist (AD-FR-87); four-eyes approvals (AD-FR-86).
- An admin view of live tracking and of chat and notes (A2).
- Prescription creation by admins moves to the console in v1.1.
- Opus review of `app/api/deps.py`, `app/services/admin_auth.py` and `app/services/admin_users.py` before merge. Points worth a hard look: the `sid` session check, the reuse-detection commit path, the advisory lock, and the shared-route dependency.
