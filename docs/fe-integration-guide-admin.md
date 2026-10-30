# FE integration guide: admin console (sign-in, admin management, bookings operations, booking policy, pricing, earnings, payouts, refunds, care plans, caregiver identity review)

| | |
|---|---|
| **Backend work package** | A0 (sections 3 to 11), branch `feat/admin-auth`, migration `0003_admin_identity`. A2 (section 12), branch `feat/admin-bookings-ops`, migration `0006_admin_bookings_ops`. Booking policy (section 12.9), branch `feat/whatsapp-dropout-redispatch-fee-tiers`. Pricing settings (section 13), branch `fix/booking-settings-audit`, no migration. Refunds (section 12.12), branch `feat/refunds-v2`, migration `0014_refunds_v2`. Care plans (section 12.13), branch `feat/care-plans-admin`, migration `0019_care_plan_admin_permissions` (data only). Caregiver identity review (section 12.14), branch `feat/kyc-admin-api`, no migration |
| **Audience** | `apps/admin` (the console): A1, A2 |
| **Base URL** | `http://127.0.0.1:8000` for the local stack; every path below starts with `/api/v1` |
| **Source of truth** | Every request and response body, status code and error string below was **copied from a real response** captured by `scripts/capture_admin_flow.py` against the running docker compose stack. The files are in [`docs/fe-captures/admin/`](fe-captures/admin/); each example names its file. Anything that could not be exercised live is marked **[not live]** with the reason. |

## 0. How to read this guide

- **Examples are generated, not typed.** Every JSON block below is rendered from the capture named in the comment above it, by `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-admin.md docs/fe-captures/admin` (`--check` fails if the guide is stale; CI runs it). Never edit an example by hand: re-capture, then re-render.
- **Captures are re-derivable.** Start the stack (see [`docs/sop/2026-10-05-secrets-and-rotation.md`](sop/2026-10-05-secrets-and-rotation.md) for `OAKLY_ENV_FILE_PATH`) with `docker compose -p oakly-a0 up --build -d db api`, then run `poetry run python -m scripts.capture_admin_flow --env-file <the same env file>`. The script rewrites the capture directory and takes about five minutes, because it waits for the per-IP rate-limit window and for fresh 30-second TOTP steps.
- **Placeholders.** All data is fake: `example.com` emails, `Placeholder ...` names. UUIDs and timestamps come from the local stack and mean nothing. These values are replaced in the files: access tokens by `<ACCESS_TOKEN_JWT>`, the MFA token by `<MFA_TOKEN_JWT>`, refresh tokens by `<REFRESH_TOKEN>`, invite tokens by `<INVITE_TOKEN>`, TOTP secrets by `<TOTP_SECRET>`, 6-digit codes by `<TOTP_CODE>` (except the deliberate wrong code `000000`) and passwords by `<PLACEHOLDER_PASSWORD>`. The real values are random per run.
- **Some things were seeded or simulated**, because the API cannot do them yet. Each one is marked **[seeded]** or **[db-simulated]** here and has a `note` in its capture:
  - the first Super Admin and a Read-only Analyst were inserted straight into the database (in production the first Super Admin is created with `python -m scripts.create_super_admin`);
  - the 30-minute idle timeout and the 48-hour invite expiry were simulated by moving a timestamp into the past.
- Section 14 is the table of what is verified live and what is not.

## 1. What this is, in the console's terms

Admins are **their own accounts**. They are not patient or caregiver users, they sign in at the console only, and their tokens do not work on the mobile apps' endpoints (or the other way round).

1. **Sign in is two steps.** Email and password, then a 6-digit code from an authenticator app (TOTP). MFA is **required** for every admin.
2. **First sign-in forces MFA enrolment.** An admin who has never completed MFA gets a secret and a `otpauth://` URI instead of a plain "enter your code" prompt. They scan it, type the first code, and are signed in.
3. **A session is a 15-minute access token plus a rotating refresh token.** Refresh tokens are single-use. Using an old one again is treated as theft and signs the admin out of **everything**.
4. **Super Admins manage admins.** They invite by email, assign roles, disable, and force an MFA reset. The last active Super Admin can never be removed.
5. **One permission check guards every admin route.** An admin has roles, roles bundle permissions, and each route needs one permission. `/admin/me` tells the console what the admin may do, so it can hide what they cannot.
6. **Every admin action is audited**, and the Super Admin and Finance can read the audit log.

## 2. Conventions

| Topic | What is true | Evidence |
|---|---|---|
| Error shape | FastAPI's `{"detail": ...}`. It is **not** `{"success": false, ...}`. `detail` is a **string** for every error this guide's endpoints raise on purpose, and a **list of objects** for request-validation `422`s (see the traps in section 9). | sections 3 to 8 |
| Auth header | `Authorization: Bearer <access_token>` on every admin route except `/admin/auth/*`. | 010 |
| No header | `401` `{"detail": "Not authenticated"}` | 011, 044 |
| Bad, expired, wrong-kind or revoked token | `401` `{"detail": "Could not validate credentials"}` | 012, 013, 020, 023, 052 |
| Signed in, missing permission | `403` `{"detail": "Access denied. Missing permission: <permission>"}`. The permission name is part of the string. | 040, 041, 043 |
| Timestamps | ISO 8601 **UTC without a `Z` or offset**, for example `2026-10-06T13:09:01.522951`. Treat them as UTC. | 025, 045 |
| IDs | UUID strings. | many |
| Roles | Always the **role key** (`super_admin`), never a display name. Keys are in section 10. | 010, 025 |
| Admin tokens vs app tokens | Different credentials. An admin token on an app route and an app token on an admin route are both `401`. | 014, 015 |

## 3. Sign in

### 3.1 Password step
`POST /api/v1/admin/auth/login`, no auth.

Request body:

<!-- capture: 004-login.json request.body -->
```json
{
  "email": "boss-f722ce@example.com",
  "password": "<PLACEHOLDER_PASSWORD>"
}
```

**Response `200`, MFA already enrolled:** the console asks for a code.

<!-- capture: 004-login.json response.body -->
```json
{
  "mfa_required": true,
  "mfa_token": "<MFA_TOKEN_JWT>"
}
```

`mfa_token` is a short-lived (5 minutes) token that only works on `POST /admin/auth/mfa/verify`. Keep it in memory; never in storage.

**Response `200`, MFA not enrolled yet** (a new admin, or after a reset): the console shows the enrolment screen. Note that `mfa_required` is **absent** and the three extra fields are present.

<!-- capture: 034-login-first-time.json response.body -->
```json
{
  "mfa_enrollment_required": true,
  "mfa_token": "<MFA_TOKEN_JWT>",
  "otpauth_uri": "otpauth://totp/Oakly%20Admin:verifier-f722ce%40example.com?secret=<TOTP_SECRET>&issuer=Oakly%20Admin",
  "secret": "<TOTP_SECRET>"
}
```

- Render `otpauth_uri` as a QR code. Show `secret` too, as the manual-entry fallback.
- `secret` is the real TOTP secret. Treat it like a password: show it, never log it or store it.
- Signing in again **before finishing enrolment returns the same secret**, so abandoning the screen is safe. Once the first code is verified, the secret is never returned again.

**Errors**

| Status | `detail` | When | Capture |
|---|---|---|---|
| `401` | `Invalid email or password` | Wrong password, unknown email, an invited admin who has not accepted yet, **and a disabled admin**. All four look identical on purpose. | 001, 002, 029, 053 |
| `422` | a list of validation objects | The body is not valid (for example the email has no `@`). | 003 |
| `423` | `Account temporarily locked. Try again later.` plus a `Retry-After` header | The account is locked after wrong **codes** (section 3.4), and only a caller who sent the right password sees it. | 071 |
| `429` | `Too many requests. Try again later.` plus a `Retry-After` header | Too many wrong passwords for this email from this IP (section 3.4), or the per-IP limit (section 3.5). Same body for both. | 064, 066, 076 |

<!-- capture: 001-login-wrong-password.json response.body -->
```json
{
  "detail": "Invalid email or password"
}
```

<!-- capture: 003-login-invalid-email.json response.body -->
```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": [
        "body",
        "email"
      ],
      "msg": "value is not a valid email address: An email address must have an @-sign.",
      "input": "not-an-email",
      "ctx": {
        "reason": "An email address must have an @-sign."
      }
    }
  ]
}
```

### 3.2 Code step (also completes enrolment)
`POST /api/v1/admin/auth/mfa/verify`, no auth.

Request body (`code` is the 6 digits from the authenticator app; the real code is replaced by a placeholder in the capture):

<!-- capture: 035-mfa-verify-enrolment.json request.body -->
```json
{
  "mfa_token": "<MFA_TOKEN_JWT>",
  "code": "<TOTP_CODE>"
}
```

**Response `200`.** The same shape for a normal sign-in and for the first (enrolling) one; shown here for a Verification admin. A Super Admin gets all 33 permissions.

<!-- capture: 035-mfa-verify-enrolment.json response.body -->
```json
{
  "access_token": "<ACCESS_TOKEN_JWT>",
  "refresh_token": "<REFRESH_TOKEN>",
  "token_type": "bearer",
  "expires_in": 900,
  "admin": {
    "id": "d84eb03e-c858-4cb9-8f5b-1d4bf1af1896",
    "email": "verifier-f722ce@example.com",
    "full_name": "Placeholder Verifier",
    "roles": [
      "verification"
    ],
    "permissions": [
      "caregivers.approve",
      "caregivers.read",
      "caregivers.suspend",
      "dashboard.read",
      "documents.read",
      "people.read"
    ]
  }
}
```

- `expires_in` is in seconds (15 minutes). `token_type` is always `"bearer"`.
- `access_token` is a JWT. `refresh_token` is an **opaque string, not a JWT**; do not try to decode it.
- This is the **only** response that carries the `admin` object. The refresh response (section 4) does not. See the nesting traps in section 9.
- The first successful verify is what turns MFA on. Until then the admin can sign in only through this enrolment path.

**Errors**

| Status | `detail` | When | Capture |
|---|---|---|---|
| `401` | `Invalid verification code` | Wrong code, a code that is malformed, **or a code that was already used** (see 3.3). | 005, 009 |
| `401` | `Invalid or expired MFA token` | The `mfa_token` is older than 5 minutes, malformed, not an MFA token, the admin was disabled meanwhile, **or it was already used for a sign-in** (it works once, see 3.2.1). Restart from the password step. | 006, 075 |
| `423` / `429` | as above | Wrong codes count towards the lockout (3.4), and the per-IP limit applies. | 070, 076 |

### 3.2.1 The `mfa_token` works once
A correct code spends the `mfa_token`. Presenting the same token again, even with a fresh valid code and inside its 5 minutes, is refused. A **wrong** code does not spend it, so the admin can retry a typo with the same token.

<!-- capture: 075-single-use-verify-again.json response.body -->
```json
{
  "detail": "Invalid or expired MFA token"
}
```

**UX consequence:** if this `401` comes back after a code was accepted (a double submit, a second tab), do not ask for another code. Send the admin back to the password step.

<!-- capture: 005-mfa-verify-wrong-code.json response.body -->
```json
{
  "detail": "Invalid verification code"
}
```

<!-- capture: 006-mfa-verify-bad-token.json response.body -->
```json
{
  "detail": "Invalid or expired MFA token"
}
```

### 3.3 A code works once (replay protection)
A 6-digit code is valid for its 30-second step (the server also tolerates one step of clock drift either side). After a successful sign-in, **that code, and any older one, is dead**. Signing in again within the same 30 seconds with the same code is refused with the same message as a wrong code:

<!-- capture: 009-mfa-verify-replay.json response.body -->
```json
{
  "detail": "Invalid verification code"
}
```

**UX consequence (must be shown):** if a sign-in succeeds and the admin signs in again straight away (a second tab, a second device, a fast logout and login), the authenticator will show the same code for up to 30 seconds and it will be refused. The error text should say **"Wait for the next code and try again"**, not just "invalid code". Do not count this as the admin's mistake in any local attempt counter; the server still counts it towards the lockout (3.4).

### 3.4 Wrong passwords throttle; wrong codes lock
There are two different protections. They behave differently on purpose.

**Password step: throttle, never a lock.** Wrong passwords are counted per **email and IP address**. The first 5 failures answer `401`. From the 6th attempt on, every attempt (even with the **right** password) answers `429` with a `Retry-After` that starts at 30 seconds and doubles with each further failure, up to 15 minutes. A failure from one IP does not affect the same admin signing in from another IP, and no account is locked at this stage. The answers are **identical** whether the email is unknown, invited, disabled or active, at every attempt count, so the console must not (and cannot) tell the admin which case it is.

<!-- capture: 063-login-failure-5-known-email.json response.body -->
```json
{
  "detail": "Invalid email or password"
}
```

<!-- capture: 064-login-throttled-known-email.json response.body -->
```json
{
  "detail": "Too many requests. Try again later."
}
```

The same call for an email that does not exist returns exactly the same status, body and `Retry-After` (captures 065 and 066). The right password while throttled is also `429`:

<!-- capture: 067-login-throttled-correct-password.json response.body -->
```json
{
  "detail": "Too many requests. Try again later."
}
```

`Retry-After` is in seconds (capture 064 shows `30`). A successful sign-in clears the count for that email and IP.

**Code step: lock.** The password is already proven here, so a hard lock is safe. **10 wrong codes in a row** (in `mfa/verify`) lock the account. The 10th still answers `401`; the 11th gets `423`. The lock lasts **15 minutes, then 1 hour, then 24 hours** for each further lock, and the escalation resets only after a successful sign-in. Every lock is written to the audit log (`admin.locked`).

<!-- capture: 069-mfa-failure-10.json response.body -->
```json
{
  "detail": "Invalid verification code"
}
```

<!-- capture: 070-mfa-locked.json response.body -->
```json
{
  "detail": "Account temporarily locked. Try again later."
}
```

The header in capture 070 is `Retry-After: 900` (seconds, the first lock). While locked, the **right password** at `login` is `423` too, but a **wrong** password is a plain `401`, so the lock is only visible to someone who knows the password:

<!-- capture: 071-login-while-mfa-locked.json response.body -->
```json
{
  "detail": "Account temporarily locked. Try again later."
}
```

<!-- capture: 072-login-wrong-password-while-mfa-locked.json response.body -->
```json
{
  "detail": "Invalid email or password"
}
```

**UX consequences (must be shown):**
- On `429` (either step) show "Too many attempts. Try again in N seconds" from `Retry-After`, disable the form until then, and do not auto-retry. Do not say whether the email exists.
- On `423` show "Account locked. Try again in N minutes" (or hours: `Retry-After` can be up to 86400). A Super Admin can lift a lock early with **Reset MFA** (section 8.4).
- A lock is only ever caused by wrong codes after a correct password. An attacker who only knows an email cannot lock the admin out.

### 3.5 Per-IP rate limit
`login`, `mfa/verify` share one limit of **30 calls per minute per client IP address**; `invite/accept` has its own. Past the limit the answer is `429` with `Retry-After` (seconds until the window ends). The call is not processed.

<!-- capture: 076-rate-limited.json response.body -->
```json
{
  "detail": "Too many requests. Try again later."
}
```

**`refresh` is not part of this limit.** It keeps working while the IP is limited (capture 078), so a busy sign-in page never ends an admin's session. A refresh token that is presented over and over is limited by its own value (same `429` shape).

<!-- capture: 078-refresh-while-ip-limited.json response.body keys=token_type,expires_in -->
```json
{
  "token_type": "bearer",
  "expires_in": 900
}
```

The "client IP" is the direct peer, unless the API is configured with `TRUSTED_PROXIES` (the reverse proxy in front of it), in which case it is the real client taken from `X-Forwarded-For`. This is server configuration, not something the console sends.

## 4. Keeping the session: refresh, logout, idle timeout

### 4.1 Refresh
`POST /api/v1/admin/auth/refresh`, no auth header. Body `{"refresh_token": "<REFRESH_TOKEN>"}`.

<!-- capture: 016-refresh.json request.body -->
```json
{
  "refresh_token": "<REFRESH_TOKEN>"
}
```

**Response `200`: a new pair, and nothing else.** No `admin` object.

<!-- capture: 016-refresh.json response.body -->
```json
{
  "access_token": "<ACCESS_TOKEN_JWT>",
  "refresh_token": "<REFRESH_TOKEN>",
  "token_type": "bearer",
  "expires_in": 900
}
```

- **The old refresh token is dead the moment this succeeds.** Store the new one.
- The new access token works at once. The previous access token keeps working until its own 15 minutes end.

**Errors:** every refresh failure is the same `401`:

<!-- capture: 021-refresh-unknown-token.json response.body -->
```json
{
  "detail": "Invalid or expired refresh token"
}
```

It means: unknown token, already rotated, logged out, idle too long, past the 12-hour session cap, or the admin was disabled. The only recovery is the sign-in screen.

### 4.2 Reuse detection: the trap that signs everyone out
Presenting a refresh token that was **already rotated** is treated as theft. The server revokes **every session of that admin** (all devices), and the answer is the same `401`:

<!-- capture: 018-refresh-reuse.json response.body -->
```json
{
  "detail": "Invalid or expired refresh token"
}
```

The new refresh token that the first refresh had produced is dead too, and so is its access token, **immediately** (not after 15 minutes):

<!-- capture: 019-refresh-after-reuse-sibling-token.json response.body -->
```json
{
  "detail": "Invalid or expired refresh token"
}
```

<!-- capture: 020-me-after-reuse.json response.body -->
```json
{
  "detail": "Could not validate credentials"
}
```

**UX consequences (must be built in):**
- **The console must serialise refresh calls: two concurrent refreshes with the same token trigger reuse detection and revoke all sessions.** Refresh single-flight: If two requests hit a `401` at the same time and both call refresh with the same token, the second one looks like theft and signs the admin out everywhere. The console must send **at most one refresh at a time** and make every waiting request reuse its result. With a BFF, serialise on the session.
- **Never retry a refresh after a network error or timeout.** If the first attempt reached the server but the response was lost, the retry looks like theft. Go to the sign-in screen instead.
- Only refresh on a `401` (or shortly before `expires_in`), not on every page load.

### 4.3 Idle timeout and session cap
- A refresh token that is **not used for 30 minutes** dies. Because access tokens last 15 minutes, an admin who is away for 30 minutes is signed out on return. **[db-simulated]** the expiry was moved into the past; the real 30-minute wait was not run:

<!-- capture: 062-refresh-after-idle-timeout.json response.body -->
```json
{
  "detail": "Invalid or expired refresh token"
}
```

- Regardless of activity, a session ends **12 hours after sign-in**. **[not live]** (covered by an automated test; a 12-hour wait was not run).

**UX consequence:** show a "your session is about to expire" warning before the 30-minute idle point (the PRD asks for it), and send the admin to sign in again, keeping their place, when refresh fails.

### 4.4 Logout
`POST /api/v1/admin/auth/logout`, no auth header. Body `{"refresh_token": "<REFRESH_TOKEN>"}`.

<!-- capture: 022-logout.json request.body -->
```json
{
  "refresh_token": "<REFRESH_TOKEN>"
}
```

Response **`204`, empty body.** It also answers `204` for a token it has never seen, so the console can always clear its state. The access token of that session stops working immediately:

<!-- capture: 023-me-after-logout.json response.body -->
```json
{
  "detail": "Could not validate credentials"
}
```

Logging out one device does **not** touch the admin's other sessions (capture 024).

## 5. Who am I: `GET /api/v1/admin/me`
Needs an admin access token, no particular permission. Use it after sign-in and whenever you need fresh permissions.

<!-- capture: 039-verifier-me.json response.body -->
```json
{
  "id": "d84eb03e-c858-4cb9-8f5b-1d4bf1af1896",
  "email": "verifier-f722ce@example.com",
  "full_name": "Placeholder Verifier",
  "roles": [
    "verification"
  ],
  "permissions": [
    "caregivers.approve",
    "caregivers.read",
    "caregivers.suspend",
    "dashboard.read",
    "documents.read",
    "people.read"
  ]
}
```

- `roles` are role **keys**. `permissions` is the full, sorted list of permission names the admin holds through those roles (a Super Admin has all 33).
- Permissions are read from the database on every request. **A role change takes effect on the admin's very next call, but the console's copy goes stale.** Refetch `/admin/me` after the admin's own roles are edited and whenever any call returns `403`.

Without a valid admin token:

<!-- capture: 011-me-no-token.json response.body -->
```json
{
  "detail": "Not authenticated"
}
```

<!-- capture: 014-admin-route-with-app-token.json response.body -->
```json
{
  "detail": "Could not validate credentials"
}
```

## 6. Permissions and the existing admin routes

Every admin route needs exactly one permission. A `403` means "signed in, not allowed": **show a no-access message and keep the session**. A `401` means "sign in again (after one refresh attempt)".

<!-- capture: 043-caregiver-queue-as-analyst.json response.body -->
```json
{
  "detail": "Access denied. Missing permission: caregivers.read"
}
```

<!-- capture: 042-caregiver-queue-as-verifier.json response.body -->
```json
{
  "items": [],
  "total": 0,
  "limit": 25,
  "offset": 0,
  "needs_review_count": 0,
  "stuck_count": 0
}
```

| Route | Permission |
|---|---|
| `GET /admin/caregivers` (with `?identity=`), `GET /admin/caregivers/{id}` | `caregivers.read` |
| `POST /admin/caregivers/documents/{id}/decision`, `.../identity-decision`, `.../identity-reset`, `.../background-check-decision`, `PUT .../skills`, `.../approve`, `.../reject`, `.../request-changes` | `caregivers.approve` |
| `POST /admin/caregivers/{id}/suspend`, `.../reinstate` | `caregivers.suspend` |
| `GET /booking-settings/`, `GET /booking-settings/latest` | `bookings.read` (section 13) |
| `POST`/`PUT`/`DELETE /booking-settings/...` | `pricing.write` (section 13) |
| `POST`/`PUT`/`DELETE /prescription/settings/...`, `PATCH /prescription/{id}`, `POST /prescription/{id}/upload_note`, `PATCH /prescription/{id}/status` | `prescriptions.manage` |
| `POST`/`PUT`/`DELETE /fallguard/admin/question...`, `GET /fallguard/admin/questions` | `fallguard.write` |
| `GET /patient/admin/refund/requests`, `PUT /patient/admin/refund/requests/{id}` (retired: `410`, section 12.12) | `refunds.approve` |
| `GET /booking/ws/stats` | `dashboard.read` |
| `GET /admin/admins`, `POST /admin/admins`, `PATCH /admin/admins/{id}`, `POST /admin/admins/{id}/reset-mfa` | `admins.manage` |
| `GET /admin/audit` | `audit.read` (Super Admin, Finance) |
| `GET /admin/bookings...`, `POST /admin/bookings/{id}/...` | see section 12.1 |
| `GET /admin/earnings`, `POST /admin/earnings/{id}/hold`, `.../release-hold`, `.../void` | see section 12.10 |
| `GET /admin/payouts`, `GET /admin/payouts/batches`, `GET /admin/payouts/batches/{id}` | `payments.read` (section 12.11) |
| `GET /admin/care-plans...`, `GET /admin/care-plan-templates` | `careplans.read` (Clinical Reviewer, Super Admin; section 12.13) |
| `POST`/`PATCH`/`DELETE` on `/admin/care-plans...`, `/admin/care-plan-goals/...`, `/admin/care-plan-tasks/...`, `/admin/care-plan-medications/...` | `careplans.write` (section 12.13) |
| `POST /admin/care-plan-templates/{id}/review` | `careplans.templates` (section 12.13) |
| `POST /admin/payouts/batches/{id}/approve`, `POST /admin/payouts/batches/{id}/cancel`, `POST /admin/payouts/{id}/cancel` | `payouts.execute` (Finance, Super Admin; section 12.11) |
| `POST /admin/payouts/{id}/retry`, `POST /admin/payouts/{id}/resolve`, `POST /admin/payouts/caregivers/{id}/unfreeze` | `ledger.adjust` (Super Admin only; section 12.11) |

**Routes shared with the apps.** These six existing routes serve patients and caregivers as before, and **also accept an admin token** that holds the permission. For an admin they skip the "is this your own booking/record" check:

| Route | Admin permission needed |
|---|---|
| `GET /booking/booking/{id}`, `GET /booking/booking/{id}/live-tracking` | `bookings.read` |
| `GET /caregiver/documents/{id}/file` | `documents.read` |
| `GET /prescription/patient/{patient_id}`, `GET /prescription/{id}`, `GET /prescription/{id}/status` | `prescriptions.manage` |

The old behaviour "any user with an admin row" is gone. A patient or caregiver token on an `/admin/...` route is `401`, not `403` (they are not admin credentials at all). **[not live]** the shared routes were verified by automated tests with admin tokens, not captured.

## 7. Inviting an admin: the state machine

```
            invite                accept invite            PATCH status=disabled
 (nothing) ---------> invited ------------------> active -------------------------> disabled
                         |                           ^   <------------------------     |
                         |  PATCH status=disabled    |      PATCH status=active        |
                         +---------------------------+-----(only if they ever accepted)-+
```

And separately, MFA for an `active` admin: `not enrolled` -> (first login, secret issued) -> `enrolling` -> (first correct code) -> `enrolled`. **Reset MFA** moves `enrolling` or `enrolled` back to `not enrolled`.

| `status` | Can sign in? | Notes |
|---|---|---|
| `invited` | No (`401` as for a wrong password) | Has a single-use invite token, valid 48 hours. |
| `active` | Yes | `mfa_enabled` is `false` until the first code is verified, then `true`. |
| `disabled` | No | All sessions are revoked at once. Can be set back to `active` only if the admin had accepted their invite. |

### 7.1 Create the invite
`POST /api/v1/admin/admins`, needs `admins.manage`.

<!-- capture: 025-invite.json request.body -->
```json
{
  "email": "verifier-f722ce@example.com",
  "full_name": "Placeholder Verifier",
  "roles": [
    "verification"
  ]
}
```

**Response `201`:** the admin as the list shows them, plus the one-time token.

<!-- capture: 025-invite.json response.body -->
```json
{
  "id": "d84eb03e-c858-4cb9-8f5b-1d4bf1af1896",
  "email": "verifier-f722ce@example.com",
  "full_name": "Placeholder Verifier",
  "status": "invited",
  "mfa_enabled": false,
  "roles": [
    "verification"
  ],
  "last_login_at": null,
  "locked_until": null,
  "created_at": "2026-10-07T20:34:01.526293",
  "invite_token": "<INVITE_TOKEN>",
  "invite_expires_at": "2026-10-09T20:34:01.524768"
}
```

**UX consequences (must be built in):**
- **`invite_token` is returned once, in this response, and nowhere else.** The list never shows it. The backend does **not** email it yet **[not live]**: the console must build the link (for example `/accept-invite?token=<invite_token>`) and show it to the Super Admin to copy. There is no endpoint to resend or re-issue an invite yet. An invite that expired, or a lost token, currently needs a developer; tell the Super Admin that.
- The token is valid for 48 hours (`invite_expires_at`) and works once.
- The email is stored lower-cased.

**Errors**

| Status | `detail` | Capture |
|---|---|---|
| `409` | `An admin with this email already exists` | 026 |
| `422` | `Unknown role: wizard` (a string) | 027 |
| `422` | a list of validation objects (for example `roles` empty, or a bad email) | 028 |
| `403` | `Access denied. Missing permission: admins.manage` | 040 |

<!-- capture: 026-invite-duplicate.json response.body -->
```json
{
  "detail": "An admin with this email already exists"
}
```

<!-- capture: 027-invite-unknown-role.json response.body -->
```json
{
  "detail": "Unknown role: wizard"
}
```

### 7.2 Accept the invite
`POST /api/v1/admin/auth/invite/accept`, no auth. The invitee opens the link and sets a password.

<!-- capture: 032-invite-accept.json request.body -->
```json
{
  "invite_token": "<INVITE_TOKEN>",
  "password": "<PLACEHOLDER_PASSWORD>"
}
```

**Response `200`.** The admin is now `active` but has **not** enrolled MFA: they must sign in (section 3), which forces enrolment.

<!-- capture: 032-invite-accept.json response.body -->
```json
{
  "accepted": true,
  "email": "verifier-f722ce@example.com"
}
```

**Errors**

| Status | `detail` | When | Capture |
|---|---|---|---|
| `400` | `Invite link is invalid or has expired` | Unknown, already used, expired (48 hours) or disabled invite. All indistinguishable. **[db-simulated]** for the expired case. | 033, 038 |
| `422` | a list of validation objects | Password shorter than 12 characters or longer than 72 bytes. | 030 |
| `422` | `Password must not contain your email name` (a string) | Also: `Password is too repetitive`. The invite is **not** used up, so the invitee can try again. | 031 |

<!-- capture: 033-invite-accept-again.json response.body -->
```json
{
  "detail": "Invite link is invalid or has expired"
}
```

<!-- capture: 030-invite-accept-weak-password.json response.body -->
```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": [
        "body",
        "password"
      ],
      "msg": "Value error, Password must be at least 12 characters",
      "input": "short",
      "ctx": {
        "error": {}
      }
    }
  ]
}
```

<!-- capture: 031-invite-accept-password-contains-email.json response.body -->
```json
{
  "detail": "Password must not contain your email name"
}
```

The password rules the console should state up front: at least 12 characters, at most 72 bytes, not mostly one repeated character, not containing the part of their email before the `@`. Because the two kinds of `422` look different, the console must handle both a string and a list for `detail` (section 9).

## 8. Managing admins

All four routes need `admins.manage`.

### 8.1 List: `GET /api/v1/admin/admins`
Returns an array (not an object with `items`) of every admin, oldest first.

<!-- capture: 045-admins-list.json response.body first=2 -->
```json
[
  {
    "id": "48b05fdb-dc10-49ca-9c76-b9019a7405ce",
    "email": "boss-f722ce@example.com",
    "full_name": "Placeholder Super Admin",
    "status": "active",
    "mfa_enabled": true,
    "roles": [
      "super_admin"
    ],
    "last_login_at": "2026-10-07T20:33:30.510311",
    "locked_until": null,
    "created_at": "2026-10-07T20:32:16.007039"
  },
  {
    "id": "d84eb03e-c858-4cb9-8f5b-1d4bf1af1896",
    "email": "verifier-f722ce@example.com",
    "full_name": "Placeholder Verifier",
    "status": "active",
    "mfa_enabled": true,
    "roles": [
      "verification"
    ],
    "last_login_at": "2026-10-07T20:34:02.340501",
    "locked_until": null,
    "created_at": "2026-10-07T20:34:01.526293"
  }
]
```

(Trimmed to the first two items for this guide.) The list never includes secrets, hashes or invite tokens.

### 8.2 Change roles or status: `PATCH /api/v1/admin/admins/{id}`
Body: `{"roles": [...]}` and/or `{"status": "active" | "disabled"}`. Both are optional; `roles` must have at least one entry. `"invited"` is not a valid target.

<!-- capture: 046-admin-update-roles.json request.body -->
```json
{
  "roles": [
    "verification",
    "support"
  ]
}
```

**Response `200`:** the same admin object as the list.

<!-- capture: 046-admin-update-roles.json response.body -->
```json
{
  "id": "d84eb03e-c858-4cb9-8f5b-1d4bf1af1896",
  "email": "verifier-f722ce@example.com",
  "full_name": "Placeholder Verifier",
  "status": "active",
  "mfa_enabled": true,
  "roles": [
    "support",
    "verification"
  ],
  "last_login_at": "2026-10-07T20:34:02.340501",
  "locked_until": null,
  "created_at": "2026-10-07T20:34:01.526293"
}
```

Disabling revokes every session of that admin on the spot; their access token fails with `401` on the next call (capture 052).

**Errors**

| Status | `detail` | When | Capture |
|---|---|---|---|
| `409` | `Cannot remove or disable the last active Super Admin` | The change would leave no active Super Admin (disabling one, or removing the `super_admin` role). | 049 |
| `409` | `You cannot disable your own account` | | 050 |
| `409` | `This admin has not accepted their invite yet` | Setting `active` on an admin who never accepted. | 055 |
| `404` | `Admin not found` | | 048 |
| `422` | a list of validation objects | `status` other than `active`/`disabled`, or `roles` empty. | 047 |
| `422` | `Unknown role: <key>` (a string) | | |

<!-- capture: 049-last-super-admin-demote.json response.body -->
```json
{
  "detail": "Cannot remove or disable the last active Super Admin"
}
```

<!-- capture: 050-disable-self.json response.body -->
```json
{
  "detail": "You cannot disable your own account"
}
```

**UX consequence:** the last-Super-Admin rule is enforced by the server under a lock, so two Super Admins cannot remove each other at the same moment. The console should still hide or disable "disable" and "remove Super Admin role" for the last one, and show the server's message if it is hit.

### 8.3 Disable and re-enable
<!-- capture: 051-admin-disable.json response.body keys=id,status,roles -->
```json
{
  "id": "d84eb03e-c858-4cb9-8f5b-1d4bf1af1896",
  "status": "disabled",
  "roles": [
    "support",
    "verification"
  ]
}
```

A disabled admin's sign-in is the generic `401 Invalid email or password` (capture 053).

### 8.4 Reset MFA: `POST /api/v1/admin/admins/{id}/reset-mfa`
No body. Clears the admin's authenticator, ends all their sessions, **and lifts a lockout** (it is the recovery path for a lost phone). Response `200`, the admin object, with `mfa_enabled` `false`:

<!-- capture: 056-reset-mfa.json response.body -->
```json
{
  "id": "d84eb03e-c858-4cb9-8f5b-1d4bf1af1896",
  "email": "verifier-f722ce@example.com",
  "full_name": "Placeholder Verifier",
  "status": "active",
  "mfa_enabled": false,
  "roles": [
    "support",
    "verification"
  ],
  "last_login_at": "2026-10-07T20:34:02.340501",
  "locked_until": null,
  "created_at": "2026-10-07T20:34:01.526293"
}
```

The admin's next sign-in goes through enrolment again (capture 057, same shape as 3.1's second response). `404` `Admin not found` for an unknown id (058).

## 9. Field-nesting and shape traps

| Trap | Detail |
|---|---|
| **Two shapes from one endpoint** | `POST /admin/auth/login` returns either `{mfa_required, mfa_token}` or `{mfa_enrollment_required, mfa_token, otpauth_uri, secret}`. Absent keys are **absent**, not `null`. Branch on which key is present. |
| **`admin` appears only in `mfa/verify`** | `POST /admin/auth/refresh` returns just `access_token`, `refresh_token`, `token_type`, `expires_in`. Call `GET /admin/me` to get `admin` after a refresh if you need it. |
| **`/admin/me` vs the admin list** | `/admin/me`: `id, email, full_name, roles, permissions`. List, invite, `PATCH` and reset-MFA: `id, email, full_name, status, mfa_enabled, roles, last_login_at, locked_until, created_at`. **`permissions` is only on `/me` and the verify response; `status` and `mfa_enabled` are only on the admin-management shapes.** |
| **Null vs absent** | In the admin object, `last_login_at` and `locked_until` are always present and `null` when not set. |
| **`invite_token` and `invite_expires_at`** | Only on the `201` of `POST /admin/admins`. Never in the list. |
| **`detail` is a string or a list** | Domain errors (`Unknown role: x`, the password rules about repetition or the email, every `401`/`403`/`409`/`423`/`429`) are strings. Pydantic validation errors (bad email, empty roles, password under 12 characters, bad query values) are a list of objects with `loc` and `msg`. Handle both. |
| **Three different `401` messages** | `Not authenticated` (no header), `Could not validate credentials` (bad or revoked token), and the endpoint-specific sign-in messages. Do not branch on the text; branch on the status and the route. |
| **`204` has no body** | `logout` returns an empty body. Do not call `.json()` on it. |
| **List endpoint returns an array** | `GET /admin/admins` is a bare array. `GET /admin/audit` is an object with `items`. |
| **Audit `before`/`after`** | Free-form objects that differ per `action`; they can be `null`. Do not assume keys. |
| **Timestamps** | UTC, no `Z`. Append `Z` before handing them to `Date`. |

## 10. Roles (what each key means)

The console shows role names; the API returns only keys.

| Key | Name |
|---|---|
| `super_admin` | Super Admin |
| `operations` | Operations |
| `verification` | Verification |
| `finance` | Finance |
| `support` | Support |
| `clinical_reviewer` | Clinical Reviewer |
| `read_only_analyst` | Read-only Analyst |

The permissions each role holds are from the PRD, section 8.2, and are what `/admin/me` returns; they are defined in `app/core/admin_permissions.py`. The console should gate on `permissions` from `/admin/me`, not on role keys.

## 11. The audit log: `GET /api/v1/admin/audit`
Needs `audit.read` (Super Admin and, since 2026-10-19, Finance). Newest first. **[not live]** Finance's `200` was not captured: the response shape is the same as for the Super Admin (captures 059 to 061), and the permission is asserted in `test_admin_rbac.py` and `test_admin_users.py`. The log shows every admin's actions with `admin_email`, `ip` and `user_agent`, so show it only to roles that hold `audit.read` (read `permissions` from `GET /admin/me`).

| Query parameter | Meaning |
|---|---|
| `entity_type`, `entity_id` | Exact match, for example `caregiver` plus an id, or `admin_user` |
| `admin_user_id` | Who acted |
| `action` | Exact match, for example `admin.update`, `caregiver.approve` |
| `date_from`, `date_to` | `YYYY-MM-DD`, both inclusive, UTC |
| `limit` | 1 to 200, default 50 |
| `offset` | default 0 |

<!-- capture: 059-audit.json response.body path=items first=1 -->
```json
[
  {
    "id": "8465841d-6a61-49de-9ae2-d38825333df1",
    "admin_user_id": "48b05fdb-dc10-49ca-9c76-b9019a7405ce",
    "admin_email": "boss-f722ce@example.com",
    "action": "admin.reset_mfa",
    "entity_type": "admin_user",
    "entity_id": "d84eb03e-c858-4cb9-8f5b-1d4bf1af1896",
    "before": null,
    "after": null,
    "reason": null,
    "ip": "172.18.0.1",
    "user_agent": "python-httpx/0.28.1",
    "created_at": "2026-10-07T20:34:03.547689"
  }
]
```

Trimmed to the first entry of `items` (the page has three; the real response is wrapped as `{"items": [...], "total": N, "limit": N, "offset": N}`). `total` is the count across all pages.

<!-- capture: 060-audit-filtered.json response.body keys=total,limit,offset -->
```json
{
  "total": 3,
  "limit": 2,
  "offset": 0
}
```

`422` for a bad `limit` and similar (a list of validation objects, capture 061). A caller without `audit.read` gets `403` (capture 041).

Actions this package writes: `admin.invite`, `admin.invite_accepted`, `admin.mfa_enrolled`, `admin.login`, `admin.update`, `admin.reset_mfa`, `admin.locked`, `admin.refresh_token_reuse`. Failed sign-ins of an unknown email are not in the log (there is no admin to attribute them to). The existing caregiver actions are in the log too, now attributed to the admin account.

## 12. Bookings operations (A2)

Backend work package A2, branch `feat/admin-bookings-ops`, migration `0006_admin_bookings_ops` (two nullable columns for the no-show flag). Captures `079` to `118` come from `poetry run python -m scripts.capture_admin_flow --env-file <env> --section bookings`, which replaces only these captures and leaves 001 to 078 alone. The patients, caregivers and bookings in them are **[seeded]** straight into the database (no API creates a paid booking without a live Paystack account); every request to `/admin/bookings...` is a real HTTP call. A re-run mints new ids and timestamps, so values differ between runs and mean nothing.

### 12.1 What the console gets

| Screen | Call |
|---|---|
| Bookings list with filters and search | `GET /admin/bookings` |
| Unmatched queue | `GET /admin/bookings/unmatched` |
| Booking detail with its timeline | `GET /admin/bookings/{id}` |
| "Who can I assign?" picker | `GET /admin/bookings/{id}/candidates` |
| Actions | `POST /admin/bookings/{id}/` `redispatch`, `assign`, `reassign`, `cancel`, `confirm-address`, `no-show`, `correct-status` |

| Route | Permission | Held by (besides Super Admin) |
|---|---|---|
| `GET /admin/bookings`, `GET /admin/bookings/unmatched`, `GET /admin/bookings/{id}` | `bookings.read` | Operations, Finance, Support, Clinical Reviewer |
| `GET .../candidates`, `POST .../assign`, `POST .../reassign` | `bookings.assign` | Operations |
| `POST .../redispatch`, `POST .../no-show` | `bookings.dispatch` | Operations |
| `POST .../cancel` | `bookings.cancel` | Operations |
| `POST .../confirm-address` | `bookings.address` | Operations |
| `POST .../correct-status` | `bookings.correct_status` | nobody else |

Do not hard-code that table in the console. **Read `available_actions` from the detail** (12.4): it already combines the admin's permissions with the booking's state.

Two permissions change what a response *contains* rather than whether it is allowed:

| Permission | Without it |
|---|---|
| `people.pii` | patient and caregiver `full_name` and `phone` are shortened (`"Chidi E."`, `"+234****2003"`) and `masked` is `true` |
| `bookings.address` | the detail's `address` has only `city` and `state`; `line`, `latitude` and `longitude` are `null` and `visible` is `false` |
| `clinical.read` | the condition and special instructions are never sent (12.4) |

### 12.2 The list: `GET /admin/bookings`

Query parameters, all optional: `q` (booking code; with `people.pii` also patient or caregiver name and phone in any format; without it only the booking code is searched, so masked values cannot be confirmed by guessing; `%` and `_` are literal), `status`, `payment_status`, `dispatch_state`, `channel` (`app`, `whatsapp`, `admin`), `city` (case-insensitive, exact), `service_type`, `caregiver_id`, `scheduled_from` / `scheduled_to`, `created_from` / `created_to` (ISO 8601 UTC; `to` is exclusive), `needs_address_check`, `no_show` (`true`: flagged or suspected no-shows, the Attention panel), `sort` (`scheduled_date` default, `created_at`, `total_amount_kobo`), `order` (`desc` default, `asc`), `limit` (1 to 100, default 25), `offset`. Money is **integer kobo** (`total_amount_kobo`), never naira.

<!-- capture: 079-bookings-list.json response.body path=items first=1 -->
```json
[
  {
    "id": "83995098-cb64-44ca-a158-5e8505779b3e",
    "booking_code": "DDDE4418",
    "service_type": "nurse",
    "scheduled_date": "2026-10-10T08:38:05.531904",
    "scheduled_end_date": "2026-10-10T10:38:05.531904",
    "city": "Lagos",
    "state": "Lagos",
    "patient": {
      "id": "50537f4e-b132-4a1f-b2ea-d7f07df939eb",
      "full_name": "Chidi Eze",
      "phone": "+2340000302013",
      "masked": false
    },
    "caregiver": null,
    "status": "pending",
    "payment_status": "pending",
    "dispatch_state": null,
    "total_amount_kobo": 5500000,
    "channel": "app",
    "created_at": "2026-10-07T19:58:05.531010",
    "age_minutes": 40,
    "needs_address_check": false,
    "refund_required": false,
    "no_show_flagged_at": null,
    "no_show_suspected": false
  }
]
```

The wrapper around `items` is `{"items": [...], "total": N, "limit": N, "offset": N}`, the same as the caregiver queue:

<!-- capture: 079-bookings-list.json response.body keys=total,limit,offset -->
```json
{
  "total": 9,
  "limit": 3,
  "offset": 0
}
```

The same call as Finance, who lacks `people.pii` (first item):

<!-- capture: 083-bookings-list-masked.json response.body path=items first=1 -->
```json
[
  {
    "id": "83995098-cb64-44ca-a158-5e8505779b3e",
    "booking_code": "DDDE4418",
    "service_type": "nurse",
    "scheduled_date": "2026-10-10T08:38:05.531904",
    "scheduled_end_date": "2026-10-10T10:38:05.531904",
    "city": "Lagos",
    "state": "Lagos",
    "patient": {
      "id": "50537f4e-b132-4a1f-b2ea-d7f07df939eb",
      "full_name": "Chidi E.",
      "phone": "+234****2013",
      "masked": true
    },
    "caregiver": null,
    "status": "pending",
    "payment_status": "pending",
    "dispatch_state": null,
    "total_amount_kobo": 5500000,
    "channel": "app",
    "created_at": "2026-10-07T19:58:05.531010",
    "age_minutes": 40,
    "needs_address_check": false,
    "refund_required": false,
    "no_show_flagged_at": null,
    "no_show_suspected": false
  }
]
```

`no_show_suspected` is computed on every read: the booking is `accepted` or `en_route`, has no `actual_start_time`, and its start was more than 15 minutes ago. `no_show_flagged_at` is set only by an admin (12.6). They are independent: a flagged booking can stop being suspected once it is cancelled or reassigned.

A bad enum value is a request-validation `422`, whose `detail` is a **list** (section 9):

<!-- capture: 084-bookings-list-bad-status.json response.body -->
```json
{
  "detail": [
    {
      "type": "literal_error",
      "loc": [
        "query",
        "status"
      ],
      "msg": "Input should be 'pending', 'accepted', 'en_route', 'in_progress', 'completed', 'cancelled' or 'declined'",
      "input": "nonsense",
      "ctx": {
        "expected": "'pending', 'accepted', 'en_route', 'in_progress', 'completed', 'cancelled' or 'declined'"
      }
    }
  ]
}
```

### 12.3 The unmatched queue: `GET /admin/bookings/unmatched`

Paid bookings still waiting for a caregiver and needing a person, soonest visit first. Each item is a list item plus two fields:

| `queue_reason` | Meaning |
|---|---|
| `address_check` | WhatsApp booking paid but held: the pin is a city-centre placeholder (`needs_address_check`). Dispatch has **not** started. Wins over the other two. |
| `unmatched` | dispatch gave up (`dispatch_state: "unmatched"`) |
| `sla_breached` | still `dispatching` after `UNMATCHED_SLA_MINUTES` (default 30) |

`waiting_minutes` counts from when dispatch began (from creation for a held booking). Optional filter `?reason=address_check|unmatched|sla_breached`. A booking appears once, under one reason. Cancelled, unpaid and already-accepted bookings never appear.

<!-- capture: 085-bookings-unmatched.json response.body path=items first=1 -->
```json
[
  {
    "id": "b0c5091e-4731-4a67-beb2-73d9a29b1149",
    "booking_code": "12FF3CB2",
    "service_type": "nurse",
    "scheduled_date": "2026-10-08T01:38:05.522142",
    "scheduled_end_date": "2026-10-08T03:38:05.522142",
    "city": "Lagos",
    "state": "Lagos",
    "patient": {
      "id": "78633756-a277-4e57-bf21-ff53a7e1a80b",
      "full_name": "Ada Okafor",
      "phone": "+2340000302011",
      "masked": false
    },
    "caregiver": null,
    "status": "pending",
    "payment_status": "paid",
    "dispatch_state": "unmatched",
    "total_amount_kobo": 5500000,
    "channel": "app",
    "created_at": "2026-10-07T19:58:05.521649",
    "age_minutes": 40,
    "needs_address_check": false,
    "refund_required": false,
    "no_show_flagged_at": null,
    "no_show_suspected": false,
    "queue_reason": "unmatched",
    "waiting_minutes": 90
  }
]
```

### 12.4 The detail: `GET /admin/bookings/{id}`

One call returns everything the detail screen needs. As Operations (address visible, condition masked):

<!-- capture: 087-booking-detail-ops.json response.body -->
```json
{
  "id": "b0c5091e-4731-4a67-beb2-73d9a29b1149",
  "booking_code": "12FF3CB2",
  "service_type": "nurse",
  "assistance_type": "Placeholder assistance",
  "booking_for": "myself",
  "duration_hours": 2,
  "scheduled_date": "2026-10-08T01:38:05.522142",
  "scheduled_end_date": "2026-10-08T03:38:05.522142",
  "actual_start_time": null,
  "actual_end_time": null,
  "status": "pending",
  "channel": "app",
  "cancel_reason": null,
  "created_at": "2026-10-07T19:58:05.521649",
  "updated_at": "2026-10-07T20:38:05.521649",
  "age_minutes": 40,
  "patient": {
    "id": "78633756-a277-4e57-bf21-ff53a7e1a80b",
    "full_name": "Ada Okafor",
    "phone": "+2340000302011",
    "masked": false
  },
  "caregiver": null,
  "address": {
    "city": "Lagos",
    "state": "Lagos",
    "visible": true,
    "line": "12 Placeholder Street, Lekki",
    "latitude": 6.4474,
    "longitude": 3.4723,
    "needs_address_check": false
  },
  "clinical": {
    "masked": true,
    "has_condition": true,
    "has_special_instructions": true,
    "condition": null,
    "special_instructions": null
  },
  "price": {
    "total_kobo": 5500000,
    "service_fee_kobo": 5000000,
    "platform_fee_kobo": 500000
  },
  "payment": {
    "status": "paid",
    "paystack_reference": "BKG_12FF3CB2_dddf1043",
    "failure_reason": null,
    "expires_at": null,
    "refund_required": false,
    "refund_required_reason": null,
    "refund_amount_kobo": null,
    "attempts": []
  },
  "dispatch": {
    "state": "unmatched",
    "started_at": "2026-10-07T19:08:05.522350",
    "waves": [],
    "offers": []
  },
  "events": [],
  "no_show_flagged_at": null,
  "no_show_suspected": false,
  "note_count": 0,
  "timeline": [
    {
      "at": "2026-10-07T19:08:05.522350",
      "kind": "dispatch_started",
      "title": "Dispatch started",
      "actor": "system",
      "detail": null
    },
    {
      "at": "2026-10-07T19:58:05.521649",
      "kind": "booking_created",
      "title": "Booking created via app",
      "actor": "system",
      "detail": null
    }
  ],
  "available_actions": [
    "redispatch",
    "assign",
    "cancel"
  ]
}
```

**Field-nesting traps**

- `patient.phone` and `caregiver.phone` are nested **inside** `patient` / `caregiver`, together with `masked`. The patient's phone comes from the patient row or, for a phone-first WhatsApp patient, from the user row; the console never needs to know which.
- `address` is an object. With no `bookings.address` it is `{"city", "state", "visible": false, "line": null, "latitude": null, "longitude": null, "needs_address_check"}`: **present but null, not absent**. Finance's view:

<!-- capture: 089-booking-detail-finance.json response.body path=address -->
```json
{
  "city": "Lagos",
  "state": "Lagos",
  "visible": false,
  "line": null,
  "latitude": null,
  "longitude": null,
  "needs_address_check": false
}
```

- `caregiver` is `null` until someone accepts. `dispatch.state` is `null` for a booking never dispatched (unpaid, or held).
- `dispatch.offers[].response` is `null` until the caregiver answers, then `accept`, `declined`, `auto_declined` (someone else took it), `booking_cancelled`, `reassigned` or `assigned_by_admin`.
- `payment.refund_required_reason` is a free-text code: `paid_after_expiry`, `paid_after_cancellation`, `admin_cancelled_full` or `admin_cancelled_partial`. The amount to refund is `payment.refund_amount_kobo` (integer kobo; `null` means a person decides it in the refunds queue, section 12.12). *Not captured live: this field was added after the captures; the shape follows the schema.*

**Clinical details are gated and every read is audited.** Without `clinical.read` the response says only that something exists:

<!-- capture: 088-booking-detail-held-whatsapp.json response.body path=clinical -->
```json
{
  "masked": true,
  "has_condition": true,
  "has_special_instructions": true,
  "condition": null,
  "special_instructions": null
}
```

With `clinical.read` (a Clinical Reviewer here) the text comes back, and that call wrote a `booking.clinical_view` audit entry naming the fields, never the content. **Do not cache or log this response in the console, and do not poll it**: each fetch is another audit entry.

<!-- capture: 090-booking-detail-clinical.json response.body path=clinical -->
```json
{
  "masked": false,
  "has_condition": true,
  "has_special_instructions": true,
  "condition": "Placeholder condition (typed in the WhatsApp Flow)",
  "special_instructions": "Placeholder instructions (gate code 0000)"
}
```

<!-- capture: 092-audit-clinical-view.json response.body path=items first=1 -->
```json
[
  {
    "id": "51c82acc-b30d-4e77-9f5f-998d5e403b05",
    "admin_user_id": "7874ee15-24f9-4186-b591-0ea02e37d058",
    "admin_email": "clin-f722ceops@example.com",
    "action": "booking.clinical_view",
    "entity_type": "booking",
    "entity_id": "5b521068-1e0c-4e08-a910-70e2247edee1",
    "before": null,
    "after": {
      "fields": [
        "condition",
        "special_instructions"
      ]
    },
    "reason": null,
    "ip": "172.18.0.1",
    "user_agent": "python-httpx/0.28.1",
    "created_at": "2026-10-07T20:38:05.699900"
  }
]
```

The condition of a WhatsApp booking is stored encrypted and is never returned to anyone without `clinical.read`; the caregiver's visit notes are not part of this response (only `note_count`).

**`timeline`** is every dated thing that happened, oldest first. `kind` is one of `booking_created`, `dispatch_started`, `wave_sent`, `offer_response`, `notification_queued`, `notification_delivered`, `visit_started`, `visit_completed`, `admin_action`. For `admin_action`, `title` is the audit action (`booking.cancel`) and `detail` is the admin's reason. A booking has no status-history table, so a status change by the caregiver or by the expiry job appears only through the fields it sets (`visit_started`, `visit_completed`, `cancel_reason`), not as its own entry. The timeline after confirming an address (dispatch wave, offers, the admin action):

<!-- capture: 112-booking-detail-after-confirm.json response.body path=timeline -->
```json
[
  {
    "at": "2026-10-07T19:58:05.524967",
    "kind": "booking_created",
    "title": "Booking created via whatsapp",
    "actor": "system",
    "detail": null
  },
  {
    "at": "2026-10-07T20:38:05.699900",
    "kind": "admin_action",
    "title": "booking.clinical_view",
    "actor": "admin: Placeholder clinical_reviewer",
    "detail": null
  },
  {
    "at": "2026-10-07T20:38:05.870942",
    "kind": "dispatch_started",
    "title": "Dispatch started",
    "actor": "system",
    "detail": null
  },
  {
    "at": "2026-10-07T20:38:05.870942",
    "kind": "wave_sent",
    "title": "Wave 1 sent",
    "actor": "system",
    "detail": "2 caregiver(s) offered the visit"
  },
  {
    "at": "2026-10-07T20:38:05.871843",
    "kind": "admin_action",
    "title": "booking.confirm_address",
    "actor": "admin: Placeholder operations",
    "detail": "Called the booker and confirmed the street"
  }
]
```

**`available_actions`** lists what this admin can do to this booking right now: any of `redispatch`, `assign`, `force_assign`, `reassign`, `cancel`, `confirm_address`, `flag_no_show`, `correct_status`. Show exactly those buttons. (`force_assign` is the Super Admin's switch on the assign dialog.) A role with no permission, or a booking in a final state, gets `[]`.

`404` for an unknown id: 

<!-- capture: 091-booking-detail-not-found.json response.body -->
```json
{
  "detail": "Booking not found"
}
```

### 12.5 Actions: the rules they share

- **Every action needs a `reason`** (1 to 2000 characters, not blank). Missing or blank is a request-validation `422` (`detail` is a list); the booking is untouched.
- **Every action writes one audit entry** in the same transaction as the change, with the booking's before and after snapshot and the reason (12.7).
- **Every action returns the same small object** (what changed; refetch the detail for the rest) with `notified_caregivers` counting caregivers offered the visit (re-dispatch, assign offer, confirm-address):
- **Errors.** `404` unknown booking or caregiver (string `detail`). `409` the booking is not in a state that allows it (string `detail`, exact wording below; **always show it to the admin**, it says what to do). `403` `Access denied. Missing permission: <permission>` as in section 2, or `Only a Super Admin can force an assignment`. `422` for invalid input.
- **Two admins at once:** the second one gets a `409` describing the state the first one left. Refetch the detail.
- **No action here moves money.** Cancelling a paid booking with a refund choice queues a refund in the refunds queue (section 12.12); nothing is refunded until a person approves it there and Paystack pays it.

<!-- capture: 096-redispatch.json response.body -->
```json
{
  "id": "b0c5091e-4731-4a67-beb2-73d9a29b1149",
  "booking_code": "12FF3CB2",
  "status": "pending",
  "payment_status": "paid",
  "dispatch_state": "dispatching",
  "caregiver_id": null,
  "needs_address_check": false,
  "refund_required": false,
  "refund_required_reason": null,
  "refund_amount_kobo": null,
  "no_show_flagged_at": null,
  "notified_caregivers": 2
}
```

### 12.6 The actions

**Re-dispatch** `POST /admin/bookings/{id}/redispatch` (`bookings.dispatch`). Body `{"reason", "radius_km"?, "include_previously_notified"?}`. For a paid, pending booking with no caregiver and no address hold: restarts dispatch with a new wave. `radius_km` (1 to 100) widens the search for that wave only; `include_previously_notified: true` offers it again to caregivers an earlier wave already notified. If nobody is eligible the call still succeeds with `notified_caregivers: 0` and `dispatch_state: "unmatched"`, and the attempt is audited. Show that as "no one available", not as an error.

<!-- capture: 094-redispatch-no-reason.json response.body -->
```json
{
  "detail": [
    {
      "type": "missing",
      "loc": [
        "body",
        "reason"
      ],
      "msg": "Field required",
      "input": {}
    }
  ]
}
```

<!-- capture: 095-redispatch-held.json response.body -->
```json
{
  "detail": "Confirm the address first: the pin is a city-centre placeholder"
}
```

**Candidates** `GET /admin/bookings/{id}/candidates?radius_km=` (`bookings.assign`): approved, available, skilled caregivers near the patient with a recent location, best match first. The caregiver's name is not masked here (they are staff of the platform, not patients).

<!-- capture: 093-booking-candidates.json response.body -->
```json
{
  "items": [
    {
      "caregiver_id": "d9d06786-abc3-422d-93d1-31074b0359cb",
      "full_name": "Placeholder Nurse One",
      "specialization": "nurse",
      "distance_km": 0.22,
      "estimated_arrival_minutes": 5,
      "match_score": 232.56
    },
    {
      "caregiver_id": "fadb48ca-b532-4c09-8048-826a49d80a86",
      "full_name": "Placeholder Nurse Two",
      "specialization": "nurse",
      "distance_km": 0.44,
      "estimated_arrival_minutes": 5,
      "match_score": 232.12
    }
  ]
}
```

**Assign** `POST /admin/bookings/{id}/assign` (`bookings.assign`). Body `{"caregiver_id", "reason", "force"?}`. Only for a paid, pending booking with no caregiver. The caregiver must be **approved** (`409` `Caregiver is not approved`).

- **Offer (default).** The visit is offered to that one caregiver, who gets a push and must still accept in the app. The booking **stays `pending`** and keeps dispatching; poll the detail to see the accept. The response shows `notified_caregivers: 1`.

<!-- capture: 097-assign-offer.json response.body -->
```json
{
  "id": "820fe9c4-ccc8-42c2-bfd3-b627c7f33147",
  "booking_code": "7F2A1CFC",
  "status": "pending",
  "payment_status": "paid",
  "dispatch_state": "dispatching",
  "caregiver_id": null,
  "needs_address_check": false,
  "refund_required": false,
  "refund_required_reason": null,
  "refund_amount_kobo": null,
  "no_show_flagged_at": null,
  "notified_caregivers": 1
}
```

<!-- capture: 098-assign-unapproved.json response.body -->
```json
{
  "detail": "Caregiver is not approved"
}
```

- **Force (`"force": true`, Super Admin only).** No accept step: the booking becomes `accepted` for that caregiver at once, a visit code is generated, every other open offer is closed, and the caregiver is told. Anyone else gets `403`:

<!-- capture: 099-assign-force-as-ops.json response.body -->
```json
{
  "detail": "Only a Super Admin can force an assignment"
}
```

<!-- capture: 100-assign-force.json response.body -->
```json
{
  "id": "2d772592-2670-4373-9f08-ffcd9ad2f215",
  "booking_code": "F69A59BB",
  "status": "accepted",
  "payment_status": "paid",
  "dispatch_state": "matched",
  "caregiver_id": "fadb48ca-b532-4c09-8048-826a49d80a86",
  "needs_address_check": false,
  "refund_required": false,
  "refund_required_reason": null,
  "refund_amount_kobo": null,
  "no_show_flagged_at": null,
  "notified_caregivers": 0
}
```

- **A booking someone accepted in the meantime is a `409`, never a silent reassignment.** This is the race between an admin and a caregiver pressing accept: exactly one wins, and the loser sees:

<!-- capture: 103-assign-on-accepted-booking.json response.body -->
```json
{
  "detail": "Only a pending booking without a caregiver can be offered"
}
```

**Reassign** `POST /admin/bookings/{id}/reassign` (`bookings.assign`). Body `{"caregiver_id", "reason"}`. For an `accepted` or `en_route` booking that has not started. The new caregiver is assigned directly and told, the old one is told the visit was reassigned, the status goes back to `accepted`, and a no-show flag is cleared. The patient's visit code is kept.

<!-- capture: 102-reassign.json response.body -->
```json
{
  "id": "b94b0bd1-a9bd-4d7f-99fd-49793772ff80",
  "booking_code": "22B3A2FC",
  "status": "accepted",
  "payment_status": "paid",
  "dispatch_state": "matched",
  "caregiver_id": "fadb48ca-b532-4c09-8048-826a49d80a86",
  "needs_address_check": false,
  "refund_required": false,
  "refund_required_reason": null,
  "refund_amount_kobo": null,
  "no_show_flagged_at": null,
  "notified_caregivers": 0
}
```

<!-- capture: 101-reassign-same-caregiver.json response.body -->
```json
{
  "detail": "The booking is already assigned to this caregiver"
}
```

**Cancel** `POST /admin/bookings/{id}/cancel` (`bookings.cancel`). Body `{"reason_code", "reason", "refund", "refund_amount_kobo"?}`.

| Field | Values |
|---|---|
| `reason_code` | `patient_request`, `no_supply`, `caregiver_no_show`, `safety`, `duplicate`, `other` (stored in `cancel_reason`) |
| `refund` | `full`, `partial` (needs `refund_amount_kobo`, greater than 0 and less than the amount paid) or `none` |

Allowed from `pending`, `accepted` and `en_route`. A refund choice on an unpaid booking is a `422`. The response of a full-refund cancel of a paid WhatsApp booking:

<!-- capture: 105-cancel.json response.body -->
```json
{
  "id": "61cc3da4-2a0d-4760-b20b-dcc41d12c5bd",
  "booking_code": "16CEFA37",
  "status": "cancelled",
  "payment_status": "paid",
  "dispatch_state": "dispatching",
  "caregiver_id": null,
  "needs_address_check": false,
  "refund_required": true,
  "refund_required_reason": "admin_cancelled_full",
  "refund_amount_kobo": 5500000,
  "no_show_flagged_at": null,
  "notified_caregivers": 0
}
```

<!-- capture: 104-cancel-partial-no-amount.json response.body -->
```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": [
        "body"
      ],
      "msg": "Value error, refund_amount_kobo is required for a partial refund",
      "input": {
        "reason_code": "patient_request",
        "reason": "Patient called",
        "refund": "partial"
      },
      "ctx": {
        "error": {}
      }
    }
  ]
}
```

<!-- capture: 106-cancel-again.json response.body -->
```json
{
  "detail": "A cancelled booking cannot be cancelled"
}
```

<!-- capture: 107-cancel-unpaid-with-refund.json response.body -->
```json
{
  "detail": "Nothing to refund: the booking has not been paid for"
}
```

**UX the console must surface**

- **Cancelling does not refund.** `refund_required` becomes `true` and a refund is queued for approval (section 12.12); the booking's `payment_status` stays `paid` until a refund succeeds (then `partially_refunded` or `refunded`). Say "refund requested", never "refunded".
- A WhatsApp booker is told "a refund is coming" when a refund was chosen. **A cancel with `refund: none` sends the patient no cancellation message yet** (there is no event for it; follow-up). Tell the admin to contact the patient.
- A caregiver who had accepted gets a notification and a push; unanswered offers to other caregivers are closed.

**Confirm address** `POST /admin/bookings/{id}/confirm-address` (`bookings.address`). Body `{"reason", "latitude", "longitude", "address"?}`. For a booking with `needs_address_check` (a WhatsApp booking with no map pin: its coordinates are a city-centre placeholder, so it was **paid but not dispatched**). Clears the hold, saves the coordinates (and the street address if given) and **starts dispatch in the same call**; `notified_caregivers` is the first wave's size. The coordinates must be inside a rough box around Nigeria (latitude 3.5 to 14, longitude 2.5 to 15), so a swapped pair is a `422`. On a booking that is not yet paid it only clears the flag.

<!-- capture: 109-confirm-address-outside-nigeria.json response.body -->
```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": [
        "body"
      ],
      "msg": "Value error, latitude/longitude are outside Nigeria; check they are not swapped",
      "input": {
        "reason": "Typo",
        "latitude": 51.5,
        "longitude": -0.12
      },
      "ctx": {
        "error": {}
      }
    }
  ]
}
```

<!-- capture: 110-confirm-address.json response.body -->
```json
{
  "id": "5b521068-1e0c-4e08-a910-70e2247edee1",
  "booking_code": "81931E50",
  "status": "pending",
  "payment_status": "paid",
  "dispatch_state": "dispatching",
  "caregiver_id": null,
  "needs_address_check": false,
  "refund_required": false,
  "refund_required_reason": null,
  "refund_amount_kobo": null,
  "no_show_flagged_at": null,
  "notified_caregivers": 2
}
```

<!-- capture: 111-confirm-address-again.json response.body -->
```json
{
  "detail": "This booking does not need an address check"
}
```

**No-show** `POST /admin/bookings/{id}/no-show` (`bookings.dispatch`). Body `{"reason"}`. Flags an `accepted` or `en_route` booking whose start time has passed and which has not started. It does **not** cancel or reassign: it sets `no_show_flagged_at` so the Attention panel and the audit log have a record, and the admin then reassigns or cancels. A second flag is a `409`.

<!-- capture: 113-no-show.json response.body -->
```json
{
  "id": "bcfecce6-4fca-4779-bf5f-4ba1f1e66d36",
  "booking_code": "550BDFF3",
  "status": "accepted",
  "payment_status": "paid",
  "dispatch_state": "matched",
  "caregiver_id": "d9d06786-abc3-422d-93d1-31074b0359cb",
  "needs_address_check": false,
  "refund_required": false,
  "refund_required_reason": null,
  "refund_amount_kobo": null,
  "no_show_flagged_at": "2026-10-07T20:38:05.896930",
  "notified_caregivers": 0
}
```

<!-- capture: 114-no-show-again.json response.body -->
```json
{
  "detail": "Already flagged as a no-show"
}
```

**Correct status** `POST /admin/bookings/{id}/correct-status` (`bookings.correct_status`, Super Admin only). Body `{"status": "en_route" | "in_progress" | "completed", "reason"}`. Moves forward only: `accepted` to any of the three, `en_route` to `in_progress` or `completed`, `in_progress` to `completed`. It sets the same timestamps the caregiver's own start and complete set (`actual_start_time` when missing, `actual_end_time` on completion). Crediting the caregiver's earnings and the feedback request are not built yet (A3 and later); when they ship they hook onto this transition. Cancelling is not a correction: use cancel, which asks for the refund choice (`422` otherwise).

<!-- capture: 115-correct-status-as-ops.json response.body -->
```json
{
  "detail": "Access denied. Missing permission: bookings.correct_status"
}
```

<!-- capture: 116-correct-status.json response.body -->
```json
{
  "id": "7842c39a-ffa3-4d4f-a2a2-d7638b5b8752",
  "booking_code": "46BDBAF2",
  "status": "completed",
  "payment_status": "paid",
  "dispatch_state": "matched",
  "caregiver_id": "fadb48ca-b532-4c09-8048-826a49d80a86",
  "needs_address_check": false,
  "refund_required": false,
  "refund_required_reason": null,
  "refund_amount_kobo": null,
  "no_show_flagged_at": null,
  "notified_caregivers": 0
}
```

<!-- capture: 117-correct-status-again.json response.body -->
```json
{
  "detail": "A completed booking cannot be corrected to completed"
}
```

### 12.7 The audit trail of these actions

Every action above and every clinical read is in `GET /admin/audit` (section 11), filterable with `entity_type=booking` and `action=booking.<name>`. Actions: `booking.redispatch`, `booking.assign`, `booking.force_assign`, `booking.reassign`, `booking.cancel`, `booking.confirm_address`, `booking.flag_no_show`, `booking.correct_status`, `booking.clinical_view`. The newest entry from the capture run:

<!-- capture: 118-audit-booking-actions.json response.body path=items first=1 -->
```json
[
  {
    "id": "a154db5c-be21-492f-b4c4-1f844a4381bf",
    "admin_user_id": "3b9c1a7b-e859-46ca-a6c1-f95858aea6e8",
    "admin_email": "boss-f722ceops@example.com",
    "action": "booking.correct_status",
    "entity_type": "booking",
    "entity_id": "7842c39a-ffa3-4d4f-a2a2-d7638b5b8752",
    "before": {
      "status": "in_progress",
      "payment_status": "paid",
      "dispatch_state": "matched",
      "caregiver_id": "fadb48ca-b532-4c09-8048-826a49d80a86",
      "needs_address_check": false,
      "refund_required": false,
      "refund_required_reason": null,
      "cancel_reason": null,
      "no_show_flagged_at": null
    },
    "after": {
      "status": "completed",
      "payment_status": "paid",
      "dispatch_state": "matched",
      "caregiver_id": "fadb48ca-b532-4c09-8048-826a49d80a86",
      "needs_address_check": false,
      "refund_required": false,
      "refund_required_reason": null,
      "cancel_reason": null,
      "no_show_flagged_at": null
    },
    "reason": "Caregiver forgot to check out",
    "ip": "172.18.0.1",
    "user_agent": "python-httpx/0.28.1",
    "created_at": "2026-10-07T20:38:05.921413"
  }
]
```

### 12.8 Booking status: who can move it

| Status | How it gets here | Admin can |
|---|---|---|
| `pending` | created; paid or not | cancel; if paid and not held: re-dispatch, assign (offer or force); if held: confirm address |
| `accepted` | a caregiver accepted, or an admin forced the assignment | reassign, cancel, flag no-show, correct status |
| `en_route` | caregiver pressed "on my way" | the same as `accepted` |
| `in_progress` | visit started with the visit code | correct status to `completed` |
| `completed`, `cancelled`, `declined` | final | nothing |

### 12.9 Booking policy: `GET` and `PATCH /api/v1/admin/booking-policy`

The cancellation fee tiers and the caregiver drop-out timings are **data an admin edits**, not settings. One policy: thirteen settings (twelve whole numbers and one choice, `payout_cadence`). Reading needs `bookings.read` (Support has it); changing needs `settings.write` (Operations and Super Admin) **and a `reason`**, and writes an audit entry with the old and new values. Errors here are `{"detail": ...}` like the other admin routes.

| Field | Meaning | Default |
|---|---|---|
| `cancel_free_hours` | full refund when the visit is further away than this (or nobody is assigned) | 12 |
| `cancel_late_hours` | inside this, or once the caregiver is on the way, the late fee applies | 2 |
| `cancel_fee_percent` | fee, percent of the service fee, between the two lines | 20 |
| `cancel_late_fee_percent` | fee, percent of the service fee, in the late window | 50 |
| `dropout_redispatch_min_lead_minutes` | a caregiver drop-out further ahead than this is re-dispatched automatically | 120 |
| `dropout_decision_lead_minutes` | no replacement by this long before the start: the family is asked | 60 |
| `onmyway_compensation_window_minutes` | an on-my-way tapped no earlier than this before the start can qualify for compensation | 120 |
| `caregiver_late_fee_share_percent` | caregiver's share of a kept late-cancellation fee (credited as a pending earning, section 12.10) | 80 |
| `caregiver_visit_share_percent` | caregiver's share of a completed visit's **service fee** (never the platform fee), percent. Copied onto each booking when it is created | 80 |
| `earning_hold_hours` | an earning becomes withdrawable this many hours after the visit is completed (0 to 168). Copied onto each booking when it is created | 24 |
| `payout_cadence` | `weekly`, `biweekly` or `monthly`. Stored for the payouts work; **nothing reads it yet** | `weekly` |
| `payout_min_naira` | smallest balance a scheduled payout will pay, in **naira**. Not read yet | 5000 |
| `finance_refund_limit_naira` | largest refund Finance may approve alone, in **naira**. Not read yet | 50000 |

Rules the form should mirror (the server enforces them, and so does the database): percents 0 to 100, hours 0 to 168, minutes 0 or more, the two `_naira` fields 0 to 100,000,000, `payout_cadence` one of the three words (anything else is a `422` validation object, below), `cancel_late_hours` shorter than `cancel_free_hours`, `dropout_decision_lead_minutes` shorter than `dropout_redispatch_min_lead_minutes`. Send **only the fields that change**; the merged result is validated as a whole.

**UX consequences to surface.** An edit changes the cancellation terms only for bookings **quoted afterwards**: each quote and booking keeps a copy of the tiers the family was shown. The caregiver share and the hold are copied onto a booking the same way, **when it is created**, so changing them never alters what a caregiver was already promised. The drop-out timings and the on-my-way window apply immediately. Say so on the save confirmation.

Read (Support), `GET`:

<!-- capture: 119-policy-get.json response.body -->
```json
{
  "cancel_free_hours": 12,
  "cancel_late_hours": 2,
  "cancel_fee_percent": 20,
  "cancel_late_fee_percent": 50,
  "dropout_redispatch_min_lead_minutes": 120,
  "dropout_decision_lead_minutes": 60,
  "onmyway_compensation_window_minutes": 120,
  "caregiver_late_fee_share_percent": 80,
  "caregiver_visit_share_percent": 80,
  "earning_hold_hours": 24,
  "payout_cadence": "weekly",
  "payout_min_naira": 5000,
  "finance_refund_limit_naira": 50000,
  "updated_at": "2026-10-07T20:31:25.551552",
  "updated_by": null
}
```

Change, `PATCH` (Operations):

<!-- capture: 131-policy-update.json request.body -->
```json
{
  "reason": "Raise the late fee while we measure no-shows",
  "cancel_late_fee_percent": 60,
  "dropout_decision_lead_minutes": 45,
  "caregiver_visit_share_percent": 75,
  "earning_hold_hours": 48,
  "payout_cadence": "biweekly"
}
```

<!-- capture: 131-policy-update.json response.body -->
```json
{
  "cancel_free_hours": 12,
  "cancel_late_hours": 2,
  "cancel_fee_percent": 20,
  "cancel_late_fee_percent": 60,
  "dropout_redispatch_min_lead_minutes": 120,
  "dropout_decision_lead_minutes": 45,
  "onmyway_compensation_window_minutes": 120,
  "caregiver_late_fee_share_percent": 80,
  "caregiver_visit_share_percent": 75,
  "earning_hold_hours": 48,
  "payout_cadence": "biweekly",
  "payout_min_naira": 5000,
  "finance_refund_limit_naira": 50000,
  "updated_at": "2026-10-07T20:39:03.215386",
  "updated_by": "c499ba9f-c4de-4e44-acab-bd94cd0e4ba5"
}
```

Errors (all `422` unless noted). No reason, nothing to change, a misspelt field (the shapes are Pydantic's list form):

<!-- capture: 121-policy-update-no-reason.json response.body -->
```json
{
  "detail": [
    {
      "type": "missing",
      "loc": [
        "body",
        "reason"
      ],
      "msg": "Field required",
      "input": {
        "cancel_late_fee_percent": 60
      }
    }
  ]
}
```

<!-- capture: 122-policy-update-nothing.json response.body -->
```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": [
        "body"
      ],
      "msg": "Value error, send at least one policy field to change",
      "input": {
        "reason": "No field sent"
      },
      "ctx": {
        "error": {}
      }
    }
  ]
}
```

<!-- capture: 123-policy-update-unknown-field.json response.body -->
```json
{
  "detail": [
    {
      "type": "extra_forbidden",
      "loc": [
        "body",
        "cancel_late_fee"
      ],
      "msg": "Extra inputs are not permitted",
      "input": 60
    }
  ]
}
```

A rule broken by the merged policy (these are plain strings, not lists):

<!-- capture: 124-policy-update-percent-over-100.json response.body -->
```json
{
  "detail": "cancel_fee_percent must be between 0 and 100"
}
```

<!-- capture: 125-policy-update-late-not-shorter.json response.body -->
```json
{
  "detail": "The late-cancellation window (cancel_late_hours) must be shorter than the free-cancellation window (cancel_free_hours)"
}
```

<!-- capture: 126-policy-update-decision-not-shorter.json response.body -->
```json
{
  "detail": "The decision lead (dropout_decision_lead_minutes) must be shorter than the re-dispatch minimum lead (dropout_redispatch_min_lead_minutes)"
}
```

The earning terms are checked the same way (share over 100, hold over a week, a cadence that is not one of the three):

<!-- capture: 127-policy-update-share-over-100.json response.body -->
```json
{
  "detail": "caregiver_visit_share_percent must be between 0 and 100"
}
```

<!-- capture: 128-policy-update-hold-too-long.json response.body -->
```json
{
  "detail": "earning_hold_hours cannot be more than 168 hours"
}
```

<!-- capture: 129-policy-update-bad-cadence.json response.body -->
```json
{
  "detail": [
    {
      "type": "literal_error",
      "loc": [
        "body",
        "payout_cadence"
      ],
      "msg": "Input should be 'weekly', 'biweekly' or 'monthly'",
      "input": "daily",
      "ctx": {
        "expected": "'weekly', 'biweekly' or 'monthly'"
      }
    }
  ]
}
```

Support cannot change it (`403`), and no token is `401`:

<!-- capture: 130-policy-update-as-support.json response.body -->
```json
{
  "detail": "Access denied. Missing permission: settings.write"
}
```

<!-- capture: 120-policy-get-no-token.json response.body -->
```json
{
  "detail": "Not authenticated"
}
```

The audit entry (`GET /admin/audit?entity_type=booking_policy`), `entity_id` is the string `"1"`:

<!-- capture: 133-audit-policy-update.json response.body path=items first=1 -->
```json
[
  {
    "id": "97d0e51d-a1bb-4bbf-9e73-80fb36708156",
    "admin_user_id": "c499ba9f-c4de-4e44-acab-bd94cd0e4ba5",
    "admin_email": "ops-f722cepol@example.com",
    "action": "booking_policy.update",
    "entity_type": "booking_policy",
    "entity_id": "1",
    "before": {
      "cancel_free_hours": 12,
      "cancel_late_hours": 2,
      "cancel_fee_percent": 20,
      "cancel_late_fee_percent": 50,
      "dropout_redispatch_min_lead_minutes": 120,
      "dropout_decision_lead_minutes": 60,
      "onmyway_compensation_window_minutes": 120,
      "caregiver_late_fee_share_percent": 80,
      "caregiver_visit_share_percent": 80,
      "earning_hold_hours": 24,
      "payout_cadence": "weekly",
      "payout_min_naira": 5000,
      "finance_refund_limit_naira": 50000
    },
    "after": {
      "cancel_free_hours": 12,
      "cancel_late_hours": 2,
      "cancel_fee_percent": 20,
      "cancel_late_fee_percent": 60,
      "dropout_redispatch_min_lead_minutes": 120,
      "dropout_decision_lead_minutes": 45,
      "onmyway_compensation_window_minutes": 120,
      "caregiver_late_fee_share_percent": 80,
      "caregiver_visit_share_percent": 75,
      "earning_hold_hours": 48,
      "payout_cadence": "biweekly",
      "payout_min_naira": 5000,
      "finance_refund_limit_naira": 50000
    },
    "reason": "Raise the late fee while we measure no-shows",
    "ip": "172.18.0.1",
    "user_agent": "python-httpx/0.28.1",
    "created_at": "2026-10-07T20:39:03.216582"
  }
]
```

## 12.10 Caregiver earnings (A3): `/api/v1/admin/earnings`

**What it is.** When a visit is completed, the assigned caregiver is credited **one earning** equal to `caregiver_visit_share_percent` (default 80%) of the booking's **service fee**, rounded half up to the kobo. The platform fee and the total are never part of the base. The earning starts `pending_clearance` and becomes withdrawable `earning_hold_hours` (default 24) after completion, when a worker job (every five minutes) releases it. A family cancellation in the late tier that was compensable and kept a fee credits a `late_cancel_compensation` the same way (share = `caregiver_late_fee_share_percent` of the fee kept). All amounts here are **integer kobo**, fields end in `_kobo`.

Money flow: `completed` -> `pending_clearance` -> (release job, after `available_at`) `successful` and the caregiver's available balance goes up. Support staff can put a pending earning on `held` (the job skips it), take the hold off, or `voided` it (never paid). A released earning cannot be changed here.

| Route | Permission | Notes |
|---|---|---|
| `GET /admin/earnings` | `payments.read` (Finance, Super Admin) | filters `caregiver_id`, `booking_id`, `status`; `limit` (1 to 200), `offset`; newest first |
| `POST /admin/earnings/{id}/hold` | `ledger.adjust_request` (Finance, Super Admin) | body `{"reason": "..."}`, reason required |
| `POST /admin/earnings/{id}/release-hold` | `ledger.adjust_request`; **also `ledger.adjust` while the booking is flagged as a no-show** | same body; the earning is pending again and keeps its original `available_at`. For a flagged booking a Finance admin gets `403` with `detail` "This booking is flagged as a no-show; releasing its hold needs the ledger.adjust permission" (**[not live]**: string asserted in `test_caregiver_earnings.py`, not captured); hide or disable the button for them |
| `POST /admin/earnings/{id}/void` | `ledger.adjust` (**Super Admin only**) | same body; also allowed from `held` |

Every hold, release of a hold and void writes an audit entry (`earning.hold`, `earning.release_hold`, `earning.void`, `entity_type` `caregiver_transaction`) with the old and new status and the reason. **[seeded]** The patient, caregiver and bookings are inserted directly; the earnings themselves come from the real completion path (a Super Admin status correction to `completed`). **[db-simulated]** `available_at` was moved into the past to stand in for the 24-hour wait, then the real job function was run once in the worker container (`worker-release-job`).

The list (`GET /admin/earnings`), first item:

<!-- capture: 158-earnings-list.json response.body path=items first=1 -->
```json
[
  {
    "id": "a3138efc-a698-4ea3-b9f3-bc982aed3526",
    "type": "earning",
    "status": "pending_clearance",
    "amount_kobo": 4000000,
    "base_amount_kobo": 5000000,
    "share_percent": 80,
    "caregiver_id": "f9ab2f24-4e17-4252-a0df-1466427d8c34",
    "caregiver_name": "Placeholder Nurse Earnings",
    "booking_id": "52998575-5b0c-4186-9443-d29a4336d9d1",
    "booking_code": "99650F53",
    "available_at": "2026-10-08T20:41:04.317472",
    "released_at": null,
    "status_reason": null,
    "status_changed_by": null,
    "status_changed_at": null,
    "created_at": "2026-10-07T20:41:04.319484"
  }
]
```

Note `base_amount_kobo` (the service fee the share was taken of) and `share_percent` (the percent that was snapshotted on the booking): `amount_kobo` can always be re-derived from them. Filtering by booking:

<!-- capture: 159-earnings-list-filtered.json response.body -->
```json
{
  "items": [
    {
      "id": "3b7c3e27-843a-4423-8a6f-00ee53cdd546",
      "type": "earning",
      "status": "pending_clearance",
      "amount_kobo": 4000000,
      "base_amount_kobo": 5000000,
      "share_percent": 80,
      "caregiver_id": "f9ab2f24-4e17-4252-a0df-1466427d8c34",
      "caregiver_name": "Placeholder Nurse Earnings",
      "booking_id": "3bfae2fb-4d36-450c-8108-735b60f5cf2e",
      "booking_code": "0A521CEA",
      "available_at": "2026-10-08T20:41:04.296220",
      "released_at": null,
      "status_reason": null,
      "status_changed_by": null,
      "status_changed_at": null,
      "created_at": "2026-10-07T20:41:04.299886"
    }
  ],
  "total": 1
}
```

Hold, again, and release the hold:

<!-- capture: 166-earning-hold.json response.body -->
```json
{
  "id": "3b7c3e27-843a-4423-8a6f-00ee53cdd546",
  "type": "earning",
  "status": "held",
  "amount_kobo": 4000000,
  "base_amount_kobo": 5000000,
  "share_percent": 80,
  "caregiver_id": "f9ab2f24-4e17-4252-a0df-1466427d8c34",
  "caregiver_name": "Placeholder Nurse Earnings",
  "booking_id": "3bfae2fb-4d36-450c-8108-735b60f5cf2e",
  "booking_code": "0A521CEA",
  "available_at": "2026-10-08T20:41:04.296220",
  "released_at": null,
  "status_reason": "The family disputes this visit",
  "status_changed_by": "d243ad6f-c5e7-4bda-af33-0e4401313bf6",
  "status_changed_at": "2026-10-07T20:41:04.404903",
  "created_at": "2026-10-07T20:41:04.299886"
}
```

<!-- capture: 167-earning-hold-again.json response.body -->
```json
{
  "detail": "This earning is already on hold"
}
```

<!-- capture: 169-earning-release-hold.json response.body -->
```json
{
  "id": "3b7c3e27-843a-4423-8a6f-00ee53cdd546",
  "type": "earning",
  "status": "pending_clearance",
  "amount_kobo": 4000000,
  "base_amount_kobo": 5000000,
  "share_percent": 80,
  "caregiver_id": "f9ab2f24-4e17-4252-a0df-1466427d8c34",
  "caregiver_name": "Placeholder Nurse Earnings",
  "booking_id": "3bfae2fb-4d36-450c-8108-735b60f5cf2e",
  "booking_code": "0A521CEA",
  "available_at": "2026-10-08T20:41:04.296220",
  "released_at": null,
  "status_reason": "Dispute resolved",
  "status_changed_by": "d243ad6f-c5e7-4bda-af33-0e4401313bf6",
  "status_changed_at": "2026-10-07T20:41:04.428779",
  "created_at": "2026-10-07T20:41:04.299886"
}
```

<!-- capture: 170-earning-release-hold-again.json response.body -->
```json
{
  "detail": "This earning is not on hold"
}
```

Void (Super Admin) and what a voided earning refuses:

<!-- capture: 172-earning-void.json response.body -->
```json
{
  "id": "a0946e7f-6f17-4beb-b16c-9b0b1b87f2fd",
  "type": "earning",
  "status": "voided",
  "amount_kobo": 4000000,
  "base_amount_kobo": 5000000,
  "share_percent": 80,
  "caregiver_id": "f9ab2f24-4e17-4252-a0df-1466427d8c34",
  "caregiver_name": "Placeholder Nurse Earnings",
  "booking_id": "3751ded6-e371-4bbc-b7bf-84c8bef47c4e",
  "booking_code": "B0EBFBB0",
  "available_at": "2026-10-08T20:41:04.307997",
  "released_at": null,
  "status_reason": "Caregiver never attended",
  "status_changed_by": "df20089a-d1c4-4d5f-bcd8-c5ec3c32455d",
  "status_changed_at": "2026-10-07T20:41:04.445614",
  "created_at": "2026-10-07T20:41:04.310090"
}
```

<!-- capture: 173-earning-void-again.json response.body -->
```json
{
  "detail": "This earning has been voided"
}
```

Finance cannot void, Support cannot hold, Operations cannot read, no token:

<!-- capture: 171-earning-void-as-finance.json response.body -->
```json
{
  "detail": "Access denied. Missing permission: ledger.adjust"
}
```

<!-- capture: 163-earning-hold-as-support.json response.body -->
```json
{
  "detail": "Access denied. Missing permission: ledger.adjust_request"
}
```

<!-- capture: 160-earnings-list-as-operations.json response.body -->
```json
{
  "detail": "Access denied. Missing permission: payments.read"
}
```

<!-- capture: 161-earnings-list-no-token.json response.body -->
```json
{
  "detail": "Not authenticated"
}
```

Other errors: a missing `reason` is `422`, an unknown earning is `404`, a bad `status` filter is `422`:

<!-- capture: 164-earning-hold-no-reason.json response.body -->
```json
{
  "detail": [
    {
      "type": "missing",
      "loc": [
        "body",
        "reason"
      ],
      "msg": "Field required",
      "input": {}
    }
  ]
}
```

<!-- capture: 165-earning-hold-unknown.json response.body -->
```json
{
  "detail": "Earning not found"
}
```

<!-- capture: 162-earnings-list-bad-status.json response.body -->
```json
{
  "detail": [
    {
      "type": "literal_error",
      "loc": [
        "query",
        "status"
      ],
      "msg": "Input should be 'pending_clearance', 'held', 'successful' or 'voided'",
      "input": "bogus",
      "ctx": {
        "expected": "'pending_clearance', 'held', 'successful' or 'voided'"
      }
    }
  ]
}
```

**The no-show flag.** Flagging a no-show holds the booking's pending earning. The flag is only offered before a visit starts, so there is normally no earning yet; if an admin then completes that booking, the earning is **credited already `held`** with the no-show as the reason, and it cannot clear until Finance releases the hold. Flag, complete, and the earning:

<!-- capture: 177-earning-of-flagged-visit.json response.body path=items first=1 -->
```json
[
  {
    "id": "93a76a78-39a2-4050-bff6-602d9dee686c",
    "type": "earning",
    "status": "held",
    "amount_kobo": 4000000,
    "base_amount_kobo": 5000000,
    "share_percent": 80,
    "caregiver_id": "f9ab2f24-4e17-4252-a0df-1466427d8c34",
    "caregiver_name": "Placeholder Nurse Earnings",
    "booking_id": "54dfa5dd-f099-4bf0-8212-95f614ffa143",
    "booking_code": "CE454A59",
    "available_at": "2026-10-08T20:41:04.468853",
    "released_at": null,
    "status_reason": "No-show flagged on this booking",
    "status_changed_by": "11f2512a-b186-41d5-a07a-81b10c5d2aec",
    "status_changed_at": "2026-10-07T20:41:04.468853",
    "created_at": "2026-10-07T20:41:04.470590"
  }
]
```

**Release job.** Of the four earnings in this run, the two pending ones were released; the voided one and the no-show one (held) were left alone (`worker-release-job` body: `{"released": 2}`). After it, a hold on a released earning is refused:

<!-- capture: 181-earning-hold-released.json response.body -->
```json
{
  "detail": "This earning has already been released to the caregiver's balance; correct it with a ledger adjustment"
}
```

The audit entries (`GET /admin/audit?entity_type=caregiver_transaction`), newest first:

<!-- capture: 182-audit-earnings.json response.body path=items first=2 -->
```json
[
  {
    "id": "e4dab3b1-6229-4777-b52a-f4e0dd690bb7",
    "admin_user_id": "df20089a-d1c4-4d5f-bcd8-c5ec3c32455d",
    "admin_email": "boss-f722ceearn@example.com",
    "action": "earning.void",
    "entity_type": "caregiver_transaction",
    "entity_id": "a0946e7f-6f17-4beb-b16c-9b0b1b87f2fd",
    "before": {
      "status": "pending_clearance",
      "amount": 4000000,
      "available_at": "2026-10-08T20:41:04.307997",
      "booking_id": "3751ded6-e371-4bbc-b7bf-84c8bef47c4e",
      "caregiver_id": "f9ab2f24-4e17-4252-a0df-1466427d8c34",
      "type": "earning"
    },
    "after": {
      "status": "voided",
      "amount": 4000000,
      "available_at": "2026-10-08T20:41:04.307997",
      "booking_id": "3751ded6-e371-4bbc-b7bf-84c8bef47c4e",
      "caregiver_id": "f9ab2f24-4e17-4252-a0df-1466427d8c34",
      "type": "earning"
    },
    "reason": "Caregiver never attended",
    "ip": "172.18.0.1",
    "user_agent": "python-httpx/0.28.1",
    "created_at": "2026-10-07T20:41:04.445850"
  },
  {
    "id": "da51b602-b98a-4741-973c-1bdc77ef727b",
    "admin_user_id": "d243ad6f-c5e7-4bda-af33-0e4401313bf6",
    "admin_email": "fin-f722ceearn@example.com",
    "action": "earning.release_hold",
    "entity_type": "caregiver_transaction",
    "entity_id": "3b7c3e27-843a-4423-8a6f-00ee53cdd546",
    "before": {
      "status": "held",
      "amount": 4000000,
      "available_at": "2026-10-08T20:41:04.296220",
      "booking_id": "3bfae2fb-4d36-450c-8108-735b60f5cf2e",
      "caregiver_id": "f9ab2f24-4e17-4252-a0df-1466427d8c34",
      "type": "earning"
    },
    "after": {
      "status": "pending_clearance",
      "amount": 4000000,
      "available_at": "2026-10-08T20:41:04.296220",
      "booking_id": "3bfae2fb-4d36-450c-8108-735b60f5cf2e",
      "caregiver_id": "f9ab2f24-4e17-4252-a0df-1466427d8c34",
      "type": "earning"
    },
    "reason": "Dispute resolved",
    "ip": "172.18.0.1",
    "user_agent": "python-httpx/0.28.1",
    "created_at": "2026-10-07T20:41:04.429014"
  }
]
```

**UX the console must surface.**
- Voiding writes money off for good. Use a confirmation that says so; a voided earning cannot be reinstated here.
- Show `status` and `available_at` on every row. A `held` row has no countdown: the caregiver sees it under "held", not "pending".
- A `successful` earning is already in the caregiver's balance; the only correction is a ledger adjustment (a later work package). Disable hold and void on it and show the `409` text if it races the job.
- Caregivers can no longer withdraw: their available balance is paid out on a schedule (section 12.11). A `successful` earning is in the available balance and is paid with the next batch.

## 12.11 Caregiver payouts (A3 slice 2): `/api/v1/admin/payouts`

**What it is.** Caregivers do not withdraw. On each payout day (policy `payout_cadence`: `weekly` = Mondays, `biweekly` = Mondays of even ISO weeks, `monthly` = the 1st; `payout_min_naira` is the smallest balance paid, 5,000 by default; both are edited through `PATCH /admin/booking-policy`, section 12.9) a job makes ONE **batch** at 06:00 UTC (the cron entry is `06:00:10`). The batch holds one **payout** per caregiver whose **available balance** is at least the minimum and who has a verified bank account, is not suspended or erased, did not change bank account in the last 24 hours and has no live payout. **The payout is the caregiver's whole available balance, and it is taken out of that balance the moment the batch is made** (reserved), so the money cannot be spent twice. Nothing is sent to the bank until an admin **approves the batch**. All amounts are **integer kobo**, fields end in `_kobo`.

Money flow of a payout: `pending_approval` -> (admin approves the batch) `processing` -> (send job posts the Paystack transfer, Paystack answers by webhook) `success`, or `failed` / `reversed` (Paystack failed it, or reversed a transfer it had never reported as paid: the money returns to the caregiver's available balance and an admin can retry) , or `cancelled` (before approval, or by the send job when the caregiver is no longer payable; the money returns), or **`needs_attention`**: a person must look (see "Payouts that need a person" below). **`needs_attention` means the caregiver is frozen**: Paystack's word contradicted what Oakly recorded, so from then on automatic code may only lower what the caregiver can be paid, never raise it. They are left out of new batches, nothing is sent for them, and **no money returns to their balance by itself** (a `failed`, `reversed` or even reversed-after-`success` event only changes the payout to `needs_attention` and keeps its money reserved). Money returns only through an admin: **resolve as `failed`** (net of any shortfall), then **unfreeze** the caregiver (`POST /admin/payouts/caregivers/{id}/unfreeze`).

| Route | Permission | Notes |
|---|---|---|
| `GET /admin/payouts/batches` | `payments.read` (Finance, Super Admin) | filter `status` (`pending_approval`, `approved`, `cancelled`); `limit` (1 to 200), `offset`; newest first. Each batch carries `payout_count`, `total_kobo` (cancelled payouts excluded) and `counts` (a map status -> number) |
| `GET /admin/payouts/batches/{id}` | `payments.read` | the batch plus its `payouts` |
| `GET /admin/payouts` | `payments.read` | filters `batch_id`, `caregiver_id`, `status` (`pending_approval`, `processing`, `success`, `failed`, `reversed`, `cancelled`, `needs_attention`); `limit`, `offset`; newest first |
| `POST /admin/payouts/batches/{id}/approve` | `payouts.execute` (Finance, Super Admin) | body `{"reason": "..."}`, reason required; returns the batch with its payouts |
| `POST /admin/payouts/{id}/cancel` | `payouts.execute` (Finance, Super Admin) | same body; only a payout that has **not been approved** |
| `POST /admin/payouts/batches/{id}/cancel` | `payouts.execute` (Finance, Super Admin) | same body; only a batch that has **not been approved**: every payout waiting in it is cancelled and its money returned; returns the batch with its payouts |
| `POST /admin/payouts/{id}/retry` | `ledger.adjust` (**Super Admin only**) | same body; only a `failed` or `reversed` payout, and only once Paystack has confirmed the failure (it is asked first) |
| `POST /admin/payouts/{id}/resolve` | `ledger.adjust` (**Super Admin only**) | body `{"outcome": "paid" or "failed", "reason": "...", "force": false}`; only a `processing` or `needs_attention` payout; Paystack is asked first; `failed` returns the reserved money **net of the caregiver's shortfall** |
| `POST /admin/payouts/caregivers/{caregiver_id}/unfreeze` | `ledger.adjust` (**Super Admin only**) | body `{"reason": "...", "write_off_shortfall": false}`; lets a frozen caregiver be paid again; refused while any of their payouts is `needs_attention` or `processing`; **debits any outstanding shortfall from the caregiver's available balance first**, and a shortfall the balance cannot cover is refused (`409`) unless `write_off_shortfall` is `true`; returns `{caregiver_id, payouts_frozen_at: null, debited_from_balance_kobo, written_off_shortfall_kobo, outstanding_shortfall_kobo}` |

**Finance can approve and cancel, but not retry, resolve or unfreeze.** Approving a batch and cancelling a batch or a payout need `payouts.execute`, which Finance and the Super Admin hold; retry, resolve (with or without `force`) and unfreeze need `ledger.adjust`, which only the Super Admin holds. Show Approve and Cancel to anyone whose `permissions` (from `GET /admin/me`) include `payouts.execute`, and Retry, Resolve and Unfreeze only to those with `ledger.adjust`; a missing permission is `403` `Access denied. Missing permission: payouts.execute` or `Access denied. Missing permission: ledger.adjust` (captures `194`, `211`). The admin who acted is recorded on the payout (`approved_by`, `cancelled_by`) and in the audit log.

**There is no "create batch" route.** The scheduled job makes batches. If it was down on a payout day nothing is created until the next payout day; a developer can run it by hand (`app.services.payouts.create_batch`). **[seeded]** The caregivers, their available balances and their verified bank accounts are inserted directly (a caregiver does this with `PUT /caregiver/bank-account`, see the caregiver guide). **[simulated schedule]** The batch, send and reconcile steps are the **real functions** run inside the worker container, standing in for the ARQ cron; the batch is for a date a week ahead because the caregiver capture run used today's. **[fake Paystack]** Every Paystack answer and webhook in these captures comes from a **local fake** shaped after Paystack's documented replies; the webhooks are correctly signed with the stack's placeholder key. **A live Paystack account has not been exercised.**

The seeding (two caregivers are left out on purpose: one is below the minimum, one changed bank account 12 hours ago):

<!-- capture: 183-db-seeded-payout-caregivers.json response.body -->
```json
{
  "caregivers": {
    "Placeholder Paid": {
      "available_kobo": 1200000,
      "bank_account": "verified"
    },
    "Placeholder Failing": {
      "available_kobo": 3000000,
      "bank_account": "verified"
    },
    "Placeholder Cancelled": {
      "available_kobo": 800000,
      "bank_account": "verified"
    },
    "Placeholder Small Balance": {
      "available_kobo": 300000,
      "bank_account": "verified"
    },
    "Placeholder Cooling Down": {
      "available_kobo": 2000000,
      "bank_account": "verified, changed 12 hours ago"
    }
  }
}
```

The batch step (`payouts_made` is 3 of 5):

<!-- capture: 184-worker-payout-batch-job.json response.body -->
```json
{
  "period_key": "2026-10-26",
  "payouts_made": 6
}
```

### Reading batches and payouts

**Six fields are new on every payout object** (list, batch detail and every action's response): `attempt_history` (a list, oldest first: `attempt`, `reference`, `status` (`pending`, `success`, `failed`, `reversed` or `refused`), `failure_reason`, `created_at`); `shortfall_kobo` (an integer, `0` unless Paystack paid out money the caregiver's balance could not cover; it only goes down when an admin nets it off by resolving another payout as `failed`, or at unfreeze, when it is debited from the caregiver's balance or written off); `returned_kobo` and `netted_kobo` (integers, `0` unless an admin resolved this payout as `failed`: how much of its reservation went back to the balance and how much was kept against the caregiver's shortfall); and `caregiver_payouts_frozen_at` and `caregiver_payouts_frozen_reason` (the caregiver's freeze: **present on every payout, `null` when the caregiver is not frozen**, never absent). **Traps:** `attempts` is the *number* of the current attempt, `attempt_history` is the *list*, and `reference` (and `transfer_code`) on the payout belong to the **current** attempt only. A retry adds an attempt; it never overwrites an earlier one, so the reason an attempt failed stays on its row in `attempt_history` even though the payout's own `failure_reason` is cleared when it is retried. **The freeze fields belong to the caregiver, not the payout**: they are the same on all of a caregiver's payouts, including a `success` or `failed` one, so do not read "frozen" as a status of the payout it appears on.

The batch list. `counts` is a map, so a status with no payouts is **absent**, not `0`:

<!-- capture: 185-payout-batches-list.json response.body -->
```json
{
  "items": [
    {
      "id": "ecd85e88-3c30-4858-8046-69b2ed56fe3a",
      "period_key": "2026-10-26",
      "cadence": "weekly",
      "min_payout_kobo": 500000,
      "status": "pending_approval",
      "approved_by": null,
      "approved_at": null,
      "approval_reason": null,
      "created_at": "2026-10-19T13:29:07.792983",
      "payout_count": 6,
      "total_kobo": 10000000,
      "counts": {
        "pending_approval": 6
      }
    }
  ],
  "total": 1
}
```

_capture `185-payout-batches-list.json`_

The batch detail with its payouts (before approval). `account_number` is **masked to the last four digits** for admins. `reference` is Oakly's unique Paystack reference for the **current attempt** (`po-<payout id>-<attempt>`); a retry changes it. `transfer_code` is Paystack's, `null` until a transfer exists. `initiated_at` is when the send job claimed the payout; `null` until then. `approved_by`, `cancelled_by`, `completed_at` and the like are present with `null`, never absent:

<!-- capture: 187-payout-batch-detail.json response.body -->
```json
{
  "id": "ecd85e88-3c30-4858-8046-69b2ed56fe3a",
  "period_key": "2026-10-26",
  "cadence": "weekly",
  "min_payout_kobo": 500000,
  "status": "pending_approval",
  "approved_by": null,
  "approved_at": null,
  "approval_reason": null,
  "created_at": "2026-10-19T13:29:07.792983",
  "payout_count": 6,
  "total_kobo": 10000000,
  "counts": {
    "pending_approval": 6
  },
  "payouts": [
    {
      "id": "d78d3234-95ec-4a0c-9241-83d4adf920d4",
      "batch_id": "ecd85e88-3c30-4858-8046-69b2ed56fe3a",
      "batch_period": "2026-10-26",
      "caregiver_id": "1ea14f8e-28ac-4c89-bfd4-15c81e8487f5",
      "caregiver_name": "Placeholder Failing",
      "amount_kobo": 3000000,
      "status": "pending_approval",
      "bank_name": "Placeholder Bank",
      "account_number": "******0012",
      "account_name": "PLACEHOLDER FAILING",
      "reference": "po-d78d323495ec4a0c924183d4adf920d4-1",
      "transfer_code": null,
      "attempts": 1,
      "attempt_history": [
        {
          "attempt": 1,
          "reference": "po-d78d323495ec4a0c924183d4adf920d4-1",
          "status": "pending",
          "failure_reason": null,
          "created_at": "2026-10-19T13:29:07.830090"
        }
      ],
      "shortfall_kobo": 0,
      "returned_kobo": 0,
      "netted_kobo": 0,
      "caregiver_payouts_frozen_at": null,
      "caregiver_payouts_frozen_reason": null,
      "failure_reason": null,
      "initiated_at": null,
      "completed_at": null,
      "approved_by": null,
      "approved_at": null,
      "cancelled_by": null,
      "cancelled_at": null,
      "cancel_reason": null,
      "created_at": "2026-10-19T13:29:07.822496"
    },
    {
      "id": "b49ceee7-a2cf-44eb-8216-2f877602bd77",
      "batch_id": "ecd85e88-3c30-4858-8046-69b2ed56fe3a",
      "batch_period": "2026-10-26",
      "caregiver_id": "5d4824cb-7fa1-48e8-bc68-b7ae067bef4a",
      "caregiver_name": "Placeholder Cancelled",
      "amount_kobo": 800000,
      "status": "pending_approval",
      "bank_name": "Placeholder Bank",
      "account_number": "******0013",
      "account_name": "PLACEHOLDER CANCELLED",
      "reference": "po-b49ceee7a2cf44eb82162f877602bd77-1",
      "transfer_code": null,
      "attempts": 1,
      "attempt_history": [
        {
          "attempt": 1,
          "reference": "po-b49ceee7a2cf44eb82162f877602bd77-1",
          "status": "pending",
          "failure_reason": null,
          "created_at": "2026-10-19T13:29:07.845527"
        }
      ],
      "shortfall_kobo": 0,
      "returned_kobo": 0,
      "netted_kobo": 0,
      "caregiver_payouts_frozen_at": null,
      "caregiver_payouts_frozen_reason": null,
      "failure_reason": null,
      "initiated_at": null,
      "completed_at": null,
      "approved_by": null,
      "approved_at": null,
      "cancelled_by": null,
      "cancelled_at": null,
      "cancel_reason": null,
      "created_at": "2026-10-19T13:29:07.844168"
    },
    {
      "id": "87e77b91-5f35-4ea9-b8ea-5b7919230d4b",
      "batch_id": "ecd85e88-3c30-4858-8046-69b2ed56fe3a",
      "batch_period": "2026-10-26",
      "caregiver_id": "63307397-54eb-4fef-96fe-64086aead8d9",
      "caregiver_name": "Placeholder Cancelled",
      "amount_kobo": 800000,
      "status": "pending_approval",
      "bank_name": "Placeholder Bank",
      "account_number": "******0013",
      "account_name": "PLACEHOLDER CANCELLED",
      "reference": "po-87e77b915f354ea9b8ea5b7919230d4b-1",
      "transfer_code": null,
      "attempts": 1,
      "attempt_history": [
        {
          "attempt": 1,
          "reference": "po-87e77b915f354ea9b8ea5b7919230d4b-1",
          "status": "pending",
          "failure_reason": null,
          "created_at": "2026-10-19T13:29:07.857097"
        }
      ],
      "shortfall_kobo": 0,
      "returned_kobo": 0,
      "netted_kobo": 0,
      "caregiver_payouts_frozen_at": null,
      "caregiver_payouts_frozen_reason": null,
      "failure_reason": null,
      "initiated_at": null,
      "completed_at": null,
      "approved_by": null,
      "approved_at": null,
      "cancelled_by": null,
      "cancelled_at": null,
      "cancel_reason": null,
      "created_at": "2026-10-19T13:29:07.854789"
    },
    {
      "id": "095b3872-1a06-47e7-b604-78c9ba893f04",
      "batch_id": "ecd85e88-3c30-4858-8046-69b2ed56fe3a",
      "batch_period": "2026-10-26",
      "caregiver_id": "7bc6c2b0-da51-4c84-908d-22808c19abe8",
      "caregiver_name": "Placeholder Failing",
      "amount_kobo": 3000000,
      "status": "pending_approval",
      "bank_name": "Placeholder Bank",
      "account_number": "******0012",
      "account_name": "PLACEHOLDER FAILING",
      "reference": "po-095b38721a0647e7b60478c9ba893f04-1",
      "transfer_code": null,
      "attempts": 1,
      "attempt_history": [
        {
          "attempt": 1,
          "reference": "po-095b38721a0647e7b60478c9ba893f04-1",
          "status": "pending",
          "failure_reason": null,
          "created_at": "2026-10-19T13:29:07.866533"
        }
      ],
      "shortfall_kobo": 0,
      "returned_kobo": 0,
      "netted_kobo": 0,
      "caregiver_payouts_frozen_at": null,
      "caregiver_payouts_frozen_reason": null,
      "failure_reason": null,
      "initiated_at": null,
      "completed_at": null,
      "approved_by": null,
      "approved_at": null,
      "cancelled_by": null,
      "cancelled_at": null,
      "cancel_reason": null,
      "created_at": "2026-10-19T13:29:07.865568"
    },
    {
      "id": "f11dc9d6-778b-4433-8e31-b51251bb86cd",
      "batch_id": "ecd85e88-3c30-4858-8046-69b2ed56fe3a",
      "batch_period": "2026-10-26",
      "caregiver_id": "a64b9bdc-eb3b-41ba-b0c1-0258743623c0",
      "caregiver_name": "Placeholder Paid",
      "amount_kobo": 1200000,
      "status": "pending_approval",
      "bank_name": "Placeholder Bank",
      "account_number": "******0011",
      "account_name": "PLACEHOLDER PAID",
      "reference": "po-f11dc9d6778b44338e31b51251bb86cd-1",
      "transfer_code": null,
      "attempts": 1,
      "attempt_history": [
        {
          "attempt": 1,
          "reference": "po-f11dc9d6778b44338e31b51251bb86cd-1",
          "status": "pending",
          "failure_reason": null,
          "created_at": "2026-10-19T13:29:07.876729"
        }
      ],
      "shortfall_kobo": 0,
      "returned_kobo": 0,
      "netted_kobo": 0,
      "caregiver_payouts_frozen_at": null,
      "caregiver_payouts_frozen_reason": null,
      "failure_reason": null,
      "initiated_at": null,
      "completed_at": null,
      "approved_by": null,
      "approved_at": null,
      "cancelled_by": null,
      "cancelled_at": null,
      "cancel_reason": null,
      "created_at": "2026-10-19T13:29:07.875618"
    },
    {
      "id": "1ca00fde-0db3-4909-8256-65ebd86c2d6b",
      "batch_id": "ecd85e88-3c30-4858-8046-69b2ed56fe3a",
      "batch_period": "2026-10-26",
      "caregiver_id": "d83d80b4-55bd-4083-bd48-db16338690dc",
      "caregiver_name": "Placeholder Paid",
      "amount_kobo": 1200000,
      "status": "pending_approval",
      "bank_name": "Placeholder Bank",
      "account_number": "******0011",
      "account_name": "PLACEHOLDER PAID",
      "reference": "po-1ca00fde0db34909825665ebd86c2d6b-1",
      "transfer_code": null,
      "attempts": 1,
      "attempt_history": [
        {
          "attempt": 1,
          "reference": "po-1ca00fde0db34909825665ebd86c2d6b-1",
          "status": "pending",
          "failure_reason": null,
          "created_at": "2026-10-19T13:29:07.883855"
        }
      ],
      "shortfall_kobo": 0,
      "returned_kobo": 0,
      "netted_kobo": 0,
      "caregiver_payouts_frozen_at": null,
      "caregiver_payouts_frozen_reason": null,
      "failure_reason": null,
      "initiated_at": null,
      "completed_at": null,
      "approved_by": null,
      "approved_at": null,
      "cancelled_by": null,
      "cancelled_at": null,
      "cancel_reason": null,
      "created_at": "2026-10-19T13:29:07.882962"
    }
  ]
}
```

_capture `187-payout-batch-detail.json`_

Filters and errors:
- Unknown batch id: `404` `{"detail": "Payout batch not found"}` _(capture `188-payout-batch-unknown.json`)_
- Bad `status` filter: `422`, the standard validation list _(capture `191-payouts-list-bad-status.json`)_
- Operations (no `payments.read`): `403` _(capture `192-payouts-list-as-operations.json`)_; no token: `401` _(capture `193-payouts-list-no-token.json`)_
- `GET /admin/payouts?caregiver_id=...` and `?batch_id=...`: captures `189` and `190`.

### Cancel a payout (before approval)

Cancelling returns the reserved money to the caregiver's available balance (8,000.00 NGN here). The caregiver's ledger row becomes `cancelled`. When the last waiting payout of a batch is cancelled the batch itself becomes `cancelled` and can no longer be approved.

<!-- capture: 197-payout-cancel.json response.body -->
```json
{
  "id": "b49ceee7-a2cf-44eb-8216-2f877602bd77",
  "batch_id": "ecd85e88-3c30-4858-8046-69b2ed56fe3a",
  "batch_period": "2026-10-26",
  "caregiver_id": "5d4824cb-7fa1-48e8-bc68-b7ae067bef4a",
  "caregiver_name": "Placeholder Cancelled",
  "amount_kobo": 800000,
  "status": "cancelled",
  "bank_name": "Placeholder Bank",
  "account_number": "******0013",
  "account_name": "PLACEHOLDER CANCELLED",
  "reference": "po-b49ceee7a2cf44eb82162f877602bd77-1",
  "transfer_code": null,
  "attempts": 1,
  "attempt_history": [
    {
      "attempt": 1,
      "reference": "po-b49ceee7a2cf44eb82162f877602bd77-1",
      "status": "pending",
      "failure_reason": null,
      "created_at": "2026-10-19T13:29:07.845527"
    }
  ],
  "shortfall_kobo": 0,
  "returned_kobo": 0,
  "netted_kobo": 0,
  "caregiver_payouts_frozen_at": null,
  "caregiver_payouts_frozen_reason": null,
  "failure_reason": null,
  "initiated_at": null,
  "completed_at": null,
  "approved_by": null,
  "approved_at": null,
  "cancelled_by": "782b30c4-afec-4093-b436-94b567e953b1",
  "cancelled_at": "2026-10-19T13:29:08.513278",
  "cancel_reason": "Caregiver asked us to hold this payout",
  "created_at": "2026-10-19T13:29:07.844168"
}
```

_capture `197-payout-cancel.json`_

| Status | `detail` | When |
|---|---|---|
| 403 | `Access denied. Missing permission: payouts.execute` | Operations, or any role without it _(capture `194`)_ |
| 422 | the validation list | no `reason`, or a blank one _(capture `195`)_ |
| 404 | `Payout not found` | unknown id _(capture `196`)_ |
| 409 | `This payout is already cancelled` | again _(capture `198`)_ |
| 409 | `Only a payout that has not been approved can be cancelled` | after approval _(capture `206`)_ |

### Cancel a whole batch (before approval)

`POST /admin/payouts/batches/{id}/cancel` with `{"reason": "..."}`, `payouts.execute`. Every payout still waiting in a batch that has not been approved is cancelled in one transaction and each caregiver's reserved money returns to their available balance (the next batch can pay it). Use it when the totals are wrong; **after approval it is refused** and only Paystack's answers can end a payout. The batch becomes `cancelled`; the response is the batch with its payouts, now `cancelled` with `cancelled_by` set to the admin.

Request body, then the response `200` (trimmed to the batch fields; `payouts` carries the cancelled payout objects):

<!-- capture: 280-payout-batch-cancel.json request.body -->
```json
{
  "reason": "Placeholder: the totals do not match Finance's report"
}
```

<!-- capture: 280-payout-batch-cancel.json response.body keys=id,period_key,status,approved_by,approval_reason,payout_count,total_kobo,counts -->
```json
{
  "id": "b0320517-068c-429f-8afb-16295b8ab814",
  "period_key": "2026-11-09",
  "status": "cancelled",
  "approved_by": null,
  "approval_reason": null,
  "payout_count": 5,
  "total_kobo": 0,
  "counts": {
    "cancelled": 5
  }
}
```

_capture `280-payout-batch-cancel.json`_

| Status | `detail` | When |
|---|---|---|
| 403 | `Access denied. Missing permission: payouts.execute` | Operations, or any role without it _(capture `277`)_ |
| 422 | the validation list | no or blank `reason` _(capture `278`)_ |
| 404 | `Payout batch not found` | unknown id _(capture `279`)_ |
| 409 | `This batch is already cancelled` | again _(capture `281`)_ |
| 409 | `This batch is already approved` | after approval _(capture `282`)_ |
| 409 | `This batch has no payouts waiting for approval` | **[not live]** (asserted in `test_payout_hardening.py`) |

### Approve a batch

Approval needs a reason and `payouts.execute` (Finance or Super Admin). It moves every payout still waiting to `processing`; **it sends nothing itself**: the send job (every minute) posts one Paystack transfer per payout, each with that payout's own reference, so a repeat can never pay twice. **Before moving a payout, approval checks the caregiver again**: one who was suspended or erased, or who changed bank account since the batch was made, is **cancelled instead and the money returned** (`cancelled_by` is `null`, `cancel_reason` starts "Not paid when the batch was approved: ..."); the batch response shows it. **[not live]** This check was not captured; it is asserted in `test_payouts.py`.

Request body:

<!-- capture: 204-payout-batch-approve.json request.body -->
```json
{
  "reason": "Placeholder: totals checked against the ledger"
}
```

Response `200` (trimmed here to the batch fields; the real response also carries `payouts`, the same objects as the batch detail, now `processing`, and the cancelled one stays `cancelled`):

<!-- capture: 204-payout-batch-approve.json response.body keys=id,period_key,status,approved_by,approved_at,approval_reason,payout_count,total_kobo,counts -->
```json
{
  "id": "ecd85e88-3c30-4858-8046-69b2ed56fe3a",
  "period_key": "2026-10-26",
  "status": "approved",
  "approved_by": "782b30c4-afec-4093-b436-94b567e953b1",
  "approved_at": "2026-10-19T13:29:08.595981",
  "approval_reason": "Placeholder: totals checked against the ledger",
  "payout_count": 6,
  "total_kobo": 9200000,
  "counts": {
    "cancelled": 1,
    "processing": 5
  }
}
```

_capture `204-payout-batch-approve.json`_

| Status | `detail` | When |
|---|---|---|
| 403 | `Access denied. Missing permission: payouts.execute` | Operations, or any role without it _(capture `200`)_ |
| 422 | the validation list | no or blank `reason` _(captures `201`, `202`)_ |
| 404 | `Payout batch not found` | unknown id _(capture `203`)_ |
| 409 | `This batch is already approved` (or `cancelled`) | approving again _(capture `205`)_ |
| 409 | `This batch has no payouts waiting for approval` | **[not live]** (asserted in `test_payouts.py`) |

### Sending, and what Paystack answers

The send step (the real function, in the worker container). The fake's balance was set to 20,000.00 NGN, so the 12,000.00 transfer was accepted and the 30,000.00 one refused with Paystack's "balance is not enough" wording:

<!-- capture: 207-worker-payout-dispatch-job.json response.body -->
```json
{
  "transfers_sent": 5,
  "transfer_requests_seen_by_the_fake": [
    {
      "amount": 3000000,
      "reference": "po-095b38721a0647e7b60478c9ba893f04-1"
    },
    {
      "amount": 1200000,
      "reference": "po-1ca00fde0db34909825665ebd86c2d6b-1"
    },
    {
      "amount": 800000,
      "reference": "po-87e77b915f354ea9b8ea5b7919230d4b-1"
    },
    {
      "amount": 3000000,
      "reference": "po-d78d323495ec4a0c924183d4adf920d4-1"
    },
    {
      "amount": 1200000,
      "reference": "po-f11dc9d6778b44338e31b51251bb86cd-1"
    }
  ]
}
```

After the send: the accepted payout is `processing` with a `transfer_code`; the refused one is `failed`, **its money is back in the caregiver's available balance**, and `failure_reason` says why. The list is newest first and the cancelled payout is in it too:

<!-- capture: 208-payouts-list-after-send.json response.body path=items first=3 -->
```json
[
  {
    "id": "1ca00fde-0db3-4909-8256-65ebd86c2d6b",
    "batch_id": "ecd85e88-3c30-4858-8046-69b2ed56fe3a",
    "batch_period": "2026-10-26",
    "caregiver_id": "d83d80b4-55bd-4083-bd48-db16338690dc",
    "caregiver_name": "Placeholder Paid",
    "amount_kobo": 1200000,
    "status": "processing",
    "bank_name": "Placeholder Bank",
    "account_number": "******0011",
    "account_name": "PLACEHOLDER PAID",
    "reference": "po-1ca00fde0db34909825665ebd86c2d6b-1",
    "transfer_code": "TRF_1053533f080b",
    "attempts": 1,
    "attempt_history": [
      {
        "attempt": 1,
        "reference": "po-1ca00fde0db34909825665ebd86c2d6b-1",
        "status": "pending",
        "failure_reason": null,
        "created_at": "2026-10-19T13:29:07.883855"
      }
    ],
    "shortfall_kobo": 0,
    "returned_kobo": 0,
    "netted_kobo": 0,
    "caregiver_payouts_frozen_at": null,
    "caregiver_payouts_frozen_reason": null,
    "failure_reason": null,
    "initiated_at": "2026-10-19T13:29:11.459737",
    "completed_at": null,
    "approved_by": "782b30c4-afec-4093-b436-94b567e953b1",
    "approved_at": "2026-10-19T13:29:08.595981",
    "cancelled_by": null,
    "cancelled_at": null,
    "cancel_reason": null,
    "created_at": "2026-10-19T13:29:07.882962"
  },
  {
    "id": "f11dc9d6-778b-4433-8e31-b51251bb86cd",
    "batch_id": "ecd85e88-3c30-4858-8046-69b2ed56fe3a",
    "batch_period": "2026-10-26",
    "caregiver_id": "a64b9bdc-eb3b-41ba-b0c1-0258743623c0",
    "caregiver_name": "Placeholder Paid",
    "amount_kobo": 1200000,
    "status": "failed",
    "bank_name": "Placeholder Bank",
    "account_number": "******0011",
    "account_name": "PLACEHOLDER PAID",
    "reference": "po-f11dc9d6778b44338e31b51251bb86cd-1",
    "transfer_code": null,
    "attempts": 1,
    "attempt_history": [
      {
        "attempt": 1,
        "reference": "po-f11dc9d6778b44338e31b51251bb86cd-1",
        "status": "refused",
        "failure_reason": "Paystack refused the transfer: Recipient specified is invalid",
        "created_at": "2026-10-19T13:29:07.876729"
      }
    ],
    "shortfall_kobo": 0,
    "returned_kobo": 0,
    "netted_kobo": 0,
    "caregiver_payouts_frozen_at": null,
    "caregiver_payouts_frozen_reason": null,
    "failure_reason": "Paystack refused the transfer: Recipient specified is invalid",
    "initiated_at": "2026-10-19T13:29:11.505852",
    "completed_at": "2026-10-19T13:29:11.510194",
    "approved_by": "782b30c4-afec-4093-b436-94b567e953b1",
    "approved_at": "2026-10-19T13:29:08.595981",
    "cancelled_by": null,
    "cancelled_at": null,
    "cancel_reason": null,
    "created_at": "2026-10-19T13:29:07.875618"
  },
  {
    "id": "095b3872-1a06-47e7-b604-78c9ba893f04",
    "batch_id": "ecd85e88-3c30-4858-8046-69b2ed56fe3a",
    "batch_period": "2026-10-26",
    "caregiver_id": "7bc6c2b0-da51-4c84-908d-22808c19abe8",
    "caregiver_name": "Placeholder Failing",
    "amount_kobo": 3000000,
    "status": "failed",
    "bank_name": "Placeholder Bank",
    "account_number": "******0012",
    "account_name": "PLACEHOLDER FAILING",
    "reference": "po-095b38721a0647e7b60478c9ba893f04-1",
    "transfer_code": null,
    "attempts": 1,
    "attempt_history": [
      {
        "attempt": 1,
        "reference": "po-095b38721a0647e7b60478c9ba893f04-1",
        "status": "refused",
        "failure_reason": "Paystack refused the transfer: Recipient specified is invalid",
        "created_at": "2026-10-19T13:29:07.866533"
      }
    ],
    "shortfall_kobo": 0,
    "returned_kobo": 0,
    "netted_kobo": 0,
    "caregiver_payouts_frozen_at": null,
    "caregiver_payouts_frozen_reason": null,
    "failure_reason": "Paystack refused the transfer: Recipient specified is invalid",
    "initiated_at": "2026-10-19T13:29:11.411576",
    "completed_at": "2026-10-19T13:29:11.434780",
    "approved_by": "782b30c4-afec-4093-b436-94b567e953b1",
    "approved_at": "2026-10-19T13:29:08.595981",
    "cancelled_by": null,
    "cancelled_at": null,
    "cancel_reason": null,
    "created_at": "2026-10-19T13:29:07.865568"
  }
]
```

_capture `208-payouts-list-after-send.json`_

**The send job checks the caregiver again before it sends.** Approval can be hours before the send job runs. When the job claims a payout it requires the caregiver to be still `approved` or `active`, with an open account, and not erased. Otherwise **it sends nothing**: it cancels the payout as the system (`cancelled_by` is `null`), returns the money and records why in `cancel_reason` ("Cancelled before sending: ..."). `db-simulated-suspended-after-approval` suspended one caregiver after the batch was approved (**[db-simulated]**: the database edit stands in for the suspension route); the dispatch capture shows four transfers sent for five approved payouts, and the list shows the cancelled one:

<!-- capture: 236-payouts-list-attention-after-send.json response.body path=items first=5 -->
```json
[
  {
    "id": "9a3248ae-3bd3-4603-925a-d6c9abc94be9",
    "batch_id": "451f6dcb-edc6-4e05-9c79-ffe45162c402",
    "batch_period": "2026-11-02",
    "caregiver_id": "d5814a3e-54dc-4226-8937-2f9bc921d457",
    "caregiver_name": "Placeholder Suspended Later",
    "amount_kobo": 700000,
    "status": "cancelled",
    "bank_name": "Placeholder Bank",
    "account_number": "******0025",
    "account_name": "PLACEHOLDER SUSPENDED LATER",
    "reference": "po-9a3248ae3bd34603925ad6c9abc94be9-1",
    "transfer_code": null,
    "attempts": 1,
    "attempt_history": [
      {
        "attempt": 1,
        "reference": "po-9a3248ae3bd34603925ad6c9abc94be9-1",
        "status": "pending",
        "failure_reason": null,
        "created_at": "2026-10-19T13:29:23.494254"
      }
    ],
    "shortfall_kobo": 0,
    "returned_kobo": 0,
    "netted_kobo": 0,
    "caregiver_payouts_frozen_at": null,
    "caregiver_payouts_frozen_reason": null,
    "failure_reason": null,
    "initiated_at": null,
    "completed_at": null,
    "approved_by": "6255fd01-7112-40cc-a9cf-e005bf4c3bf2",
    "approved_at": "2026-10-19T13:29:24.009748",
    "cancelled_by": null,
    "cancelled_at": "2026-10-19T13:29:26.693875",
    "cancel_reason": "Cancelled before sending: the caregiver's profile is suspended, not approved",
    "created_at": "2026-10-19T13:29:23.491793"
  },
  {
    "id": "960a0b4a-5e21-4079-9da4-936a8c61f05e",
    "batch_id": "451f6dcb-edc6-4e05-9c79-ffe45162c402",
    "batch_period": "2026-11-02",
    "caregiver_id": "a64b9bdc-eb3b-41ba-b0c1-0258743623c0",
    "caregiver_name": "Placeholder Paid",
    "amount_kobo": 1200000,
    "status": "failed",
    "bank_name": "Placeholder Bank",
    "account_number": "******0011",
    "account_name": "PLACEHOLDER PAID",
    "reference": "po-960a0b4a5e2140799da4936a8c61f05e-1",
    "transfer_code": null,
    "attempts": 1,
    "attempt_history": [
      {
        "attempt": 1,
        "reference": "po-960a0b4a5e2140799da4936a8c61f05e-1",
        "status": "refused",
        "failure_reason": "Paystack refused the transfer: Recipient specified is invalid",
        "created_at": "2026-10-19T13:29:23.484146"
      }
    ],
    "shortfall_kobo": 0,
    "returned_kobo": 0,
    "netted_kobo": 0,
    "caregiver_payouts_frozen_at": null,
    "caregiver_payouts_frozen_reason": null,
    "failure_reason": "Paystack refused the transfer: Recipient specified is invalid",
    "initiated_at": "2026-10-19T13:29:26.679216",
    "completed_at": "2026-10-19T13:29:26.683628",
    "approved_by": "6255fd01-7112-40cc-a9cf-e005bf4c3bf2",
    "approved_at": "2026-10-19T13:29:24.009748",
    "cancelled_by": null,
    "cancelled_at": null,
    "cancel_reason": null,
    "created_at": "2026-10-19T13:29:23.481890"
  },
  {
    "id": "05c42a05-b396-4907-b946-aa0ab89398cd",
    "batch_id": "451f6dcb-edc6-4e05-9c79-ffe45162c402",
    "batch_period": "2026-11-02",
    "caregiver_id": "7bc6c2b0-da51-4c84-908d-22808c19abe8",
    "caregiver_name": "Placeholder Failing",
    "amount_kobo": 3000000,
    "status": "failed",
    "bank_name": "Placeholder Bank",
    "account_number": "******0012",
    "account_name": "PLACEHOLDER FAILING",
    "reference": "po-05c42a05b3964907b946aa0ab89398cd-1",
    "transfer_code": null,
    "attempts": 1,
    "attempt_history": [
      {
        "attempt": 1,
        "reference": "po-05c42a05b3964907b946aa0ab89398cd-1",
        "status": "refused",
        "failure_reason": "Paystack refused the transfer: Recipient specified is invalid",
        "created_at": "2026-10-19T13:29:23.469293"
      }
    ],
    "shortfall_kobo": 0,
    "returned_kobo": 0,
    "netted_kobo": 0,
    "caregiver_payouts_frozen_at": null,
    "caregiver_payouts_frozen_reason": null,
    "failure_reason": "Paystack refused the transfer: Recipient specified is invalid",
    "initiated_at": "2026-10-19T13:29:26.597239",
    "completed_at": "2026-10-19T13:29:26.608012",
    "approved_by": "6255fd01-7112-40cc-a9cf-e005bf4c3bf2",
    "approved_at": "2026-10-19T13:29:24.009748",
    "cancelled_by": null,
    "cancelled_at": null,
    "cancel_reason": null,
    "created_at": "2026-10-19T13:29:23.467597"
  },
  {
    "id": "7bcbd73a-5a8b-4b21-9fdd-64b6da4706fd",
    "batch_id": "451f6dcb-edc6-4e05-9c79-ffe45162c402",
    "batch_period": "2026-11-02",
    "caregiver_id": "6c8e3f1a-4456-42e6-859c-be91671eb633",
    "caregiver_name": "Placeholder Stuck At Otp",
    "amount_kobo": 1500000,
    "status": "processing",
    "bank_name": "Placeholder Bank",
    "account_number": "******0021",
    "account_name": "PLACEHOLDER STUCK AT OTP",
    "reference": "po-7bcbd73a5a8b4b219fdd64b6da4706fd-1",
    "transfer_code": "TRF_b037aafcb64c",
    "attempts": 1,
    "attempt_history": [
      {
        "attempt": 1,
        "reference": "po-7bcbd73a5a8b4b219fdd64b6da4706fd-1",
        "status": "pending",
        "failure_reason": null,
        "created_at": "2026-10-19T13:29:23.457396"
      }
    ],
    "shortfall_kobo": 0,
    "returned_kobo": 0,
    "netted_kobo": 0,
    "caregiver_payouts_frozen_at": null,
    "caregiver_payouts_frozen_reason": null,
    "failure_reason": "Paystack is waiting for an OTP to release this transfer. Turn off OTP for transfers made through the API in the Paystack dashboard, then resolve this payout with Paystack.",
    "initiated_at": "2026-10-19T13:29:26.666488",
    "completed_at": null,
    "approved_by": "6255fd01-7112-40cc-a9cf-e005bf4c3bf2",
    "approved_at": "2026-10-19T13:29:24.009748",
    "cancelled_by": null,
    "cancelled_at": null,
    "cancel_reason": null,
    "created_at": "2026-10-19T13:29:23.455152"
  },
  {
    "id": "5c7eabab-f793-4478-bf7e-41e4e86e531b",
    "batch_id": "451f6dcb-edc6-4e05-9c79-ffe45162c402",
    "batch_period": "2026-11-02",
    "caregiver_id": "63f4b91b-db58-4589-ab8e-8d9acc679e17",
    "caregiver_name": "Placeholder Late Success",
    "amount_kobo": 1100000,
    "status": "processing",
    "bank_name": "Placeholder Bank",
    "account_number": "******0022",
    "account_name": "PLACEHOLDER LATE SUCCESS",
    "reference": "po-5c7eababf7934478bf7e41e4e86e531b-1",
    "transfer_code": "TRF_f4621c6410e6",
    "attempts": 1,
    "attempt_history": [
      {
        "attempt": 1,
        "reference": "po-5c7eababf7934478bf7e41e4e86e531b-1",
        "status": "pending",
        "failure_reason": null,
        "created_at": "2026-10-19T13:29:23.445167"
      }
    ],
    "shortfall_kobo": 0,
    "returned_kobo": 0,
    "netted_kobo": 0,
    "caregiver_payouts_frozen_at": null,
    "caregiver_payouts_frozen_reason": null,
    "failure_reason": null,
    "initiated_at": "2026-10-19T13:29:26.654222",
    "completed_at": null,
    "approved_by": "6255fd01-7112-40cc-a9cf-e005bf4c3bf2",
    "approved_at": "2026-10-19T13:29:24.009748",
    "cancelled_by": null,
    "cancelled_at": null,
    "cancel_reason": null,
    "created_at": "2026-10-19T13:29:23.443427"
  }
]
```

_capture `236-payouts-list-attention-after-send.json` (the cancelled payout is the last item; the OTP and silent ones are explained below). The send job and its transfers: `235-worker-payout-dispatch-job-2.json`_

What decides a payout (the rules the console must explain):

| Paystack says | Payout becomes | Money |
|---|---|---|
| `transfer.success` (webhook, or the reconcile job asks and gets `success`) | `success` | paid out; the caregiver's `total_withdrawn` rises |
| `transfer.failed`, or the transfer is refused when sent | `failed` | **returned** to the caregiver's available balance |
| `transfer.reversed` (Paystack paid, then took it back) | `reversed` | **returned** |
| no answer (timeout, 5xx, 408, 409) when the transfer is sent | stays `processing` | stays reserved: the money may have gone |
| `otp` (Paystack wants an OTP for API transfers) | stays `processing`, `failure_reason` says to turn OTP off (capture `236`) | stays reserved; an admin resolves it (below). **Paystack OTP for transfers must be disabled in the Paystack dashboard before going live** |
| `transfer.success` for a payout that is already `failed`, `reversed` or `cancelled` (Paystack paid after Oakly gave the money back) | **`needs_attention`**, and **the caregiver is frozen** | the returned money is **taken back**: debited from the caregiver's available balance, as much as is there; what the balance could not cover is `shortfall_kobo`. The ledger row is `successful` (captures `248`, `249`) |
| `transfer.reversed` for a payout that was already `success` (Paystack paid, then took it back) | **`needs_attention`**, caregiver frozen | **nothing is returned**: the ledger row goes back to `pending` (the money is in limbo) until an admin resolves the payout as `failed` (captures `218`, `219`) |
| `transfer.success` for an **earlier attempt** while a retry holds the reservation | **`needs_attention`**, caregiver frozen | stays reserved (one payment, one reservation); the retry is never sent. **[not live]** (asserted in `test_payout_hardening.py`) |
| the same late success while the caregiver has a **newer payout that was not sent yet** | the old payout is **`needs_attention`**; the newer payout is **`cancelled`** by the system (`cancel_reason` starts "superseded: earlier payout confirmed paid by Paystack") | its reservation pays the take-back. **[not live]** (asserted in `test_payout_hardening.py`) |
| the same, but the newer payout was **already sent** (it cannot be recalled) | **both payouts are `needs_attention`**, caregiver frozen | the old payout carries the whole amount as `shortfall_kobo` (the balance is empty); the newer one keeps its reservation. Captures `293`, `294` |
| any `transfer.failed` or `transfer.reversed` for a payout of a **frozen** caregiver | the payout becomes (or stays) **`needs_attention`**; the attempt's status is recorded | **no money returns**: it stays reserved until an admin resolves it. Capture `295`, `296` |
| any `transfer.success` for a payout of a frozen caregiver that is still `processing` or `needs_attention` with its money reserved | `success` | the reservation becomes the payment: the balance does not move |
| `transfer.success` for the **wrong amount** | **`needs_attention`**, caregiver frozen; money stays reserved, never re-sent | **[not live]** (asserted in `test_payouts.py`) |
| the reconcile job finds Paystack has **no record** 30 minutes after the send | **`needs_attention`**, caregiver frozen, `failure_reason` is "Paystack has no record; check before releasing" | **stays reserved, not returned** (captures `262`, `263`) |

The webhook is `POST /api/v1/payment/paystack/webhook` (the existing route; the signature is verified over the raw body before anything is read). It answers `200` for a transfer that is not ours too. **A webhook delivered twice changes nothing**, and a webhook for the wrong amount is not applied: the payout becomes `needs_attention` and the caregiver is frozen (**[not live]**, `test_payouts.py`). Success webhook, then the payout:

<!-- capture: 209-paystack-transfer-success-webhook.json observed -->
```json
{
  "request": {
    "method": "POST",
    "path": "/api/v1/payment/paystack/webhook",
    "auth": "x-paystack-signature: <HMAC-SHA512 of the body with the Paystack secret key>",
    "body": {
      "event": "transfer.success",
      "data": {
        "amount": 1200000,
        "currency": "NGN",
        "reason": "Oakly caregiver payout",
        "reference": "po-1ca00fde0db34909825665ebd86c2d6b-1",
        "status": "success",
        "transfer_code": "TRF_1053533f080b",
        "recipient": {
          "recipient_code": "RCP_ca95a4b24ed7"
        }
      }
    }
  },
  "response": {
    "status_code": 200,
    "headers": {},
    "body": {
      "status": "success",
      "message": "Webhook processed successfully"
    }
  }
}
```

<!-- capture: 210-payouts-list-after-success.json response.body path=items first=1 -->
```json
[
  {
    "id": "1ca00fde-0db3-4909-8256-65ebd86c2d6b",
    "batch_id": "ecd85e88-3c30-4858-8046-69b2ed56fe3a",
    "batch_period": "2026-10-26",
    "caregiver_id": "d83d80b4-55bd-4083-bd48-db16338690dc",
    "caregiver_name": "Placeholder Paid",
    "amount_kobo": 1200000,
    "status": "success",
    "bank_name": "Placeholder Bank",
    "account_number": "******0011",
    "account_name": "PLACEHOLDER PAID",
    "reference": "po-1ca00fde0db34909825665ebd86c2d6b-1",
    "transfer_code": "TRF_1053533f080b",
    "attempts": 1,
    "attempt_history": [
      {
        "attempt": 1,
        "reference": "po-1ca00fde0db34909825665ebd86c2d6b-1",
        "status": "success",
        "failure_reason": null,
        "created_at": "2026-10-19T13:29:07.883855"
      }
    ],
    "shortfall_kobo": 0,
    "returned_kobo": 0,
    "netted_kobo": 0,
    "caregiver_payouts_frozen_at": null,
    "caregiver_payouts_frozen_reason": null,
    "failure_reason": null,
    "initiated_at": "2026-10-19T13:29:11.459737",
    "completed_at": "2026-10-19T13:29:11.847354",
    "approved_by": "782b30c4-afec-4093-b436-94b567e953b1",
    "approved_at": "2026-10-19T13:29:08.595981",
    "cancelled_by": null,
    "cancelled_at": null,
    "cancel_reason": null,
    "created_at": "2026-10-19T13:29:07.882962"
  }
]
```

_capture `210-payouts-list-after-success.json`_

**Webhooks can go missing.** The reconcile job (every 15 minutes) asks Paystack about transfers that have had no webhook for 15 minutes and settles them. A transfer that Paystack has **no record of** 30 minutes after it was sent is **not** treated as failed and its money is **not** returned: the transfer may exist and succeed later, and returning the money would pay it twice. The payout becomes `needs_attention` (see below). `db-simulated-missing-webhook` moved `initiated_at` back 20 minutes to stand in for the wait; here Paystack answers `success`, so the job settles it:

<!-- capture: 226-worker-payout-reconcile-job.json response.body -->
```json
{
  "payouts_settled": 1
}
```

### Retry a failed or reversed payout

Retry **first asks Paystack about the current attempt** (`GET /transfer/verify/<reference>`) and goes ahead only on a definite `failed` or `reversed` answer (the one exception: a transfer Paystack itself refused when it was sent, `attempt_history` status `refused`, has no Paystack record, and that is accepted). A transfer that is pending, waiting for an OTP, unknown to Paystack or unreachable could still be paid, and sending again would pay twice. If Paystack says it was **paid**, the retry is refused and the payout is settled as `needs_attention` instead (the returned money is taken back and the caregiver is frozen). **A frozen caregiver's payout is never retried.** Otherwise retry reserves the money again (it must still be in the caregiver's available balance), adds a **new attempt with a new reference** (`attempts` + 1) and sends it again. It is refused if the caregiver is suspended, changed bank account since, has another live payout (the next batch may already have taken the returned money), or no longer has the balance.

Request body, then response `200` (the payout is `processing` again, `attempts` is 2, `transfer_code`, `initiated_at` and `failure_reason` are cleared; the first attempt, with its reason, is in `attempt_history`):

<!-- capture: 215-payout-retry.json request.body -->
```json
{
  "reason": "Paystack balance topped up"
}
```

<!-- capture: 215-payout-retry.json response.body -->
```json
{
  "id": "d78d3234-95ec-4a0c-9241-83d4adf920d4",
  "batch_id": "ecd85e88-3c30-4858-8046-69b2ed56fe3a",
  "batch_period": "2026-10-26",
  "caregiver_id": "1ea14f8e-28ac-4c89-bfd4-15c81e8487f5",
  "caregiver_name": "Placeholder Failing",
  "amount_kobo": 3000000,
  "status": "processing",
  "bank_name": "Placeholder Bank",
  "account_number": "******0012",
  "account_name": "PLACEHOLDER FAILING",
  "reference": "po-d78d323495ec4a0c924183d4adf920d4-2",
  "transfer_code": null,
  "attempts": 2,
  "attempt_history": [
    {
      "attempt": 1,
      "reference": "po-d78d323495ec4a0c924183d4adf920d4-1",
      "status": "refused",
      "failure_reason": "Paystack refused the transfer: Your balance is not enough to fulfil this request",
      "created_at": "2026-10-19T13:29:07.830090"
    },
    {
      "attempt": 2,
      "reference": "po-d78d323495ec4a0c924183d4adf920d4-2",
      "status": "pending",
      "failure_reason": null,
      "created_at": "2026-10-19T13:29:11.934373"
    }
  ],
  "shortfall_kobo": 0,
  "returned_kobo": 0,
  "netted_kobo": 0,
  "caregiver_payouts_frozen_at": null,
  "caregiver_payouts_frozen_reason": null,
  "failure_reason": null,
  "initiated_at": null,
  "completed_at": null,
  "approved_by": "6255fd01-7112-40cc-a9cf-e005bf4c3bf2",
  "approved_at": "2026-10-19T13:29:11.913016",
  "cancelled_by": null,
  "cancelled_at": null,
  "cancel_reason": null,
  "created_at": "2026-10-19T13:29:07.822496"
}
```

_capture `215-payout-retry.json`_

| Status | `detail` | When |
|---|---|---|
| 403 | `Access denied. Missing permission: ledger.adjust` | Finance _(capture `211`)_, Operations _(capture `212`)_ |
| 422 | the validation list | no `reason` _(capture `213`)_ |
| 409 | `Only a failed or reversed payout can be retried` | a payout that is waiting, processing, paid, cancelled or `needs_attention` _(captures `199`, `214`, `216`, `221`, `251`)_ |
| 409 | `This payout cannot be retried: <why>` | suspended, bank account changed, or `the caregiver's payouts are frozen until an admin has looked at a payout Paystack contradicted` **[not live]** (asserted in `test_payout_hardening.py`) |
| 409 | `The caregiver's available balance is lower than this payout, so it cannot be sent again` | **[not live]** |
| 409 | `This caregiver already has a payout in progress` | **[not live]** |
| 409 | `Paystack says this transfer is still pending (pending), so it cannot be retried. Wait for Paystack's answer.` | Paystack's own record still says pending _(capture `270`)_ |
| 409 | `Paystack reports this transfer as successful, so it was not sent again. The payout now needs attention: check it before doing anything else` | Paystack says it was paid; **the payout is now `needs_attention`** _(capture `272`, list `272`)_ |
| 409 | `Paystack is waiting for an OTP to release this transfer, so it cannot be retried. Resolve the payout instead.` | **[not live]** (asserted in `test_payout_hardening.py`) |
| 409 | `Paystack has no record of this transfer, so it cannot be retried. Resolve the payout instead.` | **[not live]** (same) |
| 409 | `We cannot reach Paystack right now to check this transfer, so it cannot be retried. Try again in a moment.` | **[not live]** (same) |
| 409 | `This payout changed while Paystack was being asked. Look at it again` | **[not live]** (same) |

**A payout Paystack paid and then reversed is NOT `reversed`.** The two answers disagree, so Oakly freezes the caregiver, sets the payout to `needs_attention`, and **does not return the money** (the ledger row goes back to `pending` and the 30,000.00 NGN stays out of the caregiver's available balance). Only an admin can release it: resolve the payout as `failed` (below), which returns it. The `reversed` status itself is reached only by a `transfer.reversed` for a transfer that was never reported as paid (**[not live]**, `test_payouts.py`). The reversal webhook, then the same event delivered again (which changes nothing):

<!-- capture: 218-paystack-transfer-reversed-webhook.json observed -->
```json
{
  "request": {
    "method": "POST",
    "path": "/api/v1/payment/paystack/webhook",
    "auth": "x-paystack-signature: <HMAC-SHA512 of the body with the Paystack secret key>",
    "body": {
      "event": "transfer.reversed",
      "data": {
        "amount": 3000000,
        "currency": "NGN",
        "reason": "Oakly caregiver payout",
        "reference": "po-d78d323495ec4a0c924183d4adf920d4-2",
        "status": "reversed",
        "transfer_code": "TRF_53494aa0c319",
        "recipient": {
          "recipient_code": "RCP_4d9510362c24"
        }
      }
    }
  },
  "response": {
    "status_code": 200,
    "headers": {},
    "body": {
      "status": "success",
      "message": "Webhook processed successfully"
    }
  }
}
```

<!-- capture: 219-payouts-list-after-reversal.json response.body path=items first=1 -->
```json
[
  {
    "id": "d78d3234-95ec-4a0c-9241-83d4adf920d4",
    "batch_id": "ecd85e88-3c30-4858-8046-69b2ed56fe3a",
    "batch_period": "2026-10-26",
    "caregiver_id": "1ea14f8e-28ac-4c89-bfd4-15c81e8487f5",
    "caregiver_name": "Placeholder Failing",
    "amount_kobo": 3000000,
    "status": "needs_attention",
    "bank_name": "Placeholder Bank",
    "account_number": "******0012",
    "account_name": "PLACEHOLDER FAILING",
    "reference": "po-d78d323495ec4a0c924183d4adf920d4-2",
    "transfer_code": "TRF_53494aa0c319",
    "attempts": 2,
    "attempt_history": [
      {
        "attempt": 1,
        "reference": "po-d78d323495ec4a0c924183d4adf920d4-1",
        "status": "refused",
        "failure_reason": "Paystack refused the transfer: Your balance is not enough to fulfil this request",
        "created_at": "2026-10-19T13:29:07.830090"
      },
      {
        "attempt": 2,
        "reference": "po-d78d323495ec4a0c924183d4adf920d4-2",
        "status": "reversed",
        "failure_reason": "Paystack reported the transfer as reversed",
        "created_at": "2026-10-19T13:29:11.934373"
      }
    ],
    "shortfall_kobo": 0,
    "returned_kobo": 0,
    "netted_kobo": 0,
    "caregiver_payouts_frozen_at": "2026-10-19T13:29:14.370730",
    "caregiver_payouts_frozen_reason": "Payout d78d3234-95ec-4a0c-9241-83d4adf920d4: Paystack reported attempt 2 as reversed after it had been paid. Check Paystack, then resolve this payout as failed (the money is returned) or paid.",
    "failure_reason": "Paystack reported attempt 2 as reversed after it had been paid. Check Paystack, then resolve this payout as failed (the money is returned) or paid.",
    "initiated_at": "2026-10-19T13:29:14.078581",
    "completed_at": "2026-10-19T13:29:14.358872",
    "approved_by": "6255fd01-7112-40cc-a9cf-e005bf4c3bf2",
    "approved_at": "2026-10-19T13:29:11.913016",
    "cancelled_by": null,
    "cancelled_at": null,
    "cancel_reason": null,
    "created_at": "2026-10-19T13:29:07.822496"
  }
]
```

The payout after the reversal (capture `219`, above): `needs_attention`, with the freeze fields set on it. A retry is refused (capture `221`), and the admin resolves the payout as `failed` once Paystack confirms the reversal (capture `222`; `returned_kobo` is the whole 30,000.00 NGN and `netted_kobo` is `0`, because the caregiver has no shortfall):

<!-- capture: 221-payout-retry-needs-attention-after-reversal.json response.body -->
```json
{
  "detail": "Only a failed or reversed payout can be retried"
}
```

<!-- capture: 222-payout-resolve-reversed-as-failed.json request.body -->
```json
{
  "outcome": "failed",
  "reason": "Paystack shows the transfer as reversed and the money back in our balance"
}
```

<!-- capture: 222-payout-resolve-reversed-as-failed.json response.body keys=status,amount_kobo,shortfall_kobo,returned_kobo,netted_kobo,caregiver_payouts_frozen_at,caregiver_payouts_frozen_reason,failure_reason -->
```json
{
  "status": "failed",
  "amount_kobo": 3000000,
  "shortfall_kobo": 0,
  "returned_kobo": 3000000,
  "netted_kobo": 0,
  "caregiver_payouts_frozen_at": "2026-10-19T13:29:14.370730",
  "caregiver_payouts_frozen_reason": "Payout d78d3234-95ec-4a0c-9241-83d4adf920d4: Paystack reported attempt 2 as reversed after it had been paid. Check Paystack, then resolve this payout as failed (the money is returned) or paid.",
  "failure_reason": "Resolved as failed by an admin: Paystack shows the transfer as reversed and the money back in our balance"
}
```

With nothing left open the freeze is lifted (capture `223`, section "Unfreezing a caregiver" below), and the payout can be retried (third attempt, `payout-retry-reversed`, capture `224`; the first attempt had been **refused** by Paystack and the second reversed, and the retry's check of Paystack accepted both) and is settled by the reconcile job. The `attempt_history` in capture `219` above shows how its first attempt (refused by Paystack) and second (reversed) are kept, each with its reason. The final state of the batch:

<!-- capture: 228-payout-batches-list-final.json response.body path=items first=1 -->
```json
[
  {
    "id": "ecd85e88-3c30-4858-8046-69b2ed56fe3a",
    "period_key": "2026-10-26",
    "cadence": "weekly",
    "min_payout_kobo": 500000,
    "status": "approved",
    "approved_by": "782b30c4-afec-4093-b436-94b567e953b1",
    "approved_at": "2026-10-19T13:29:08.595981",
    "approval_reason": "Placeholder: totals checked against the ledger",
    "created_at": "2026-10-19T13:29:07.792983",
    "payout_count": 6,
    "total_kobo": 9200000,
    "counts": {
      "cancelled": 1,
      "failed": 3,
      "success": 2
    }
  }
]
```

### Payouts that need a person: `needs_attention` and `POST /admin/payouts/{id}/resolve`

**The rule: once a caregiver's payouts are disputed, automatic code may only DECREASE what they can be paid, never increase it. Money returns only through an explicit, audited admin action.** A *contradiction* (below) **freezes the caregiver** and sets the payouts involved to `needs_attention`. From then on:
- the batch leaves the caregiver out, and the send job sends nothing for them;
- every webhook or reconcile result on ANY of their payouts records the attempt's outcome and the payout's status but **credits nothing**: a `failed` or `reversed` payout becomes `needs_attention` and keeps its reservation, a `success` just settles (the balance does not move);
- the only money moves automatic code makes are: cancel a newer payout that was not sent and let its reservation pay a take-back, debit what the balance holds up to the amount owed, and record the rest as `shortfall_kobo`. It never credits the balance;
- the freeze is on the caregiver, so `retry` is refused for any of their payouts, and every payout object shows `caregiver_payouts_frozen_at` and `caregiver_payouts_frozen_reason` (the first reason stays).

What counts as a contradiction, what happens to the money, and what the admin does:

| How it got here | Money | What the admin does |
|---|---|---|
| Paystack **paid after Oakly returned the money** (`failed`, `reversed`, cancelled, then `transfer.success`) | taken back from the caregiver's balance (`shortfall_kobo` is what the balance could not cover); ledger row `successful` | check Paystack, resolve as `paid`; unfreeze (the shortfall is debited from the balance first; what is left needs `write_off_shortfall: true`) |
| Paystack paid an **earlier attempt** while a retry holds the reservation, or paid the same payout twice | still reserved (one payment, one reservation), the retry is never sent; a second payment is taken back like the row above | check both attempts at Paystack; resolve as `paid` |
| The reconcile job found **no record** at Paystack | still reserved (not returned); ledger row `pending` | check the Paystack dashboard or support; resolve as `failed` (needs `force`) or `paid` |
| `transfer.success` for the **wrong amount**, on a payout in **any** status (`processing`, `failed`, `reversed`, `cancelled`, `success`) | no money moves, never re-sent; the payout becomes `needs_attention` whatever it was, so the unfreeze is blocked until a person has looked | check Paystack; resolve as `paid` or `failed` (see "A payout flagged after its money was returned" below) |
| `transfer.reversed` (or `failed`) **after a success** | not returned; ledger row back to `pending` | check Paystack; resolve as `failed` (the money is returned) or `paid` |
| `transfer.failed` or `transfer.reversed` **after an admin forced the payout to `paid`** (`force: true`, Paystack's answer was not final) | not returned; ledger row back to `pending`; the caregiver is frozen | check Paystack; resolve as `failed` (the money is returned) or `paid` |

`POST /admin/payouts/{id}/resolve` also settles a `processing` payout that Paystack's webhook will not settle, typically one stuck at OTP.

Body: `{"outcome": "paid" | "failed", "reason": "...", "force": false}`. `reason` is required (it is stored in the audit log). **Paystack is asked first** (`GET /transfer/verify/<reference>`, about the attempt that was paid if one was, else the current one):

| Paystack answers | `outcome: "paid"` | `outcome: "failed"` |
|---|---|---|
| `success` | done | **refused** (`409`), even with `force` |
| `failed`, `rejected`, `blocked`, `abandoned`, `reversed` | **refused**, even with `force` | done: the reserved money returns |
| anything not final: `pending`, `otp`, no record, unreachable | refused unless `force: true` | refused unless `force: true` |

`force` never overrides a definite answer; it only lets the admin act when Paystack gives none. **`failed` is also refused when an attempt was already paid** (the money must never be returned twice), and when the payout's money is no longer reserved. `paid` marks the payout `success` and its ledger row `successful`. `failed` marks it `failed`, returns the reserved money **net of the caregiver's outstanding shortfall** and records the reason on the attempt. When the caregiver is frozen the amount returned is `max(0, reserved - outstanding shortfall)`, and the shortfall goes down by what is kept (oldest payout first); `returned_kobo` and `netted_kobo` on the payout say how it was split, and the same two numbers are written to the payout's ledger row and the audit entry. A payout that is merely stuck (a `processing` one at OTP, for a caregiver who is not frozen) is returned in full.

**Paystack is asked with no lock held, so the rules are checked again when the money moves.** If a late webhook changes the payout while the request is in flight, the resolve is refused with `409` and **nothing changes** (the admin looks at the payout again). Texts (**[not live]**: they need a webhook to commit between Paystack's answer and the database lock; asserted in `test_payout_hardening.py` by committing that webhook from a second session inside the Paystack call):

| Status | `detail` | When |
|---|---|---|
| 409 | `Paystack paid attempt <n> of this payout while Paystack was being asked, so the money cannot be returned. Look at it again and resolve it as paid` | resolving as `failed`, and an attempt turned out to be paid meanwhile (returning the reservation would pay twice) |
| 409 | `Paystack also paid attempt <n> of this payout while Paystack was being asked, so this would pay it twice. Look at it again` | resolving as `paid`, and a different attempt was paid meanwhile |
| 409 | `This payout's attempt changed while Paystack was being asked. Look at it again` | resolving as `paid`, and the attempt that was checked changed state meanwhile |

**A payout flagged after its money was returned** (a wrong-amount success on a `failed`, `reversed` or `cancelled` payout; **[not live]**, covered by tests): the payout is `needs_attention` but its money is already back in the balance, so nothing is reserved. Resolve as `failed` (Paystack says it failed): the payout is closed as `failed` (or `cancelled`) and **no money moves** (`returned_kobo` and `netted_kobo` stay `0`). Resolve as `paid` (Paystack says it was paid): the money is **taken back from the balance** as far as it goes, the rest is recorded as `shortfall_kobo`.

**UX consequence: a payout you forced to `paid` can come back.** If you resolve with `force: true` while Paystack's answer is not final and Paystack later reports the transfer as `failed` or `reversed`, the payout becomes `needs_attention` again and the caregiver is frozen; nothing is credited. Show it as an alert on that payout, not as a silent status change.

**Stuck at OTP.** Paystack accepted the transfer but is waiting for an OTP (the fake answers `otp`; `failure_reason` carries the message, capture `236`). While Paystack still says `otp`, neither outcome is allowed without `force`:

<!-- capture: 241-payout-resolve-answer-not-final.json request.body -->
```json
{
  "outcome": "failed",
  "reason": "It has been stuck for an hour"
}
```

Response `409`:

<!-- capture: 241-payout-resolve-answer-not-final.json response.body -->
```json
{
  "detail": "Paystack is waiting for an OTP to release this transfer, so there is no proof for either answer. Resolve it only with force and a reason that says how you checked"
}
```

Someone then enters the OTP in the Paystack dashboard and the transfer succeeds (`fake-paystack-otp-entered`, **[simulated]**: no webhook is sent). Now resolving as `failed` is refused because Paystack contradicts it, and `paid` goes through:

<!-- capture: 243-payout-resolve-contradicts-paystack.json response.body -->
```json
{
  "detail": "Paystack reports this transfer as successful, so it cannot be resolved as failed"
}
```

Resolved as paid, response `200` (`status` is `success`):

<!-- capture: 244-payout-resolve-paid.json request.body -->
```json
{
  "outcome": "paid",
  "reason": "OTP entered in the Paystack dashboard; Paystack shows the transfer as successful"
}
```

<!-- capture: 244-payout-resolve-paid.json response.body -->
```json
{
  "id": "7bcbd73a-5a8b-4b21-9fdd-64b6da4706fd",
  "batch_id": "451f6dcb-edc6-4e05-9c79-ffe45162c402",
  "batch_period": "2026-11-02",
  "caregiver_id": "6c8e3f1a-4456-42e6-859c-be91671eb633",
  "caregiver_name": "Placeholder Stuck At Otp",
  "amount_kobo": 1500000,
  "status": "success",
  "bank_name": "Placeholder Bank",
  "account_number": "******0021",
  "account_name": "PLACEHOLDER STUCK AT OTP",
  "reference": "po-7bcbd73a5a8b4b219fdd64b6da4706fd-1",
  "transfer_code": "TRF_b037aafcb64c",
  "attempts": 1,
  "attempt_history": [
    {
      "attempt": 1,
      "reference": "po-7bcbd73a5a8b4b219fdd64b6da4706fd-1",
      "status": "success",
      "failure_reason": null,
      "created_at": "2026-10-19T13:29:23.457396"
    }
  ],
  "shortfall_kobo": 0,
  "returned_kobo": 0,
  "netted_kobo": 0,
  "caregiver_payouts_frozen_at": null,
  "caregiver_payouts_frozen_reason": null,
  "failure_reason": null,
  "initiated_at": "2026-10-19T13:29:26.666488",
  "completed_at": "2026-10-19T13:29:27.206239",
  "approved_by": "6255fd01-7112-40cc-a9cf-e005bf4c3bf2",
  "approved_at": "2026-10-19T13:29:24.009748",
  "cancelled_by": null,
  "cancelled_at": null,
  "cancel_reason": null,
  "created_at": "2026-10-19T13:29:23.455152"
}
```

_captures `240` to `244`_

**Paid after the money was returned.** A `transfer.failed` webhook returns the money (the payout is plainly `failed`: nothing is contradicted yet); the caregiver's balance then falls to 3,000.00 NGN (**[db-simulated]**: `db-simulated-balance-changed`, standing in for a ledger adjustment); then the **same transfer is reported successful** (captures `246`, `248`, **[fake Paystack]** webhooks). Oakly takes back what is there (300000 kobo) and records the rest as a shortfall (800000 kobo). The payout is `needs_attention`, its ledger row `successful`, and the caregiver is frozen (`caregiver_payouts_frozen_at` and `caregiver_payouts_frozen_reason` on the payout):

<!-- capture: 249-payouts-list-needs-attention.json response.body path=items first=1 -->
```json
[
  {
    "id": "5c7eabab-f793-4478-bf7e-41e4e86e531b",
    "batch_id": "451f6dcb-edc6-4e05-9c79-ffe45162c402",
    "batch_period": "2026-11-02",
    "caregiver_id": "63f4b91b-db58-4589-ab8e-8d9acc679e17",
    "caregiver_name": "Placeholder Late Success",
    "amount_kobo": 1100000,
    "status": "needs_attention",
    "bank_name": "Placeholder Bank",
    "account_number": "******0022",
    "account_name": "PLACEHOLDER LATE SUCCESS",
    "reference": "po-5c7eababf7934478bf7e41e4e86e531b-1",
    "transfer_code": "TRF_f4621c6410e6",
    "attempts": 1,
    "attempt_history": [
      {
        "attempt": 1,
        "reference": "po-5c7eababf7934478bf7e41e4e86e531b-1",
        "status": "success",
        "failure_reason": "Paystack reported the transfer as failed",
        "created_at": "2026-10-19T13:29:23.445167"
      }
    ],
    "shortfall_kobo": 800000,
    "returned_kobo": 0,
    "netted_kobo": 0,
    "caregiver_payouts_frozen_at": "2026-10-19T13:29:27.255657",
    "caregiver_payouts_frozen_reason": "Payout 5c7eabab-f793-4478-bf7e-41e4e86e531b: Paystack paid attempt 1 after the payout was marked failed; 300000 kobo was taken back from the available balance, 800000 kobo could not be recovered (shortfall).",
    "failure_reason": "Paystack reported the transfer as failed Paystack paid attempt 1 after the payout was marked failed; 300000 kobo was taken back from the available balance, 800000 kobo could not be recovered (shortfall).",
    "initiated_at": "2026-10-19T13:29:26.654222",
    "completed_at": "2026-10-19T13:29:27.243300",
    "approved_by": "6255fd01-7112-40cc-a9cf-e005bf4c3bf2",
    "approved_at": "2026-10-19T13:29:24.009748",
    "cancelled_by": null,
    "cancelled_at": null,
    "cancel_reason": null,
    "created_at": "2026-10-19T13:29:23.443427"
  }
]
```

_capture `249-payouts-list-needs-attention.json`. Note `attempt_history` (the one attempt is `success`), `shortfall_kobo`, and the `failure_reason` the console should show verbatim._

`db-observed-wallet-after-late-success` (capture `250`) read the caregiver's balance straight from the database: `0`, so the next batch cannot pay the same money again. From here the caregiver is frozen: retry is refused (`250`, `Only a failed or reversed payout can be retried`), resolving as `failed` is refused even with `force` (`251`) and resolving as `paid` confirms it (`257`). **`shortfall_kobo` stays on the payout as the record of money Oakly paid out and could not recover**; recovering it is a separate ledger adjustment, nothing automatic. It is settled only by the admin, when unfreezing (below): debited from the caregiver's balance, and what the balance cannot cover is written off on purpose.

<!-- capture: 252-payout-resolve-paid-twice-failed.json response.body -->
```json
{
  "detail": "Paystack has already paid attempt 1 of this payout, so the money cannot be returned. Resolve it as paid"
}
```

**No record at Paystack.** A transfer call that got a `502` may or may not have reached Paystack, so the payout stays `processing`. `db-simulated-silent-transfer` (**[db-simulated]**) moved `initiated_at` back 31 minutes; the reconcile job then asks Paystack, gets "not found", and **does not return the money**:

<!-- capture: 262-worker-payout-reconcile-job-2.json response.body -->
```json
{
  "payouts_acted_on": 1
}
```

<!-- capture: 263-payout-no-record-needs-attention.json response.body path=items first=1 -->
```json
[
  {
    "id": "177df64f-4f83-4714-a19f-99676ac40610",
    "batch_id": "451f6dcb-edc6-4e05-9c79-ffe45162c402",
    "batch_period": "2026-11-02",
    "caregiver_id": "3bb04460-4c40-44ce-bc69-b1ff2e130d7b",
    "caregiver_name": "Placeholder Silent Transfer",
    "amount_kobo": 900000,
    "status": "needs_attention",
    "bank_name": "Placeholder Bank",
    "account_number": "******0023",
    "account_name": "PLACEHOLDER SILENT TRANSFER",
    "reference": "po-177df64f4f834714a19f99676ac40610-1",
    "transfer_code": null,
    "attempts": 1,
    "attempt_history": [
      {
        "attempt": 1,
        "reference": "po-177df64f4f834714a19f99676ac40610-1",
        "status": "pending",
        "failure_reason": null,
        "created_at": "2026-10-19T13:29:23.408070"
      }
    ],
    "shortfall_kobo": 0,
    "returned_kobo": 0,
    "netted_kobo": 0,
    "caregiver_payouts_frozen_at": "2026-10-19T13:29:29.645050",
    "caregiver_payouts_frozen_reason": "Payout 177df64f-4f83-4714-a19f-99676ac40610: Paystack has no record; check before releasing",
    "failure_reason": "Paystack has no record; check before releasing",
    "initiated_at": "2026-10-19T12:58:27.417889",
    "completed_at": null,
    "approved_by": "6255fd01-7112-40cc-a9cf-e005bf4c3bf2",
    "approved_at": "2026-10-19T13:29:24.009748",
    "cancelled_by": null,
    "cancelled_at": null,
    "cancel_reason": null,
    "created_at": "2026-10-19T13:29:23.403257"
  }
]
```

Resolving as `failed` without `force` is refused (nothing proves the transfer will never exist); with `force` and a reason that says how it was checked, the reserved money returns (capture `266`, observed in the database: the 9,000.00 NGN is back):

<!-- capture: 264-payout-resolve-no-record-without-force.json response.body -->
```json
{
  "detail": "Paystack has no record of this transfer, so there is no proof for either answer. Resolve it only with force and a reason that says how you checked"
}
```

<!-- capture: 265-payout-resolve-no-record-forced.json request.body -->
```json
{
  "outcome": "failed",
  "reason": "Checked the Paystack dashboard and support: no transfer exists for this reference",
  "force": true
}
```

If the transfer turns up and is paid after a forced `failed`, the late success is taken back as described above and the payout needs attention again. The "no record" flag froze the caregiver; once the payout is resolved the freeze is lifted with the unfreeze route (capture `267`). If it turns up while the payout is still `needs_attention` for "no record", the success settles it cleanly as `success` (**[not live]**, `test_payout_hardening.py`).

Resolve errors:

| Status | `detail` | When |
|---|---|---|
| 403 | `Access denied. Missing permission: ledger.adjust` | Finance _(capture `237`)_ |
| 422 | the validation list | no `reason`, or an `outcome` that is not `paid` or `failed` _(captures `238`, `239`)_ |
| 404 | `Payout not found` | unknown id _(capture `240`)_ |
| 409 | `Paystack is waiting for an OTP to release this transfer, so there is no proof for either answer. Resolve it only with force and a reason that says how you checked` | not final _(capture `241`)_; other unknowns read `Paystack has no record of this transfer, ...` _(capture `264`)_, `Paystack says this transfer is still pending (...)`, `We cannot reach Paystack right now ...` (**[not live]**) |
| 409 | `Paystack reports this transfer as successful, so it cannot be resolved as failed` | contradicts, even with `force` _(capture `243`)_; the mirror image reads `... as failed, so it cannot be resolved as paid` (**[not live]**) |
| 409 | `Paystack has already paid attempt 1 of this payout, so the money cannot be returned. Resolve it as paid` | `failed` for a payout with a paid attempt _(capture `252`)_ |
| 409 | `A payout that is success cannot be resolved here; only a processing payout or one that needs attention can be` | any other status; `success` here is the payout's status _(capture `245`)_ |
| 409 | `The money of this payout is no longer reserved, so it cannot be returned by resolving it. Fix the balance with a ledger adjustment` | **[not live]** |
| 409 | `This payout changed while Paystack was being asked. Look at it again` | a webhook settled it in between **[not live]** |

### Netting: the shortfall is kept out of the money that would come back

The case the freeze exists for. A caregiver's first payout fails (the money returns, capture `290`), the next batch reserves it again as a **second** payout and it is sent, and then Paystack reports the **first** transfer as successful after all (capture `294`). The second payout cannot be recalled, and the caregiver's balance is empty, so **both payouts become `needs_attention`, the caregiver is frozen, and the first payout carries the whole 10,000.00 NGN as `shortfall_kobo`** (capture `295`; **[db-simulated]** only to keep every other caregiver out of these two batches: capture `286`):

<!-- capture: 295-payouts-list-net-off-frozen.json response.body path=items first=2 keys=status,amount_kobo,shortfall_kobo,returned_kobo,netted_kobo,caregiver_payouts_frozen_at,caregiver_payouts_frozen_reason,failure_reason -->
```json
[
  {
    "id": "15fe42c7-20ed-420a-9269-01307516ea9b",
    "batch_id": "1e48b2a6-a33c-415c-80f6-6545a5adf8ce",
    "batch_period": "2026-11-23",
    "caregiver_id": "e1806387-f470-4c99-a7ec-45f5a10806ad",
    "caregiver_name": "Placeholder Net Off",
    "amount_kobo": 1000000,
    "status": "needs_attention",
    "bank_name": "Placeholder Bank",
    "account_number": "******0031",
    "account_name": "PLACEHOLDER NET OFF",
    "reference": "po-15fe42c720ed420a926901307516ea9b-1",
    "transfer_code": "TRF_60994c665c37",
    "attempts": 1,
    "attempt_history": [
      {
        "attempt": 1,
        "reference": "po-15fe42c720ed420a926901307516ea9b-1",
        "status": "pending",
        "failure_reason": null,
        "created_at": "2026-10-19T13:29:48.571212"
      }
    ],
    "shortfall_kobo": 0,
    "returned_kobo": 0,
    "netted_kobo": 0,
    "caregiver_payouts_frozen_at": "2026-10-19T13:29:54.602355",
    "caregiver_payouts_frozen_reason": "Payout ab693793-b0aa-471a-904e-95a1f1fae4d9: Paystack paid attempt 1 after the payout was marked failed; 0 kobo was taken back from the available balance, 1000000 kobo could not be recovered (shortfall).",
    "failure_reason": "Payout ab693793-b0aa-471a-904e-95a1f1fae4d9 was confirmed paid by Paystack after this one was sent. Check Paystack for both before resolving this payout.",
    "initiated_at": "2026-10-19T13:29:50.366382",
    "completed_at": null,
    "approved_by": "6255fd01-7112-40cc-a9cf-e005bf4c3bf2",
    "approved_at": "2026-10-19T13:29:49.236130",
    "cancelled_by": null,
    "cancelled_at": null,
    "cancel_reason": null,
    "created_at": "2026-10-19T13:29:48.565705"
  },
  {
    "id": "ab693793-b0aa-471a-904e-95a1f1fae4d9",
    "batch_id": "3dbf08d4-a265-4129-aee8-4f0e12fee34d",
    "batch_period": "2026-11-16",
    "caregiver_id": "e1806387-f470-4c99-a7ec-45f5a10806ad",
    "caregiver_name": "Placeholder Net Off",
    "amount_kobo": 1000000,
    "status": "needs_attention",
    "bank_name": "Placeholder Bank",
    "account_number": "******0031",
    "account_name": "PLACEHOLDER NET OFF",
    "reference": "po-ab693793b0aa471a904e95a1f1fae4d9-1",
    "transfer_code": "TRF_e509ef13ddd3",
    "attempts": 1,
    "attempt_history": [
      {
        "attempt": 1,
        "reference": "po-ab693793b0aa471a904e95a1f1fae4d9-1",
        "status": "success",
        "failure_reason": "Paystack reported the transfer as failed",
        "created_at": "2026-10-19T13:29:36.151053"
      }
    ],
    "shortfall_kobo": 1000000,
    "returned_kobo": 0,
    "netted_kobo": 0,
    "caregiver_payouts_frozen_at": "2026-10-19T13:29:54.602355",
    "caregiver_payouts_frozen_reason": "Payout ab693793-b0aa-471a-904e-95a1f1fae4d9: Paystack paid attempt 1 after the payout was marked failed; 0 kobo was taken back from the available balance, 1000000 kobo could not be recovered (shortfall).",
    "failure_reason": "Paystack reported the transfer as failed Paystack paid attempt 1 after the payout was marked failed; 0 kobo was taken back from the available balance, 1000000 kobo could not be recovered (shortfall).",
    "initiated_at": "2026-10-19T13:29:41.914735",
    "completed_at": "2026-10-19T13:29:42.969146",
    "approved_by": "6255fd01-7112-40cc-a9cf-e005bf4c3bf2",
    "approved_at": "2026-10-19T13:29:36.614656",
    "cancelled_by": null,
    "cancelled_at": null,
    "cancel_reason": null,
    "created_at": "2026-10-19T13:29:36.146894"
  }
]
```

Now Paystack reports the **second** transfer as failed (capture `296`). Because the caregiver is frozen, nothing is returned: the second payout stays `needs_attention` with its 10,000.00 NGN reserved (capture `297`). The admin resolves it as `failed` (capture `298`): the 10,000.00 NGN reservation is **kept against the 10,000.00 NGN shortfall**, so `returned_kobo` is `0`, `netted_kobo` is `1000000`, and the first payout's `shortfall_kobo` drops to `0` (capture `299`). Had the second payout been larger than the shortfall, only the difference would have come back.

<!-- capture: 298-payout-resolve-net-off.json request.body -->
```json
{
  "outcome": "failed",
  "reason": "Paystack shows the second transfer as failed"
}
```

<!-- capture: 298-payout-resolve-net-off.json response.body keys=status,amount_kobo,shortfall_kobo,returned_kobo,netted_kobo,caregiver_payouts_frozen_at,failure_reason -->
```json
{
  "status": "failed",
  "amount_kobo": 1000000,
  "shortfall_kobo": 0,
  "returned_kobo": 0,
  "netted_kobo": 1000000,
  "caregiver_payouts_frozen_at": "2026-10-19T13:29:54.602355",
  "failure_reason": "Resolved as failed by an admin: Paystack shows the second transfer as failed"
}
```

<!-- capture: 299-payouts-list-net-off-resolved.json response.body path=items first=2 keys=status,amount_kobo,shortfall_kobo,returned_kobo,netted_kobo,caregiver_payouts_frozen_at -->
```json
[
  {
    "id": "15fe42c7-20ed-420a-9269-01307516ea9b",
    "batch_id": "1e48b2a6-a33c-415c-80f6-6545a5adf8ce",
    "batch_period": "2026-11-23",
    "caregiver_id": "e1806387-f470-4c99-a7ec-45f5a10806ad",
    "caregiver_name": "Placeholder Net Off",
    "amount_kobo": 1000000,
    "status": "failed",
    "bank_name": "Placeholder Bank",
    "account_number": "******0031",
    "account_name": "PLACEHOLDER NET OFF",
    "reference": "po-15fe42c720ed420a926901307516ea9b-1",
    "transfer_code": "TRF_60994c665c37",
    "attempts": 1,
    "attempt_history": [
      {
        "attempt": 1,
        "reference": "po-15fe42c720ed420a926901307516ea9b-1",
        "status": "failed",
        "failure_reason": "Resolved as failed by an admin: Paystack shows the second transfer as failed",
        "created_at": "2026-10-19T13:29:48.571212"
      }
    ],
    "shortfall_kobo": 0,
    "returned_kobo": 0,
    "netted_kobo": 1000000,
    "caregiver_payouts_frozen_at": "2026-10-19T13:29:54.602355",
    "caregiver_payouts_frozen_reason": "Payout ab693793-b0aa-471a-904e-95a1f1fae4d9: Paystack paid attempt 1 after the payout was marked failed; 0 kobo was taken back from the available balance, 1000000 kobo could not be recovered (shortfall).",
    "failure_reason": "Resolved as failed by an admin: Paystack shows the second transfer as failed",
    "initiated_at": "2026-10-19T13:29:50.366382",
    "completed_at": "2026-10-19T13:29:54.730616",
    "approved_by": "6255fd01-7112-40cc-a9cf-e005bf4c3bf2",
    "approved_at": "2026-10-19T13:29:49.236130",
    "cancelled_by": null,
    "cancelled_at": null,
    "cancel_reason": null,
    "created_at": "2026-10-19T13:29:48.565705"
  },
  {
    "id": "ab693793-b0aa-471a-904e-95a1f1fae4d9",
    "batch_id": "3dbf08d4-a265-4129-aee8-4f0e12fee34d",
    "batch_period": "2026-11-16",
    "caregiver_id": "e1806387-f470-4c99-a7ec-45f5a10806ad",
    "caregiver_name": "Placeholder Net Off",
    "amount_kobo": 1000000,
    "status": "needs_attention",
    "bank_name": "Placeholder Bank",
    "account_number": "******0031",
    "account_name": "PLACEHOLDER NET OFF",
    "reference": "po-ab693793b0aa471a904e95a1f1fae4d9-1",
    "transfer_code": "TRF_e509ef13ddd3",
    "attempts": 1,
    "attempt_history": [
      {
        "attempt": 1,
        "reference": "po-ab693793b0aa471a904e95a1f1fae4d9-1",
        "status": "success",
        "failure_reason": "Paystack reported the transfer as failed",
        "created_at": "2026-10-19T13:29:36.151053"
      }
    ],
    "shortfall_kobo": 0,
    "returned_kobo": 0,
    "netted_kobo": 0,
    "caregiver_payouts_frozen_at": "2026-10-19T13:29:54.602355",
    "caregiver_payouts_frozen_reason": "Payout ab693793-b0aa-471a-904e-95a1f1fae4d9: Paystack paid attempt 1 after the payout was marked failed; 0 kobo was taken back from the available balance, 1000000 kobo could not be recovered (shortfall).",
    "failure_reason": "Paystack reported the transfer as failed Paystack paid attempt 1 after the payout was marked failed; 0 kobo was taken back from the available balance, 1000000 kobo could not be recovered (shortfall).",
    "initiated_at": "2026-10-19T13:29:41.914735",
    "completed_at": "2026-10-19T13:29:42.969146",
    "approved_by": "6255fd01-7112-40cc-a9cf-e005bf4c3bf2",
    "approved_at": "2026-10-19T13:29:36.614656",
    "cancelled_by": null,
    "cancelled_at": null,
    "cancel_reason": null,
    "created_at": "2026-10-19T13:29:36.146894"
  }
]
```

The first payout is then resolved as `paid` (capture `300`) and the caregiver unfrozen (capture `301`).

**Not live:** the case where the second payout is larger than the shortfall (`returned_kobo` above `0`) is asserted in `test_payout_hardening.py::test_resolve_returns_only_the_part_of_the_reservation_above_the_shortfall`.

### Unfreezing a caregiver: `POST /admin/payouts/caregivers/{caregiver_id}/unfreeze`

Permission `ledger.adjust` (Super Admin only). Body `{"reason": "...", "write_off_shortfall": false}`; `reason` is required and audited. It is **refused while any of the caregiver's payouts is `needs_attention` or `processing`** (resolve them first), and while the caregiver is not frozen.

**Earnings keep accruing while a caregiver is frozen** (on purpose: they are theirs), so an unfreeze **first debits the outstanding shortfall from the available balance**, as much as the balance holds, and the shortfall goes down by that amount. The caregiver's balance only ever goes **down** here. Then:
- nothing is left owed: the unfreeze goes ahead without `write_off_shortfall`;
- something is still owed: the unfreeze is **refused with `409` and nothing changes** (the debit is not applied either), unless `write_off_shortfall` is `true`, which writes the rest off (the audit entry records the amount).

The response is `{caregiver_id, payouts_frozen_at: null, debited_from_balance_kobo, written_off_shortfall_kobo, outstanding_shortfall_kobo}`; `outstanding_shortfall_kobo` is always `0` on success. **UX: tell the admin before they confirm** that earnings that built up while the caregiver was frozen will be debited. The debit is written to the shortfall payout's ledger row (`status_reason`) and to the audit entry (`debited_from_balance_kobo` and `per_payout`).

Refused while a payout still needs attention (capture `257`; the message lists the payout ids), then, after resolving it as paid (capture `258`), refused again because the balance is empty and an 8,000.00 NGN shortfall is left (capture `259`, the message states the amount and the flag to use):

<!-- capture: 257-payout-unfreeze-refused-while-open.json response.body -->
```json
{
  "detail": "1 payout(s) of this caregiver still need attention or are processing (5c7eabab-f793-4478-bf7e-41e4e86e531b). Resolve them first"
}
```

<!-- capture: 259-payout-unfreeze-refused-shortfall.json response.body -->
```json
{
  "detail": "This caregiver still owes 800000 kobo that Paystack paid beyond their earnings, even after 0 kobo is taken from their available balance. Unfreeze with write_off_shortfall to write it off, or leave them frozen"
}
```

Then 11,000.00 NGN of earnings reach the caregiver while they are frozen (**[db-simulated]**: the balance was set in the database just before the call, as completed bookings would). The same unfreeze, without a write-off, now debits the 8,000.00 NGN shortfall from the balance (3,000.00 NGN is left) and succeeds (capture `260`):

<!-- capture: 260-payout-unfreeze-debits-balance.json request.body -->
```json
{
  "reason": "Earnings kept accruing while frozen; the shortfall is taken from them"
}
```

<!-- capture: 260-payout-unfreeze-debits-balance.json response.body -->
```json
{
  "caregiver_id": "63f4b91b-db58-4589-ab8e-8d9acc679e17",
  "payouts_frozen_at": null,
  "debited_from_balance_kobo": 800000,
  "written_off_shortfall_kobo": 0,
  "outstanding_shortfall_kobo": 0
}
```

The same call **with** `write_off_shortfall: true` for another caregiver whose balance is empty (capture `275`, a 5,000.00 NGN shortfall, nothing to debit):

<!-- capture: 275-payout-unfreeze-write-off.json request.body -->
```json
{
  "reason": "Caregiver agreed to a write-off of the 5,000.00 NGN shortfall",
  "write_off_shortfall": true
}
```

<!-- capture: 275-payout-unfreeze-write-off.json response.body -->
```json
{
  "caregiver_id": "5d4824cb-7fa1-48e8-bc68-b7ae067bef4a",
  "payouts_frozen_at": null,
  "debited_from_balance_kobo": 0,
  "written_off_shortfall_kobo": 500000,
  "outstanding_shortfall_kobo": 0
}
```

Errors:

| Status | `detail` | When |
|---|---|---|
| 403 | `Access denied. Missing permission: ledger.adjust` | Finance _(capture `253`)_ |
| 422 | the validation list | no `reason`, or a blank one, or an unknown field _(capture `254`)_ |
| 404 | `Caregiver not found` | unknown id _(capture `255`)_ |
| 409 | `This caregiver's payouts are not frozen` | already unfrozen, or never frozen _(capture `256`)_ |
| 409 | `<n> payout(s) of this caregiver still need attention or are processing (<ids>). Resolve them first` | any payout `needs_attention` or `processing` _(capture `257`)_ |
| 409 | `This caregiver still owes <n> kobo that Paystack paid beyond their earnings, even after <m> kobo is taken from their available balance. Unfreeze with write_off_shortfall to write it off, or leave them frozen` | a shortfall the balance cannot cover, without `write_off_shortfall` _(capture `259`)_ |

Other unfreezes: after a reversal (capture `223`), after a "no record" flag (capture `267`), after a netted resolve (capture `301`): each now carries `debited_from_balance_kobo: 0`.

The audit entries (`payout.unfreeze`, entity `caregiver`, `entity_id` is the caregiver id; `before` has the freeze time and reason and the outstanding shortfall, `after` the amount debited from the balance (`debited_from_balance_kobo`), the amount written off, what stays recorded and `per_payout`, the split by payout):

<!-- capture: 302-audit-payout-unfreeze.json response.body path=items first=1 -->
```json
[
  {
    "id": "e5bd6838-4fbb-4d94-8d6b-b1c2063b52dc",
    "admin_user_id": "6255fd01-7112-40cc-a9cf-e005bf4c3bf2",
    "admin_email": "boss-d704c6pay@example.com",
    "action": "payout.unfreeze",
    "entity_type": "caregiver",
    "entity_id": "e1806387-f470-4c99-a7ec-45f5a10806ad",
    "before": {
      "payouts_frozen_at": "2026-10-19T13:29:54.602355",
      "payouts_frozen_reason": "Payout ab693793-b0aa-471a-904e-95a1f1fae4d9: Paystack paid attempt 1 after the payout was marked failed; 0 kobo was taken back from the available balance, 1000000 kobo could not be recovered (shortfall).",
      "outstanding_shortfall_kobo": 0
    },
    "after": {
      "payouts_frozen_at": null,
      "debited_from_balance_kobo": 0,
      "written_off_shortfall_kobo": 0,
      "outstanding_shortfall_kobo": 0,
      "per_payout": {}
    },
    "reason": "Both transfers accounted for",
    "ip": "172.19.0.1",
    "user_agent": "python-httpx/0.28.1",
    "created_at": "2026-10-19T13:29:54.903607"
  }
]
```

### The audit trail

Every approval, cancel, retry, resolve and unfreeze writes an `admin_audit_log` row: `payout_batch.cancel` (entity `payout_batch`; `after` lists the cancelled payouts and `returned_total_kobo`), `payout.resolve` (entity `payout`; `after` carries the payout's status, `outcome`, `forced`, `paystack_said`, the sentence describing Paystack's answer, and `returned_kobo` / `netted_kobo`), `payout.unfreeze` (entity `caregiver`), `payout_batch.approve` (entity `payout_batch`; `after` lists the approved payouts, the total and any payouts cancelled at approval), `payout.cancel` and `payout.retry` (entity `payout`; `before`/`after` carry status, amount, reference and attempts). Webhook and job settlements have **no admin**, so they leave no audit row: the payout row itself (`status`, `completed_at`, `failure_reason`, `transfer_code`) is their record.

<!-- capture: 229-audit-payouts.json response.body path=items first=1 -->
```json
[
  {
    "id": "96545600-5e1e-48d4-81ea-7340160f8876",
    "admin_user_id": "782b30c4-afec-4093-b436-94b567e953b1",
    "admin_email": "fin-d704c6pay@example.com",
    "action": "payout_batch.approve",
    "entity_type": "payout_batch",
    "entity_id": "ecd85e88-3c30-4858-8046-69b2ed56fe3a",
    "before": {
      "status": "pending_approval"
    },
    "after": {
      "status": "approved",
      "approved_payouts": [
        "d78d3234-95ec-4a0c-9241-83d4adf920d4",
        "87e77b91-5f35-4ea9-b8ea-5b7919230d4b",
        "095b3872-1a06-47e7-b604-78c9ba893f04",
        "f11dc9d6-778b-4433-8e31-b51251bb86cd",
        "1ca00fde-0db3-4909-8256-65ebd86c2d6b"
      ],
      "approved_total_kobo": 9200000,
      "cancelled_at_approval": []
    },
    "reason": "Placeholder: totals checked against the ledger",
    "ip": "172.19.0.1",
    "user_agent": "python-httpx/0.28.1",
    "created_at": "2026-10-19T13:29:08.616162"
  }
]
```

<!-- capture: 230-audit-payout-actions.json response.body path=items first=2 -->
```json
[
  {
    "id": "c49cf100-935f-4030-b8d7-522e9b717c5f",
    "admin_user_id": "6255fd01-7112-40cc-a9cf-e005bf4c3bf2",
    "admin_email": "boss-d704c6pay@example.com",
    "action": "payout.retry",
    "entity_type": "payout",
    "entity_id": "d78d3234-95ec-4a0c-9241-83d4adf920d4",
    "before": {
      "status": "failed",
      "amount_kobo": 3000000,
      "caregiver_id": "1ea14f8e-28ac-4c89-bfd4-15c81e8487f5",
      "reference": "po-d78d323495ec4a0c924183d4adf920d4-2",
      "attempts": 2,
      "shortfall_kobo": 0,
      "returned_kobo": 3000000,
      "netted_kobo": 0
    },
    "after": {
      "status": "processing",
      "amount_kobo": 3000000,
      "caregiver_id": "1ea14f8e-28ac-4c89-bfd4-15c81e8487f5",
      "reference": "po-d78d323495ec4a0c924183d4adf920d4-3",
      "attempts": 3,
      "shortfall_kobo": 0,
      "returned_kobo": 3000000,
      "netted_kobo": 0
    },
    "reason": "Reversal was a bank hiccup; send again",
    "ip": "172.19.0.1",
    "user_agent": "python-httpx/0.28.1",
    "created_at": "2026-10-19T13:29:14.472789"
  },
  {
    "id": "c28dc17a-7386-47a4-8124-afe615b309ad",
    "admin_user_id": "6255fd01-7112-40cc-a9cf-e005bf4c3bf2",
    "admin_email": "boss-d704c6pay@example.com",
    "action": "payout.resolve",
    "entity_type": "payout",
    "entity_id": "d78d3234-95ec-4a0c-9241-83d4adf920d4",
    "before": {
      "status": "needs_attention",
      "amount_kobo": 3000000,
      "caregiver_id": "1ea14f8e-28ac-4c89-bfd4-15c81e8487f5",
      "reference": "po-d78d323495ec4a0c924183d4adf920d4-2",
      "attempts": 2,
      "shortfall_kobo": 0,
      "returned_kobo": 0,
      "netted_kobo": 0
    },
    "after": {
      "status": "failed",
      "amount_kobo": 3000000,
      "caregiver_id": "1ea14f8e-28ac-4c89-bfd4-15c81e8487f5",
      "reference": "po-d78d323495ec4a0c924183d4adf920d4-2",
      "attempts": 2,
      "shortfall_kobo": 0,
      "returned_kobo": 3000000,
      "netted_kobo": 0,
      "outcome": "failed",
      "forced": false,
      "paystack_said": "Paystack reports this transfer as reversed"
    },
    "reason": "Paystack shows the transfer as reversed and the money back in our balance",
    "ip": "172.19.0.1",
    "user_agent": "python-httpx/0.28.1",
    "created_at": "2026-10-19T13:29:14.427693"
  }
]
```

The audit entries of the new actions (a batch cancellation, and a resolve with the forced flag):

<!-- capture: 284-audit-payout-batch-cancel.json response.body path=items first=1 -->
```json
[
  {
    "id": "cb67b756-70b4-44bf-af9b-4b40ef56739c",
    "admin_user_id": "782b30c4-afec-4093-b436-94b567e953b1",
    "admin_email": "fin-d704c6pay@example.com",
    "action": "payout_batch.cancel",
    "entity_type": "payout_batch",
    "entity_id": "b0320517-068c-429f-8afb-16295b8ab814",
    "before": {
      "status": "pending_approval"
    },
    "after": {
      "status": "cancelled",
      "cancelled_payouts": [
        "6a20177e-d2d7-4b4b-a76e-438033e9aa5b",
        "09b4155c-6144-4a3c-b6b1-4b5a43c8bf23",
        "ca0c0532-2c87-411f-a3c2-6a64afabed1f",
        "1fd3801b-47ca-48b4-a312-26b5b2cf8c09",
        "c1a70a7b-f78b-457d-8ae8-4ebad03591ac"
      ],
      "returned_total_kobo": 6800000
    },
    "reason": "Placeholder: the totals do not match Finance's report",
    "ip": "172.19.0.1",
    "user_agent": "python-httpx/0.28.1",
    "created_at": "2026-10-19T13:29:33.511541"
  }
]
```

<!-- capture: 285-audit-payout-resolve.json response.body path=items first=1 -->
```json
[
  {
    "id": "ca6764c9-761a-4d41-806e-ad03c327db1b",
    "admin_user_id": "6255fd01-7112-40cc-a9cf-e005bf4c3bf2",
    "admin_email": "boss-d704c6pay@example.com",
    "action": "payout.resolve",
    "entity_type": "payout",
    "entity_id": "ded559d1-47f5-4137-ba63-6799c94b966f",
    "before": {
      "status": "needs_attention",
      "amount_kobo": 800000,
      "caregiver_id": "5d4824cb-7fa1-48e8-bc68-b7ae067bef4a",
      "reference": "po-ded559d147f54137ba636799c94b966f-1",
      "attempts": 1,
      "shortfall_kobo": 500000,
      "returned_kobo": 0,
      "netted_kobo": 0
    },
    "after": {
      "status": "success",
      "amount_kobo": 800000,
      "caregiver_id": "5d4824cb-7fa1-48e8-bc68-b7ae067bef4a",
      "reference": "po-ded559d147f54137ba636799c94b966f-1",
      "attempts": 1,
      "shortfall_kobo": 500000,
      "returned_kobo": 0,
      "netted_kobo": 0,
      "outcome": "paid",
      "forced": false,
      "paystack_said": "Paystack reports this transfer as successful"
    },
    "reason": "Paystack shows the transfer as successful",
    "ip": "172.19.0.1",
    "user_agent": "python-httpx/0.28.1",
    "created_at": "2026-10-19T13:29:30.240002"
  }
]
```

_captures `283`, `284`_

**UX the console must surface.**
- **Approving pays real money.** Show the batch total and the number of payouts, list who gets what, and use a confirmation that says "this sends N transfers totalling X". There is no undo after approval except Paystack's own answer.
- Show a `processing` payout older than about 30 minutes with a warning: the webhook may be late (the reconcile job is checking). A `processing` payout with a `failure_reason` mentioning OTP needs a person.
- A `failed` or `reversed` payout shows `failure_reason` and a Retry button (Super Admin only). Say that the money is back in the caregiver's balance and that the next batch may pay it instead.
- A payout cancelled with no `cancelled_by` was cancelled by the system at approval: show "Cancelled automatically" and the reason.
- **A `needs_attention` payout is an alert, not a status badge.** Surface a count of them on the payouts screen and show `failure_reason` verbatim. Offer **Resolve** (not Retry). Make the admin choose `paid` or `failed`, and show what Paystack said in the `409` text when it refuses. Show the `force` option only after a refusal that says the answer is not final, with a warning that **forcing `failed` returns money that may still be paid**, and require a reason that says how it was checked.
- Show `shortfall_kobo` (as naira) wherever a `needs_attention` payout appears: it is money Oakly paid out that the caregiver's balance could not cover, to be recovered by a ledger adjustment or written off when unfreezing.
- **Show the freeze on the caregiver, not only on the payout.** Whenever `caregiver_payouts_frozen_at` is set, show a banner with `caregiver_payouts_frozen_reason`: "This caregiver's payouts are frozen. They are left out of batches and no money returns to their balance until an admin resolves their payouts and unfreezes them." Do not offer Retry for any of their payouts.
- **Say what Resolve-as-`failed` will do before it is clicked.** For a frozen caregiver it returns `max(0, reserved - outstanding shortfall)`: show "Returns X, keeps Y against a shortfall" (the response's `returned_kobo` and `netted_kobo` confirm it afterwards). Money does NOT return by itself when Paystack later reports a failure for a frozen caregiver: tell the admin the payout still needs resolving.
- **Offer Unfreeze only when no payout of the caregiver is `needs_attention` or `processing`.** If a shortfall is left, ask explicitly whether to write it off or leave it recorded (`write_off_shortfall`); never default to writing it off.
- Never offer Retry for `needs_attention`, and do not offer Resolve-as-`failed` for a payout whose `attempt_history` has a `success`.
- A **Cancel batch** button belongs on a batch that is `pending_approval` (next to Approve); after approval it is refused. Confirm with the total that will go back.
- After a `409` from Retry that says Paystack reports the transfer as successful, **reload the payout**: it is now `needs_attention`, not `failed`, and the caregiver is frozen. A payout that Paystack reversed after paying it is also `needs_attention`, not `reversed`: the money is not back until an admin resolves it.
- Show a payout cancelled with no `cancelled_by` as "Cancelled automatically" with the reason, whether it was cancelled at approval or before sending.
- Payouts are weekly by default; show the policy's `payout_cadence` and `payout_min_naira` on the batch screen and link to the booking policy editor.
- The console can never see a full bank account number here; do not build a "reveal" control.

## 12.12 Refunds (A3 slice 3): `/api/v1/admin/refunds`

**What it is, in the console's terms.** A **refund** is one row in a queue that Finance works. Every refund is created `pending_approval`, a **person always approves it** (nothing is refunded automatically, even when nobody was at fault), it is paid **once**, through **one destination** (the family's original payment method through Paystack, or the family's Oakly wallet), and its status is **Paystack's word**, not a guess. A booking can have several refunds over its life (for example a fee refunded after the main refund) but **never two open at once**.

A refund is created by:

| Origin | `origin` | How | Evidence |
|---|---|---|---|
| A cancel path | `system` | An admin cancels a **paid** booking with `refund: full` or `partial` (section 12.6); the caregiver cancels a paid **app** booking; the booker cancels on WhatsApp; a payment lands after the booking expired or was cancelled. `suggested_amount_naira` comes from the cancel policy: the whole payment for a no-fault cancel, the payment less the cancellation fee for a family cancel, the admin's amount for a partial cancel. `requested_by` is `null`. | Admin cancel: 304, 306. The other three paths: **[not live]**, tests only (`test_refunds_create.py`, `test_whatsapp_notifications.py`); no caregiver app or WhatsApp user was driven in the capture run |
| A person | `staff` | `POST /admin/refunds`, for a complaint or anything else, on a **completed or cancelled** booking only. `requested_by` is the admin. | 328, 364 |

**[seeded]** The patients, caregivers and the 13 paid bookings (55,000.00 NGN each) are inserted directly (capture 303). **[fake Paystack]** Every Paystack answer and webhook below comes from a **local fake** shaped after Paystack's documented refund replies; the webhooks are correctly signed with the stack's placeholder key. **[simulated schedule]** The send and reconcile steps are the **real functions** run in a one-off worker container (in production the ARQ cron runs them: send every minute, reconcile every 15 minutes). **A live Paystack account has not been exercised**, so real Paystack timings (a processed card refund reaches the family **up to 10 business days later**) and real WhatsApp delivery are **[not live]**.

| Route | Permission | Notes |
|---|---|---|
| `GET /admin/refunds` | any of `refunds.request`, `refunds.approve`, `payments.read` (Operations, Support, Finance, Super Admin) | filters `status`, `reason_code`, `booking_id`, `above_limit`; `limit` (1 to 200, default 50), `offset`; newest first. Returns `{items, total, limit, offset}` |
| `GET /admin/refunds/{id}` | same | the refund plus its booking context, attempts and the caller's `available_actions` |
| `POST /admin/refunds` | `refunds.request` | body `{booking_id, amount_naira, reason_code, note?}`; **`201`** with the detail |
| `POST /admin/refunds/{id}/approve` | `refunds.approve` (Finance, Super Admin), plus `refunds.approve_above_limit` (**Super Admin only**) above the limit or for the wallet | body `{amount_naira?, destination?, take_back_from_caregiver?, take_back_reason?, note?}`, all optional; returns the detail |
| `POST /admin/refunds/{id}/reject` | `refunds.approve` | body `{reason}` |
| `POST /admin/refunds/{id}/retry` | `refunds.approve` | no body; only a `failed` refund |
| `POST /admin/refunds/{id}/bank-details` | `refunds.approve` | body `{bank_code, account_number}`; only a `needs_attention` refund |
| `POST /admin/refunds/{id}/switch-to-wallet` | `refunds.approve_above_limit` (**Super Admin only**) | no body; only a `failed` refund of an app booker |
| `POST /admin/refunds/{id}/close` | `refunds.approve` | body `{reason}`; only a `failed` refund |
| `POST /admin/refunds/{id}/resolve` | `refunds.approve_above_limit` (**Super Admin only**) | body `{outcome: "succeeded" or "failed", note, amount_naira?}`; only a refund in `needs_review` (or flagged) |

Every write returns the **refund detail** (the same shape as `GET /admin/refunds/{id}`), so the console can replace the row without a second call. Every write is audited (section "The audit trail" below). **There is no "send" route**: approving does not send, the send job does (up to a minute later).

**Money is NAIRA here, as JSON numbers with at most two decimals** (`55000.0`, `14545.45`), **not kobo**. The booking routes (section 12) and the payouts (12.11) speak integer kobo; this API does not. Request amounts with more than two decimals are a `422` (capture 319). One message is still in kobo (capture 331, see the errors table).

### Reading the queue

The default `status` is **`open`**: every unfinished refund (`pending_approval`, `approved`, `processing`, `needs_attention`, `needs_review`, `failed`) **plus finished ones flagged for review** (`review_flagged_at` not `null`). `all` removes the filter; any single status filters to it. `reason_code` is one of `cancelled_by_booker`, `caregiver_cancelled`, `no_caregiver_found`, `admin_cancelled`, `paid_after_expiry`, `paid_after_cancellation`, `complaint`, `other`. `above_limit=true` keeps refunds whose booking's refunds total more than the Finance limit.

<!-- capture: 307-refunds-list.json response.body path=items first=1 -->
```json
[
  {
    "id": "d7b74acb-16af-4965-b6b3-5de7e7995b5b",
    "booking_id": "e559a2e8-c4a0-489f-a091-9cb3ab147178",
    "booking_code": "22D0E98F",
    "status": "pending_approval",
    "reason_code": "admin_cancelled",
    "origin": "system",
    "suggested_amount_naira": 12000.0,
    "amount_naira": null,
    "destination": null,
    "take_back_from_caregiver": false,
    "take_back_naira": 0.0,
    "failure_reason": null,
    "note": null,
    "rejected_reason": null,
    "requested_by": null,
    "approved_by": null,
    "review_flagged_at": null,
    "review_reason": null,
    "created_at": "2026-10-21T14:29:06.229633",
    "approved_at": null,
    "processed_at": null,
    "max_approvable_naira": 50000.0,
    "available_actions": [
      "approve",
      "reject"
    ]
  }
]
```

_capture `307-refunds-list.json` (first of three items; `total` was 10)_

A filtered list (`status=pending_approval`, `reason_code=admin_cancelled`, `above_limit=true`):

<!-- capture: 308-refunds-list-filtered.json response.body keys=total,limit,offset -->
```json
{
  "total": 9,
  "limit": 2,
  "offset": 0
}
```

Errors: a bad `status` is a `422` with a list `detail` (capture 310); no token `401` (312); a signed-in admin with none of the three read permissions (the Clinical Reviewer) gets `403` `Access denied. Missing permission: one of refunds.request, refunds.approve, payments.read` (311).

### The refund object, and the detail

<!-- capture: 313-refund-detail-system.json response.body -->
```json
{
  "id": "7903c1f2-5fbd-4628-aab9-b5ce9e83a600",
  "booking_id": "4ec47135-8b9b-46cc-83d5-92f12d18b440",
  "booking_code": "780212EB",
  "status": "pending_approval",
  "reason_code": "admin_cancelled",
  "origin": "system",
  "suggested_amount_naira": 55000.0,
  "amount_naira": null,
  "destination": null,
  "take_back_from_caregiver": false,
  "take_back_naira": 0.0,
  "failure_reason": null,
  "note": null,
  "rejected_reason": null,
  "requested_by": null,
  "approved_by": null,
  "review_flagged_at": null,
  "review_reason": null,
  "created_at": "2026-10-21T14:29:06.129551",
  "approved_at": null,
  "processed_at": null,
  "max_approvable_naira": 50000.0,
  "available_actions": [
    "approve",
    "reject"
  ],
  "booking": {
    "code": "780212EB",
    "status": "cancelled",
    "channel": "app",
    "cancel_fee_tier": null,
    "cancel_fee_naira": null,
    "caregiver_assigned": false,
    "caregiver_arrived": false,
    "completed": false,
    "total_naira": 55000.0,
    "paystack_fee_naira": 825.0,
    "refunded_naira": 0.0,
    "refundable_naira": 55000.0
  },
  "suggestion_why": "Set by the admin who cancelled the booking.",
  "attempts": [],
  "take_back_reason": null,
  "take_back_preview_naira": 0.0,
  "take_back_debited_naira": null,
  "take_back_owed_naira": null,
  "needs_super_admin": true,
  "limit_naira": 50000.0
}
```

_capture `313-refund-detail-system.json`: a system refund of a cancelled booking, read by Finance_

**Field-nesting and null traps**

| Field | What to know |
|---|---|
| `suggested_amount_naira` vs `amount_naira` | `suggested_amount_naira` is what the policy said, frozen at creation; it is `null` when "a person must enter the amount" (a started visit cancelled by the caregiver, for example). `amount_naira` is **`null` until the refund is approved**, then the amount approved. Show the suggestion while pending, the amount afterwards. |
| `destination` | `null` until approved, then `original_method` or `wallet`. |
| `requested_by` / `approved_by` | admin ids. `requested_by` is `null` for a `system` refund; both are `null` until they happen. They are ids only: look the name up in the admin list if you need it. |
| `take_back_naira`, `take_back_preview_naira`, `take_back_debited_naira`, `take_back_owed_naira` | `take_back_naira` is the amount **agreed at approval** (`0.0` when not taken back). `take_back_preview_naira` is what taking back **would** be: `0.0` for a booking that is not completed, a number for a completed one, and **`null` once the refund is finished** (succeeded, rejected, closed). `take_back_debited_naira` and `take_back_owed_naira` are **`null` until the money has left** and the take-back was applied, then the split: debited from the caregiver's wallet now, and owed (recovered from her next earnings). |
| `review_flagged_at`, `review_reason` | set when Paystack contradicted Oakly (see "Needs review"). **They can be set on a refund whose `status` is `succeeded`, `closed` or `rejected`** (when the booking already has another open refund, the contradicting refund keeps its finished status and carries the flag). So the console's "needs a person" filter is **`status = needs_review` OR `review_flagged_at != null`**, which is exactly the default `open` list. |
| `failure_reason` | Paystack's text for the current attempt, or Oakly's explanation. On a refund in `needs_review` it repeats the `review_reason`. Cleared when the refund is retried. |
| `max_approvable_naira` vs `needs_super_admin` | **Both are about the caller.** `max_approvable_naira` is the most **this admin** may approve now: what is left to refund on the booking, and (without `refunds.approve_above_limit`) also what is left under the Finance limit, counting this booking's other refunds. `needs_super_admin` (detail only) is `true` when this refund's amount (the approved amount, else the suggestion) is **more than `max_approvable_naira`** for this caller. A Super Admin sees `needs_super_admin: false` for the same refund (capture 314). It is also computed for finished refunds, where it means nothing: ignore both fields once the refund is terminal. |
| `limit_naira` | the Finance limit in force (policy `finance_refund_limit_naira`, 50,000.00 by default). |
| `booking.refunded_naira` / `refundable_naira` | what is committed to refunds of this booking **including this one once approved** (not what has been paid out), and what is still refundable. `refundable_naira` is `0.0` while a 55,000.00 refund of a 55,000.00 booking is merely approved. |
| `booking.cancel_fee_tier`, `cancel_fee_naira` | `null` for an admin cancel (no tier applies); set for a family cancel. `cancel_fee_naira` is `null` when the booking has no policy copy to work it out from. |
| `booking.paystack_fee_naira` | `null` for bookings paid before this slice. Oakly **absorbs** Paystack's fee on every refund: the family gets every naira of the refund amount. |
| `attempts[]` | one entry per call to Paystack, oldest first. `paystack_refund_id` is `null` until Paystack answered. `bank_code`, `account_last4`, `account_name` are filled only after bank details were given **and the caller holds `refunds.approve` or `payments.read`**: for **Support and Operations they are `null`** even when set (capture 359 vs 358). The full account number is **never stored and never returned** (no response in these captures contains it). |
| `suggestion_why` | a plain sentence for the console to show under the suggestion. |
| `available_actions` | what **this caller** may do **now**, a subset of `approve`, `reject`, `retry`, `bank_details`, `switch_to_wallet`, `close`, `resolve`. **Render buttons from this list; never re-derive permissions.** It is empty for a finished refund. |

**`available_actions` is the truth about "someone else must approve it".** The admin who **raised** a staff refund is never offered `approve` (they may still `reject` their own request), and an approver with **nothing left under their limit** is not offered it either. The admin who **approved or raised** a refund is never offered `bank_details` for it. Compare 351 (Finance who approved it: `[]`) with 352 (another Finance admin: `["bank_details"]`), and 364 (the raiser: `["reject"]`) with 365 (another Finance admin: `["approve", "reject"]`).

### Raising a refund by hand: `POST /admin/refunds`

<!-- capture: 328-refund-request.json request.body -->
```json
{
  "booking_id": "0f511a01-411a-4f5c-b4e2-22b22a67ec07",
  "amount_naira": 5000,
  "reason_code": "complaint",
  "note": "Placeholder: the family complained about the cancellation fee"
}
```

<!-- capture: 328-refund-request.json response.body keys=id,booking_code,status,reason_code,origin,suggested_amount_naira,amount_naira,requested_by,note,available_actions -->
```json
{
  "id": "650d0029-531c-4d1c-90b5-11acc207b626",
  "booking_code": "E819BAF2",
  "status": "pending_approval",
  "reason_code": "complaint",
  "origin": "staff",
  "suggested_amount_naira": 5000.0,
  "amount_naira": null,
  "requested_by": "e32c8e64-e715-4453-92f9-7b48d39dfb96",
  "note": "Placeholder: the family complained about the cancellation fee",
  "available_actions": []
}
```

_capture `328-refund-request.json`: **`201`**, trimmed_

The person who raised it cannot approve it: `403` `You raised this refund, so someone else must approve it.` (capture 366, below). A refund can be raised only for a **completed or cancelled** booking that was paid; a booking with an open refund cannot have a second.

### Approving: the limit, a lower amount, the wallet

The Finance limit is **cumulative per booking**: this refund **plus** the booking's earlier approved, in-flight and succeeded refunds, so a large refund cannot be split to get under it. Finance approves up to the limit; a Super Admin approves any amount. The approver may approve a **lower amount** than suggested, never more than is still refundable.

| Case | Capture | Result |
|---|---|---|
| Finance, suggestion above the limit | 317 | `403` |
| Finance, a lower amount within the limit | 321 | `200`, status `approved`, `amount_naira` 20000 |
| Super Admin, the whole 55,000.00 | 323 | `200`, status `approved` |
| Finance, suggestion within the limit | 324 | `200` |
| Finance asks for the wallet | 320 | `403` |
| Super Admin, wallet | 339 | `200`, **already `succeeded`** (the wallet is credited in the same call) |
| Super Admin, wallet, WhatsApp booker | 338 | `422` |
| Support (no `refunds.approve`) | 316 | `403` |

<!-- capture: 317-refund-approve-above-limit-as-finance.json response.body -->
```json
{
  "detail": "Refunds on this booking would total ₦55,000, above the ₦50,000 limit for Finance. A Super Admin must approve it."
}
```

<!-- capture: 321-refund-approve-lower-amount.json request.body -->
```json
{
  "amount_naira": 20000,
  "note": "Approving 20,000.00 NGN now; the rest can be raised later"
}
```

<!-- capture: 321-refund-approve-lower-amount.json response.body keys=status,amount_naira,destination,approved_by,approved_at,max_approvable_naira,available_actions -->
```json
{
  "status": "approved",
  "amount_naira": 20000.0,
  "destination": "original_method",
  "approved_by": "e935f884-e2d3-41bc-9cd9-8237111d16c8",
  "approved_at": "2026-10-21T14:29:06.424294",
  "max_approvable_naira": 50000.0,
  "available_actions": []
}
```

_capture `321-refund-approve-lower-amount.json`: **`approved` is not sent yet.** The send job sends it within a minute._

<!-- capture: 339-refund-approve-wallet.json response.body keys=status,destination,amount_naira,processed_at,available_actions -->
```json
{
  "status": "succeeded",
  "destination": "wallet",
  "amount_naira": 55000.0,
  "processed_at": "2026-10-21T14:29:06.697764",
  "available_actions": []
}
```

_capture `339-refund-approve-wallet.json`: wallet credit is immediate, so the refund is `succeeded` and has `processed_at`_

**Take-back (a refund of a completed visit).** The approver may take the caregiver's share of the refund back: send `take_back_from_caregiver: true` with a `take_back_reason`. It is only for a **completed** visit with a caregiver earning (`422` otherwise, capture 337); the reason is required (`422`, capture 367). The amount is **proportional and not editable**: the earning's share of what is refunded (here a 40,000.00 earning, 20,000.00 of 55,000.00 refunded: 14,545.45), capped at the earning less earlier take-backs. Show `take_back_preview_naira` **before** the approver confirms (capture 365). **Nothing is taken at approval; it happens when Paystack says the money has left.** It follows what the caregiver actually received: money still waiting to clear is reduced there, money already in her wallet is debited, and whatever the wallet cannot cover is recorded as **owed** and recovered from her **next earnings** (and from any payout, which cannot send money that is owed). The caregiver is never asked to repay. Result after the money left (capture 380): `take_back_debited_naira` 1000 (all her wallet held) and `take_back_owed_naira` 13545.45. The `late_cancel_compensation` a caregiver earned is never reversed by the refund of its own booking.

<!-- capture: 365-refund-detail-take-back-preview.json response.body keys=status,take_back_preview_naira,available_actions,booking -->
```json
{
  "status": "pending_approval",
  "take_back_preview_naira": 14545.45,
  "available_actions": [
    "approve",
    "reject"
  ],
  "booking": {
    "code": "A18FA979",
    "status": "completed",
    "channel": "app",
    "cancel_fee_tier": null,
    "cancel_fee_naira": null,
    "caregiver_assigned": true,
    "caregiver_arrived": true,
    "completed": true,
    "total_naira": 55000.0,
    "paystack_fee_naira": 825.0,
    "refunded_naira": 0.0,
    "refundable_naira": 55000.0
  }
}
```

<!-- capture: 368-refund-approve-with-take-back.json request.body -->
```json
{
  "take_back_from_caregiver": true,
  "take_back_reason": "The caregiver left the visit early"
}
```

<!-- capture: 380-refund-detail-take-back-applied.json response.body keys=status,take_back_from_caregiver,take_back_naira,take_back_reason,take_back_debited_naira,take_back_owed_naira,take_back_preview_naira,processed_at -->
```json
{
  "status": "succeeded",
  "take_back_from_caregiver": true,
  "take_back_naira": 14545.45,
  "take_back_reason": "The caregiver left the visit early",
  "take_back_debited_naira": 1000.0,
  "take_back_owed_naira": 13545.45,
  "take_back_preview_naira": null,
  "processed_at": "2026-10-21T14:29:21.474143"
}
```

### Rejecting

`POST /admin/refunds/{id}/reject` `{"reason": "..."}` (a reason is required: `422` without, capture 325). Only a `pending_approval` refund; it becomes `rejected`, which is final and **frees the booking** so it can be refunded again. Again: `409` (327).

<!-- capture: 326-refund-reject.json response.body keys=status,rejected_reason,available_actions -->
```json
{
  "status": "rejected",
  "rejected_reason": "Raised by mistake: the family keeps the visit credit instead",
  "available_actions": []
}
```

### The refund's life, and what the console does in each state

```
pending_approval --approve--> approved --send job--> processing --refund.processed--> succeeded
      |                          |    (wallet: credited at approval, succeeded at once)
      +--reject--> rejected      |                       +--refund.needs-attention--> needs_attention --bank details--> processing
                                                         +--refund.failed--> failed --retry--> approved
                                                                                  +--switch to wallet (Super Admin)--> succeeded
                                                                                  +--close--> closed
   (anything Paystack contradicts) --> needs_review --resolve (Super Admin)--> succeeded | failed
```

| `status` | Open? | What it means | What the console shows or offers |
|---|---|---|---|
| `pending_approval` | yes | waiting for a person | `approve` / `reject` from `available_actions` |
| `approved` | yes | approved, **not sent yet** | "Waiting to be sent" (the send job runs every minute). Nothing to do. |
| `processing` | yes | sent to Paystack; waiting for its word | "Sent to Paystack". **Do not call it refunded.** |
| `needs_attention` | yes | Paystack cannot send the money back to the card and wants the family's bank account | `bank_details` (not to the admin who approved or raised it) |
| `failed` | yes (**still open**) | Paystack failed it and returned the money to Oakly | `retry`, `close`, and for a Super Admin of an app booker `switch_to_wallet`. It keeps the booking's open-refund slot until closed. |
| `needs_review` | yes | Paystack contradicts what Oakly recorded, or Paystack has no record of a send | `resolve` (Super Admin only). Nobody else can act. |
| `succeeded` | no | the money has left Oakly (Paystack `processed`, or the wallet was credited) | nothing; show `processed_at` |
| `rejected`, `closed` | no | finished, no money moved | nothing |

`approved` and `processing` count against what is still refundable the moment they are approved, so a second refund cannot be approved on top of them.

### Sending and Paystack's word

After approval the send job (`refund_dispatch_job`, every minute) claims the refund, records attempt N, and calls Paystack with the booking's payment reference and the amount. **It never sends a refund twice**: if Paystack does not answer, the attempt is left `unknown` and the refund stays `processing` until the reconcile job (every 15 minutes) asks Paystack.

<!-- capture: 341-worker-refund-dispatch-job.json response.body -->
```json
{
  "refunds_sent": 3,
  "refund_requests_seen_by_the_fake": [
    {
      "transaction": "BKG_780212EB_15cdb46e",
      "amount_kobo": 2000000
    },
    {
      "transaction": "BKG_FC463CFD_d4090458",
      "amount_kobo": 5500000
    },
    {
      "transaction": "BKG_22D0E98F_3a6a03d3",
      "amount_kobo": 1200000
    }
  ]
}
```

_capture `341-worker-refund-dispatch-job.json`: the job run by hand; the amounts are kobo here because they are the fake Paystack's own view_

<!-- capture: 342-refund-detail-processing.json response.body keys=status,amount_naira,failure_reason,attempts,available_actions -->
```json
{
  "status": "processing",
  "amount_naira": 20000.0,
  "failure_reason": null,
  "attempts": [
    {
      "attempt": 1,
      "status": "pending",
      "failure_reason": null,
      "paystack_refund_id": "1001",
      "bank_code": null,
      "account_last4": null,
      "account_name": null,
      "bank_details_submitted_at": null,
      "created_at": "2026-10-21T14:29:10.145603"
    }
  ],
  "available_actions": []
}
```

Paystack then tells Oakly by a signed webhook (`POST /payment/paystack/webhook`, section for the webhook is Paystack's, not the console's). The events the console sees the effect of, all handled idempotently (a repeat changes nothing):

<!-- capture: 343-paystack-refund-processing-webhook.json request.body -->
```json
{
  "event": "refund.processing",
  "data": {
    "status": "processing",
    "transaction_reference": "BKG_FC463CFD_d4090458",
    "refund_reference": null,
    "amount": "5500000",
    "currency": "NGN",
    "processor": "instant-transfer",
    "customer": {
      "email": "placeholder@example.com"
    },
    "integration": 412829,
    "domain": "test",
    "id": 1002
  }
}
```

_capture `343`: the shape of Paystack's refund webhook (**fake**, shaped after Paystack's documented one). The refund id is `data.id`; `refund_reference` is `null`; `amount` is a **string** of kobo._

<!-- capture: 345-refund-detail-succeeded.json response.body keys=status,amount_naira,processed_at,attempts,booking,available_actions -->
```json
{
  "status": "succeeded",
  "amount_naira": 55000.0,
  "processed_at": "2026-10-21T14:29:10.693794",
  "attempts": [
    {
      "attempt": 1,
      "status": "processed",
      "failure_reason": null,
      "paystack_refund_id": "1002",
      "bank_code": null,
      "account_last4": null,
      "account_name": null,
      "bank_details_submitted_at": null,
      "created_at": "2026-10-21T14:29:10.226966"
    }
  ],
  "booking": {
    "code": "FC463CFD",
    "status": "cancelled",
    "channel": "app",
    "cancel_fee_tier": null,
    "cancel_fee_naira": null,
    "caregiver_assigned": false,
    "caregiver_arrived": false,
    "completed": false,
    "total_naira": 55000.0,
    "paystack_fee_naira": 825.0,
    "refunded_naira": 55000.0,
    "refundable_naira": 0.0
  },
  "available_actions": []
}
```

_capture `345`: after `refund.processed`_

**The booking's `payment_status` changes when a refund succeeds.** `refunded` when the refunds that succeeded reach the whole payment, **`partially_refunded`** when they do not. **This is a new value on the booking routes** (section 12.2 filter `payment_status`, the list's `payment_status`, the detail's `payment.status`): the console's enum must accept `pending`, `paid`, `failed`, `partially_refunded` and `refunded`. These routes answered `500` for a partially refunded booking until these captures found it (fixed in commit `c1b9cdd`, with a test); they are verified live now (captures 340, 346, 348):

<!-- capture: 348-booking-after-partial-refund.json response.body path=payment -->
```json
{
  "status": "partially_refunded",
  "paystack_reference": "BKG_22D0E98F_3a6a03d3",
  "failure_reason": null,
  "expires_at": null,
  "refund_required": false,
  "refund_required_reason": "admin_cancelled_partial",
  "refund_amount_kobo": 1200000,
  "attempts": []
}
```

_capture `348`: `payment.status` is `partially_refunded` after 12,000.00 of 55,000.00 was refunded. `payment.refund_required` is `false` once a refund succeeds; the flag is only the "a refund is owed" marker until then._

**UX consequences the console must surface**

- **"Processed" is not "in the family's account".** For a card refund the family sees the money **up to 10 business days after** Paystack says `processed`. Never promise a date; say "Paystack has refunded it; it can take up to 10 business days to appear". **[not live]**: real timing was not exercised.
- **The family is told on WhatsApp** (template `refund_update`) when the refund is **sent to Paystack** ("being processed"), when it is **complete**, and when it **needs bank details** ("waiting for your bank details. Our team will contact you"). It is sent once per refund and status. **[not live]**: queued and asserted against a fake Meta in tests (`test_refunds_settle.py`, `test_whatsapp_notifications.py`), not delivered live. **App-channel bookers are not notified by this slice** (the booking's `payment_status` is the only signal); say so to the family on the phone.
- **"Someone else must approve it"**: when `approve` is missing for a refund the admin raised, show "Another admin must approve this refund" instead of an empty row; the API answers `403` `You raised this refund, so someone else must approve it.` if they try (capture 366).
- **Above the limit**: when `needs_super_admin` is `true` for the caller, show "A Super Admin must approve this" and offer a lower amount up to `max_approvable_naira`.
- **A taken-back share is recorded, not collected**: the preview is shown before the choice; after it, show debited vs owed.

### Needs attention: the family's bank details

Paystack sometimes cannot send the money back to the card and asks for a bank account. The refund goes to `needs_attention` and the family is told Oakly is waiting for their bank details. Finance gets them from the family (by phone) and enters them here.

<!-- capture: 350-paystack-refund-needs-attention-webhook.json request.body keys=event -->
```json
{
  "event": "refund.needs-attention"
}
```

<!-- capture: 352-refund-detail-needs-attention-other-finance.json response.body keys=status,available_actions -->
```json
{
  "status": "needs_attention",
  "available_actions": [
    "bank_details"
  ]
}
```

<!-- capture: 358-refund-bank-details.json request.body -->
```json
{
  "bank_code": "058",
  "account_number": "0000000077"
}
```

_capture `358`: the account number is a placeholder. It goes to Paystack and is **never stored**._

<!-- capture: 358-refund-bank-details.json response.body keys=status,attempts -->
```json
{
  "status": "processing",
  "attempts": [
    {
      "attempt": 1,
      "status": "processing",
      "failure_reason": null,
      "paystack_refund_id": "1001",
      "bank_code": "058",
      "account_last4": "0077",
      "account_name": "PLACEHOLDER ACCOUNT 0077",
      "bank_details_submitted_at": "2026-10-21T14:29:10.953165",
      "created_at": "2026-10-21T14:29:10.145603"
    }
  ]
}
```

_capture `358`: the attempt keeps the bank code, the last four digits and the account name Paystack resolved. The same refund seen by Support (capture 359) has `null` in all three._

The rules: `account_number` is exactly 10 digits (a shorter one is a `422` with a list `detail`, capture 355); `bank_code` must be in Paystack's bank list (`422` `Choose a bank from the list.`, 356); the admin who **approved** the refund, or raised it, cannot enter the details (`403`, capture 353), because the details decide where the money goes; above the Finance limit only a Super Admin can; a refund that is not waiting is a `409` (357); Operations and Support have no `refunds.approve` (`403`, 354). After success the refund is `processing` again and Paystack's webhook finishes it (captures 360, 361).

### Failed: retry, switch to the wallet, close

`failed` means Paystack returned the money to Oakly. The refund stays open. Before any of the three actions Oakly **asks Paystack whether the last attempt really failed** and proceeds only if Paystack says so; if Paystack says it is still moving or already processed, nothing is done and the answer is a `409` (a `processed` answer puts the refund in `needs_review`). If Paystack cannot be reached the answer is `503` and the refund stays `failed`.

- **Retry** (`refunds.approve`): the refund goes back to `approved`; the send job makes **attempt 2**. Attempt 1 stays in `attempts` with its reason (capture 378).
- **Switch to the wallet** (Super Admin): app bookers only; credits the patient's Oakly wallet and the refund is `succeeded` (384).
- **Close** (`refunds.approve`, reason required): the money stays with Oakly; the refund is `closed` and the booking can be refunded again (389).

<!-- capture: 373-refund-detail-failed.json response.body keys=status,failure_reason,attempts,available_actions -->
```json
{
  "status": "failed",
  "failure_reason": "Paystack reported the refund as failed",
  "attempts": [
    {
      "attempt": 1,
      "status": "failed",
      "failure_reason": "Paystack reported the refund as failed",
      "paystack_refund_id": "1005",
      "bank_code": null,
      "account_last4": null,
      "account_name": null,
      "bank_details_submitted_at": null,
      "created_at": "2026-10-21T14:29:17.801827"
    }
  ],
  "available_actions": [
    "retry",
    "close"
  ]
}
```

_capture `373`: a WhatsApp booker's failed refund. `switch_to_wallet` is absent because only app bookers have a wallet. Compare 406 (an app booker's refund after a resolve): `["retry", "switch_to_wallet", "close"]`._

<!-- capture: 378-refund-detail-after-retry.json response.body keys=status,attempts,available_actions -->
```json
{
  "status": "succeeded",
  "attempts": [
    {
      "attempt": 1,
      "status": "failed",
      "failure_reason": "Paystack reported the refund as failed",
      "paystack_refund_id": "1005",
      "bank_code": null,
      "account_last4": null,
      "account_name": null,
      "bank_details_submitted_at": null,
      "created_at": "2026-10-21T14:29:17.801827"
    },
    {
      "attempt": 2,
      "status": "processed",
      "failure_reason": null,
      "paystack_refund_id": "1009",
      "bank_code": null,
      "account_last4": null,
      "account_name": null,
      "bank_details_submitted_at": null,
      "created_at": "2026-10-21T14:29:20.896146"
    }
  ],
  "available_actions": []
}
```

<!-- capture: 389-refund-close.json response.body keys=status,note,booking,available_actions -->
```json
{
  "status": "closed",
  "note": "Closed: The family asked us to stop; they will use their credit instead",
  "booking": {
    "code": "68DACF41",
    "status": "cancelled",
    "channel": "app",
    "cancel_fee_tier": null,
    "cancel_fee_naira": null,
    "caregiver_assigned": false,
    "caregiver_arrived": false,
    "completed": false,
    "total_naira": 55000.0,
    "paystack_fee_naira": 825.0,
    "refunded_naira": 0.0,
    "refundable_naira": 55000.0
  },
  "available_actions": []
}
```

_capture `389`: after closing, `booking.refundable_naira` is back to the whole 55,000.00._

### Needs review: `POST /admin/refunds/{id}/resolve` (Super Admin)

When Paystack's word contradicts what Oakly recorded, **nothing moves by itself**: the refund goes to `needs_review`, an error is logged, and a Super Admin decides after looking at the Paystack dashboard. Causes include: Paystack processed a different amount (capture 391), Paystack processed an older attempt while a newer one exists (a possible double payment), Paystack reported `processed` for a refund that was failed or closed, a refund processed outside Oakly, and **Paystack having no record of a send 30 minutes after Oakly sent it** (captures 401 to 405).

<!-- capture: 392-refund-detail-needs-review.json response.body keys=status,failure_reason,review_flagged_at,review_reason,available_actions -->
```json
{
  "status": "needs_review",
  "failure_reason": "Paystack reported processed for 5400000 kobo, but this refund is 5500000 kobo.",
  "review_flagged_at": "2026-10-21T14:29:21.705900",
  "review_reason": "Paystack reported processed for 5400000 kobo, but this refund is 5500000 kobo.",
  "available_actions": [
    "resolve"
  ]
}
```

_capture `392`: processed for 54,000.00 where Oakly asked for 55,000.00_

<!-- capture: 405-refund-detail-no-record-review.json response.body keys=status,review_reason,attempts,available_actions -->
```json
{
  "status": "needs_review",
  "review_reason": "Paystack has no record of this refund, but it may have been sent. Check the Paystack dashboard before resolving it.",
  "attempts": [
    {
      "attempt": 1,
      "status": "unknown",
      "failure_reason": null,
      "paystack_refund_id": null,
      "bank_code": null,
      "account_last4": null,
      "account_name": null,
      "bank_details_submitted_at": null,
      "created_at": "2026-10-21T14:29:24.292704"
    }
  ],
  "available_actions": [
    "resolve"
  ]
}
```

_capture `405`: the reconcile job (clock moved 31 minutes ahead, **[simulated]**, captures 403 and 404) found Paystack has no record. That is not proof the refund was not sent, so it is `needs_review`, never `failed`._

`resolve` re-asks Paystack and the answer must agree with the outcome. `succeeded` records the success once (booking `payment_status`, take-back, the family told "complete"); `failed` makes the refund `failed` so retry, switch or close follow. A refund whose money already left cannot be marked failed (409); reversing it is a manual matter on the Paystack dashboard. When Paystack confirmed a **different amount**, the first call answers `409` naming it and the second call carries `amount_naira`:

<!-- capture: 398-refund-resolve-different-amount.json response.body -->
```json
{
  "detail": "Paystack processed a different amount from this refund's. To record what Paystack confirmed, resolve again with that amount (5400000 kobo)."
}
```

<!-- capture: 399-refund-resolve-succeeded.json request.body -->
```json
{
  "outcome": "succeeded",
  "note": "Checked the Paystack dashboard: 54,000.00 NGN processed",
  "amount_naira": 54000
}
```

<!-- capture: 399-refund-resolve-succeeded.json response.body keys=status,amount_naira,processed_at,review_flagged_at,review_reason,available_actions -->
```json
{
  "status": "succeeded",
  "amount_naira": 54000.0,
  "processed_at": "2026-10-21T14:29:21.799036",
  "review_flagged_at": null,
  "review_reason": null,
  "available_actions": []
}
```

<!-- capture: 406-refund-resolve-failed.json response.body keys=status,failure_reason,available_actions -->
```json
{
  "status": "failed",
  "failure_reason": "Marked failed after review: Checked the Paystack dashboard: no refund for this payment",
  "available_actions": [
    "retry",
    "switch_to_wallet",
    "close"
  ]
}
```

### Every error the console must handle

`detail` is a **string** for the errors the service raises on purpose, and a **list of objects** for request-validation `422`s (bad types, more than two decimals, a missing field, a bad enum, an account number that is not 10 digits). Branch on the status, show the string.

| Status | `detail` (exact) | When | Capture |
|---|---|---|---|
| `401` | `Not authenticated` | no token | 312 |
| `403` | `Access denied. Missing permission: refunds.approve` | approve, reject, retry, bank details, close without it | 316, 354, 374 |
| `403` | `Access denied. Missing permission: refunds.approve_above_limit` | switch to wallet, resolve as Finance | 383, 394 |
| `403` | `Access denied. Missing permission: one of refunds.request, refunds.approve, payments.read` | reads without any of the three | 311 |
| `403` | `Access denied. Missing permission: refunds.request` | raising without it | 335 |
| `403` | `Refunds on this booking would total ₦55,000, above the ₦50,000 limit for Finance. A Super Admin must approve it.` | Finance above the cumulative limit | 317 |
| `403` | `Only a Super Admin can refund to the Oakly wallet.` | Finance chooses the wallet | 320 |
| `403` | `You raised this refund, so someone else must approve it.` | approving your own staff refund | 366 |
| `403` | `Someone other than the person who approved this refund must enter the family's bank details.` | bank details by the approver or raiser | 353 |
| `404` | `Refund not found` / `Booking not found` | unknown id | 315, 334 |
| `409` | `This refund is no longer waiting for approval` | approve or reject twice | 322, 327 |
| `409` | `This booking already has a refund in progress` | raising on a booking with an open refund | 329 |
| `409` | `Only a failed refund can be retried, switched to the wallet or closed` | retry, switch or close of a refund that is not `failed` | 371, 385, 388 |
| `409` | `This refund is not waiting for bank details` | bank details when not `needs_attention` | 357 |
| `409` | `This refund is not waiting for review` | resolve of a refund not in review | 396 |
| `409` | `Paystack shows this refund as processed, not failed. Check the Paystack dashboard.` | resolve as failed while Paystack shows processed | 397 |
| `409` | `Paystack processed a different amount from this refund's. To record what Paystack confirmed, resolve again with that amount (5400000 kobo).` | resolve as succeeded for a different amount (**the amount is in kobo; send `amount_naira`**) | 398 |
| `422` | `A refund can be raised only for a completed or cancelled visit.` | raising on a pending or active booking | 330 |
| `422` | `The amount must be between 1 and 5500000 kobo, what is left to refund` | raising too much (**kobo in a naira API: convert it for display**) | 331 |
| `422` | `The amount must be between ₦0.01 and ₦55,000, what is left to refund` | approving too much | 318 |
| `422` | `The caregiver's share can only be taken back for a completed visit` | take-back on a booking that is not completed | 337 |
| `422` | `Give a reason for taking back the caregiver's share` | take-back without a reason | 367 |
| `422` | `Wallet credit is only for app users. Refund this one to the original payment method.` | wallet for a WhatsApp booker | 338 |
| `422` | `Choose a bank from the list.` | unknown bank code | 356 |
| `422` | list (`loc`, `msg`, `type`) | a missing or malformed field (no `reason`, more than 2 decimals, a zero amount, an unknown `reason_code`, a bad `status` filter, a short account number) | 310, 319, 325, 332, 333, 355, 387, 395 |

**[not live]** These service errors exist and are covered by tests, but were not provoked in the capture run (the string is quoted from `app/services/refunds.py`, so check the wording against a real response before building a translation table on it): `Paying this would refund more than the family paid. Check the Paystack dashboard.` (`409`); `This refund changed while Paystack was being asked. Look at it again.` (`409`); `We couldn't reach Paystack to confirm the last attempt failed. Try again shortly.` (**`503`**, also for the other "couldn't reach Paystack" messages); `Paystack shows another refund on this payment; check the Paystack dashboard before retrying.` (`409`); `Paystack shows an earlier attempt of this refund as processed, so the money has already left. ...` (`409`); `This account is closed, so it cannot be credited. Refund to the original payment method.` (`422`); `We could not find that account. Check the account number and the bank.` (`422`); `Paystack did not accept the bank details for this refund. Look at it again.` (`409`); `This refund is above the Finance limit, so a Super Admin must enter the family's bank details.` (`403`); `Money has already left for this refund (it was processed or credited), so it cannot be marked failed here. ...` (`409`).

### Polling

There is no push. **Poll `GET /admin/refunds/{id}` (or the list) every 30 to 60 seconds while a refund is `approved` or `processing`**, and stop on a terminal status. After a write, use the detail that the write returned. A refund normally leaves `approved` within a minute (the send job) and `processing` within minutes (Paystack's webhook); if it stays `processing` for more than about 15 minutes the reconcile job will ask Paystack on its own. The queue (`status=open`) is the Finance work list; refresh it on focus.

### The retired routes

`GET /patient/admin/refund/requests` and `PUT /patient/admin/refund/requests/{id}` (admin) and `POST /patient/wallet/refund/request` (patient) answer **`410`**. The admin ones keep their permission check in front of the 410, so an admin without `refunds.approve` gets `403`, not `410` (capture 413):

<!-- capture: 411-legacy-admin-refund-list.json response.body -->
```json
{
  "detail": "This route has moved to /api/v1/admin/refunds."
}
```

<!-- capture: 415-legacy-patient-refund-request.json response.body -->
```json
{
  "detail": "Refunds are now handled by the Oakly team. Contact support to request one."
}
```

### The audit trail

Every write is an `admin_audit_log` row with `entity_type` `refund` and `entity_id` the refund id: `refund.request`, `refund.approve`, `refund.reject`, `refund.retry`, `refund.bank_details`, `refund.switch_to_wallet`, `refund.close`, `refund.resolve`. Read them with `GET /admin/audit?entity_type=refund&action=...` (section 11; Super Admin and Finance hold `audit.read`). Amounts there are **kobo** (it is the audit's own free-form `before`/`after`).

<!-- capture: 407-audit-refund-approve.json response.body path=items.0 -->
```json
{
  "id": "4bc16631-1ae2-4ca2-8dc5-033707f87a19",
  "admin_user_id": "bc3ef2da-1b83-46df-936e-2e65fa83438e",
  "admin_email": "boss-cba871ref@example.com",
  "action": "refund.approve",
  "entity_type": "refund",
  "entity_id": "1888dd86-81b2-407f-a6f2-84f3dcaef0e7",
  "before": {
    "status": "pending_approval",
    "suggested_amount_kobo": 5500000
  },
  "after": {
    "booking_id": "9fd6a940-e5d7-4d98-9385-b499f1a1b71f",
    "status": "approved",
    "amount_kobo": 5500000,
    "destination": "original_method",
    "take_back_from_caregiver": false,
    "take_back_kobo": 0,
    "limit_kobo": 5000000,
    "cumulative_kobo": 5500000,
    "above_limit": true
  },
  "reason": null,
  "ip": "172.24.0.1",
  "user_agent": "python-httpx/0.28.1",
  "created_at": "2026-10-21T14:29:21.830120"
}
```

_capture `407`: the approval with the limit in force and whether this one was above it_

<!-- capture: 408-audit-refund-bank-details.json response.body path=items.0 keys=action,before,after,reason -->
```json
{
  "action": "refund.bank_details",
  "before": {
    "status": "needs_attention",
    "attempt": 1
  },
  "after": {
    "booking_id": "4ec47135-8b9b-46cc-83d5-92f12d18b440",
    "status": "processing",
    "attempt": 1,
    "bank_code": "058",
    "bank_id": "9",
    "account_last4": "0077",
    "account_name": "PLACEHOLDER ACCOUNT 0077"
  },
  "reason": null
}
```

_capture `408`: only the bank code, the last four digits and the resolved name are ever written down._

## 12.13 Care plans (B4): `/api/v1/admin/care-plans`

**What it is, in the console's terms.** A **care plan** is one client's record of care: a diagnosis, goals, daily tasks, medications, allergies and special instructions. Nurses write and tick plans in the caregiver app (caregiver guide, section 16). The **Clinical Reviewer** (and the Super Admin) can do the same from here, and also **sign off the five seeded templates**. There are **no console screens for this in v1**: the Clinical Reviewer authors through this API directly, so this section is the contract for whoever builds the screens later.

**Captured by the backend test harness (the real app and a real Postgres), not the compose stack**: captures `500` to `513` come from `app/tests/test_capture_care_plans_admin.py`. The data is placeholder. **[seeded]** The admins (a Clinical Reviewer, a Support and a Finance admin), the patients, the relative and the nurse's visit are inserted straight into the database; the nurse's note (capture 509-b) and the caregiver's view of the plan (510) go through the real caregiver routes. The admin tokens are real session tokens issued by the harness (the sign-in itself is section 3). **[not live]** Nothing here was run against the compose stack or a browser.

| Route | Permission | Notes |
|---|---|---|
| `GET /admin/care-plans` | `careplans.read` | filters `status` (`active`, `completed`, `archived`), `q` (client name only); `limit` (1 to 200, default 50), `offset`; newest change first. `{items, total, limit, offset}` |
| `POST /admin/care-plans` | `careplans.write` | create. **`201`** with the full plan |
| `GET /admin/care-plans/{plan_id}` | `careplans.read` | the full plan at any status. **Audited** |
| `PATCH /admin/care-plans/{plan_id}` | `careplans.write` | header, allergies, instructions, `status` to `completed` or `archived`. Needs `expected_version` |
| `GET /admin/care-plans/{plan_id}/observations` | `careplans.read` | the notes, newest first, keyset paged. **Audited** |
| `POST /admin/care-plans/{plan_id}/observations` | `careplans.write` | add a note. **`201`** |
| `POST /admin/care-plans/{plan_id}/goals`, `PATCH /admin/care-plan-goals/{goal_id}`, `DELETE /admin/care-plan-goals/{goal_id}?expected_version=` | `careplans.write` | `{goal, plan_version}` |
| `POST /admin/care-plans/{plan_id}/tasks`, `PATCH /admin/care-plan-tasks/{task_id}`, `DELETE /admin/care-plan-tasks/{task_id}?expected_version=` | `careplans.write` | `{task, plan_version}` |
| `POST /admin/care-plans/{plan_id}/medications`, `PATCH /admin/care-plan-medications/{medication_id}`, `POST /admin/care-plan-medications/{medication_id}/stop` | `careplans.write` | `{medication, plan_version}` |
| `GET /admin/care-plan-templates` | `careplans.read` | every template with content and review state |
| `POST /admin/care-plan-templates/{template_id}/review` | `careplans.templates` | sign off or take back |

Who holds the permissions: the **Clinical Reviewer** and the **Super Admin** hold all three; **no other role holds any** (Support gets `403`, captures 500-c, 501-f, 509-c, 511-f). There is no role with read but not write.

**The write bodies are the caregiver ones** (caregiver guide, sections 16.5 to 16.24), unchanged: the same fields, limits (30 goals, 60 tasks, 40 active medications), `expected_version` rules and `409` shapes. Only create differs, and the responses (below). The medications **record-only notice** of caregiver guide 16.21 applies to any screen that shows medications.

### The admin plan is not the caregiver plan (field-nesting traps)

| | Caregiver `GET /caregiver/care-plans/{id}` | Admin `GET /admin/care-plans/{id}` |
|---|---|---|
| `booking_id` | the visit that gives access | **absent** (no visit) |
| `capabilities` | `can_edit_plan`, `can_tick_tasks`, `can_add_observation` | **absent**: gate on the admin's `permissions` from `/admin/me` instead |
| `subject` | absent | `{kind, id}`: `kind` is `patient` or `recipient`; `id` is the `patients` or `care_recipients` id |
| `author.kind` | `caregiver` or `admin` | the same |
| `client` | `{full_name, age, gender}` | the same, **full name, not masked** (see UX consequences) |

Everything else (`diagnosis`, `risk_level`, `goals`, `medications`, `tasks`, `allergies`, `special_instructions`, `template`, `version`, dates) has the same name, type and nesting in both. The `plan` inside a `409` `version_conflict` is the **admin** shape on these routes.

### Creating a plan

Body: the caregiver create body (`diagnosis` and, unless a template supplies it, `risk_level`; optional `template_id`, `start_date`, `duration_days`, `review_every_days`, `goals`, `tasks`, `allergies`, `special_instructions`) plus **exactly one** of `patient_id` or `care_recipient_id`. Which one: a client who booked for themselves, or an app patient, is a `patient_id`; a relative that someone else booked visits for (WhatsApp) is a `care_recipient_id`. The list returns `subject` for every plan, so the id can be read back. There is **no client search route** in this slice: the console must get the id from the booking screens (section 12) or a person record.

<!-- capture: 501-a-create.json request.body -->
```json
{
  "patient_id": "5f880aa7-5ce0-42c6-9211-2cf9618b744d",
  "diagnosis": "Placeholder diagnosis",
  "risk_level": "high",
  "start_date": "2026-10-28",
  "duration_days": 30,
  "review_every_days": 14,
  "goals": [
    {
      "title": "Placeholder goal",
      "target": "Placeholder target",
      "frequency": "daily"
    }
  ],
  "tasks": [
    {
      "title": "Placeholder task",
      "time_slot": "morning",
      "time_of_day": "08:00",
      "days_of_week": [
        1,
        3,
        5
      ]
    }
  ],
  "allergies": [
    "Placeholder allergen"
  ],
  "special_instructions": [
    "Placeholder instruction"
  ]
}
```
<!-- capture: 501-a-create.json response.body -->
```json
{
  "id": "4de89fc7-ff0c-46ac-8256-c862607dc8ec",
  "version": 1,
  "status": "active",
  "subject": {
    "kind": "patient",
    "id": "5f880aa7-5ce0-42c6-9211-2cf9618b744d"
  },
  "client": {
    "full_name": "Placeholder Client One",
    "age": 71,
    "gender": "female"
  },
  "diagnosis": "Placeholder diagnosis",
  "risk_level": "high",
  "start_date": "2026-10-28",
  "duration_days": 30,
  "review_every_days": 14,
  "review_due_on": "2026-11-11",
  "template": null,
  "author": {
    "name": "Placeholder Reviewer",
    "kind": "admin"
  },
  "allergies": [
    "Placeholder allergen"
  ],
  "special_instructions": [
    "Placeholder instruction"
  ],
  "goals": [
    {
      "id": "a0d08167-194a-4380-955a-8fbf5664fa35",
      "title": "Placeholder goal",
      "target": "Placeholder target",
      "frequency": "daily",
      "status": "active"
    }
  ],
  "medications": [],
  "tasks": [
    {
      "id": "6f0f6818-7d39-45ac-8c46-f37009a1787d",
      "title": "Placeholder task",
      "time_slot": "morning",
      "time_of_day": "08:00",
      "days_of_week": [
        1,
        3,
        5
      ]
    }
  ],
  "created_at": "2026-10-28T16:46:29.542389",
  "updated_at": "2026-10-28T16:46:29.542394"
}
```

From a template, for a relative (the template fills what the body leaves out; trimmed here):

<!-- capture: 502-a-create-from-template-for-a-recipient.json response.body keys=id,version,status,subject,client,risk_level,duration_days,review_every_days,template,author -->
```json
{
  "id": "3bf9e07a-f1e8-484e-a3af-766c3d2f9a7b",
  "version": 1,
  "status": "active",
  "subject": {
    "kind": "recipient",
    "id": "9667032b-28ba-4ab3-934c-15841580d652"
  },
  "client": {
    "full_name": "Placeholder Relative",
    "age": 76,
    "gender": "male"
  },
  "risk_level": "high",
  "duration_days": 30,
  "review_every_days": 14,
  "template": {
    "id": "9c04f684-dc4d-401c-abc1-fac28a708df3",
    "name": "Stroke Recovery",
    "version": 1
  },
  "author": {
    "name": "Placeholder Reviewer",
    "kind": "admin"
  }
}
```

A client with an active plan already: `409`, with the existing plan's id so the console can open it:

<!-- capture: 501-b-create-duplicate.json response.body -->
```json
{
  "detail": {
    "code": "care_plan_exists",
    "message": "This client already has an active care plan.",
    "plan_id": "4de89fc7-ff0c-46ac-8256-c862607dc8ec"
  }
}
```

No such client (also an erased patient, or the recipient row that stands for an erased person), and no such template:

<!-- capture: 501-e-create-unknown-client.json response.body -->
```json
{
  "detail": {
    "code": "client_not_found",
    "message": "No such client."
  }
}
```
<!-- capture: 502-b-create-unknown-template.json response.body -->
```json
{
  "detail": {
    "code": "template_not_found",
    "message": "Care plan template not found."
  }
}
```

Both ids, or neither, is a `422` whose entry names the rule and **never echoes a value** (health text can be in the same body):

<!-- capture: 501-c-create-both-ids.json response.body -->
```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": [
        "body"
      ],
      "msg": "Value error, send exactly one of patient_id or care_recipient_id",
      "ctx": {
        "error": {}
      }
    }
  ]
}
```

### Reading: the list, the plan, the notes

<!-- capture: 503-a-list.json response.body -->
```json
{
  "items": [
    {
      "id": "3bf9e07a-f1e8-484e-a3af-766c3d2f9a7b",
      "version": 1,
      "status": "active",
      "subject": {
        "kind": "recipient",
        "id": "9667032b-28ba-4ab3-934c-15841580d652"
      },
      "client": {
        "full_name": "Placeholder Relative",
        "age": 76,
        "gender": "male"
      },
      "risk_level": "high",
      "start_date": "2026-10-28",
      "duration_days": 30,
      "author": {
        "name": "Placeholder Reviewer",
        "kind": "admin"
      },
      "goal_count": 4,
      "task_count": 8,
      "medication_count": 0,
      "updated_at": "2026-10-28T16:46:29.628468"
    },
    {
      "id": "4de89fc7-ff0c-46ac-8256-c862607dc8ec",
      "version": 1,
      "status": "active",
      "subject": {
        "kind": "patient",
        "id": "5f880aa7-5ce0-42c6-9211-2cf9618b744d"
      },
      "client": {
        "full_name": "Placeholder Client One",
        "age": 71,
        "gender": "female"
      },
      "risk_level": "high",
      "start_date": "2026-10-28",
      "duration_days": 30,
      "author": {
        "name": "Placeholder Reviewer",
        "kind": "admin"
      },
      "goal_count": 1,
      "task_count": 1,
      "medication_count": 0,
      "updated_at": "2026-10-28T16:46:29.542394"
    }
  ],
  "total": 2,
  "limit": 50,
  "offset": 0
}
```

**Rows carry no plan text** (no diagnosis, goals or allergies): only who, status, risk, counts and dates. `goal_count` excludes discontinued goals; `task_count` and `medication_count` count active ones. `q` matches the client's name, case-insensitively, as plain text (`%` and `_` are not wildcards). The diagnosis is encrypted, so it cannot be searched.

The full plan:

<!-- capture: 504-a-detail.json response.body -->
```json
{
  "id": "4de89fc7-ff0c-46ac-8256-c862607dc8ec",
  "version": 1,
  "status": "active",
  "subject": {
    "kind": "patient",
    "id": "5f880aa7-5ce0-42c6-9211-2cf9618b744d"
  },
  "client": {
    "full_name": "Placeholder Client One",
    "age": 71,
    "gender": "female"
  },
  "diagnosis": "Placeholder diagnosis",
  "risk_level": "high",
  "start_date": "2026-10-28",
  "duration_days": 30,
  "review_every_days": 14,
  "review_due_on": "2026-11-11",
  "template": null,
  "author": {
    "name": "Placeholder Reviewer",
    "kind": "admin"
  },
  "allergies": [
    "Placeholder allergen"
  ],
  "special_instructions": [
    "Placeholder instruction"
  ],
  "goals": [
    {
      "id": "a0d08167-194a-4380-955a-8fbf5664fa35",
      "title": "Placeholder goal",
      "target": "Placeholder target",
      "frequency": "daily",
      "status": "active"
    }
  ],
  "medications": [],
  "tasks": [
    {
      "id": "6f0f6818-7d39-45ac-8c46-f37009a1787d",
      "title": "Placeholder task",
      "time_slot": "morning",
      "time_of_day": "08:00",
      "days_of_week": [
        1,
        3,
        5
      ]
    }
  ],
  "created_at": "2026-10-28T16:46:29.542389",
  "updated_at": "2026-10-28T16:46:29.542394"
}
```

A plan that does not exist is **one `404` with the same body on every route in this section**, including the goal, task and medication routes:

<!-- capture: 504-b-detail-unknown.json response.body -->
```json
{
  "detail": "Care plan not found"
}
```

The notes, newest first, from nurses and admins (`author.kind`). Paging is the caregiver one (caregiver guide 16.17): pass `next_before` and `next_before_id` back as `before` and `before_id`; `limit` 1 to 50, default 20; `category` filters.

<!-- capture: 509-b-notes-list.json response.body -->
```json
{
  "items": [
    {
      "id": "8bb60d2b-a80e-4a45-8ade-280f72418fe9",
      "category": "observation",
      "body": "Placeholder visit note",
      "author": {
        "name": "Placeholder Nurse",
        "kind": "caregiver"
      },
      "created_at": "2026-10-28T16:46:30.445388"
    },
    {
      "id": "6d066d08-403c-4af5-bfcf-348bcfc6ebaf",
      "category": "special_note",
      "body": "Placeholder review note",
      "author": {
        "name": "Placeholder Reviewer",
        "kind": "admin"
      },
      "created_at": "2026-10-28T16:46:30.128038"
    }
  ],
  "next_before": null,
  "next_before_id": null
}
```

### Editing

Every write bumps the plan's `version` by one (notes do not) and returns `plan_version`; send it as the next `expected_version`. A stale `expected_version` is `409` `version_conflict` and the body holds the **current plan** so the console can reload without another call.

<!-- capture: 505-a-patch.json request.body -->
```json
{
  "expected_version": 1,
  "risk_level": "medium",
  "allergies": [
    "Placeholder allergen",
    "Placeholder allergen two"
  ]
}
```
<!-- capture: 505-b-patch-stale.json response.body keys=detail -->
```json
{
  "detail": {
    "code": "version_conflict",
    "message": "Someone just changed this plan. Reload it and try again.",
    "plan": {
      "id": "4de89fc7-ff0c-46ac-8256-c862607dc8ec",
      "version": 2,
      "status": "active",
      "subject": {
        "kind": "patient",
        "id": "5f880aa7-5ce0-42c6-9211-2cf9618b744d"
      },
      "client": {
        "full_name": "Placeholder Client One",
        "age": 71,
        "gender": "female"
      },
      "diagnosis": "Placeholder diagnosis",
      "risk_level": "medium",
      "start_date": "2026-10-28",
      "duration_days": 30,
      "review_every_days": 14,
      "review_due_on": "2026-11-11",
      "template": null,
      "author": {
        "name": "Placeholder Reviewer",
        "kind": "admin"
      },
      "allergies": [
        "Placeholder allergen",
        "Placeholder allergen two"
      ],
      "special_instructions": [
        "Placeholder instruction"
      ],
      "goals": [
        {
          "id": "a0d08167-194a-4380-955a-8fbf5664fa35",
          "title": "Placeholder goal",
          "target": "Placeholder target",
          "frequency": "daily",
          "status": "active"
        }
      ],
      "medications": [],
      "tasks": [
        {
          "id": "6f0f6818-7d39-45ac-8c46-f37009a1787d",
          "title": "Placeholder task",
          "time_slot": "morning",
          "time_of_day": "08:00",
          "days_of_week": [
            1,
            3,
            5
          ]
        }
      ],
      "created_at": "2026-10-28T16:46:29.542389",
      "updated_at": "2026-10-28T16:46:29.774978"
    }
  }
}
```

The goal, task and medication writes (captures 506, 507 and 508) answer exactly as in the caregiver guide. One example each:

<!-- capture: 506-a-goal-add.json response.body -->
```json
{
  "goal": {
    "id": "b8baffd9-9d58-4012-a6a4-938e5c8e521b",
    "title": "Placeholder goal two",
    "target": null,
    "frequency": "weekly",
    "status": "active"
  },
  "plan_version": 3
}
```
<!-- capture: 507-c-task-remove.json response.body -->
```json
{
  "task": {
    "id": "484cafdb-7efe-49d6-b19e-10c7ae567e23",
    "title": "Placeholder task two",
    "time_slot": "night",
    "time_of_day": "18:00",
    "days_of_week": [
      1,
      2,
      3,
      4,
      5,
      6,
      7
    ]
  },
  "plan_version": 8
}
```
<!-- capture: 508-c-medication-stop.json response.body -->
```json
{
  "medication": {
    "id": "98742aab-a8af-44b3-ab75-264ff132f693",
    "name": "Placeholder medicine A",
    "strength": "500 mg",
    "dose_amount": "2",
    "dose_unit": "tablet",
    "route": "oral",
    "reason": "Placeholder reason",
    "prescribed_by": "Placeholder prescriber",
    "note": null,
    "frequency": "twice_daily",
    "times_of_day": [
      "08:00",
      "20:00"
    ],
    "start_date": "2026-10-28",
    "end_date": null,
    "status": "stopped",
    "stopped_at": "2026-10-28T16:46:30.082309"
  },
  "plan_version": 11
}
```

A removed task is the plain `404`; a stopped medication cannot be edited (`409` `medication_stopped`); a plan that is `completed` or `archived` refuses every write with `409` `plan_not_editable` but stays readable:

<!-- capture: 508-d-medication-edit-stopped.json response.body -->
```json
{
  "detail": {
    "code": "medication_stopped",
    "message": "This medication was stopped. Record a new one instead of editing it."
  }
}
```
<!-- capture: 512-b-write-on-completed.json response.body -->
```json
{
  "detail": {
    "code": "plan_not_editable",
    "message": "This care plan is completed or archived and can no longer be changed."
  }
}
```

Completing a plan (`PATCH` with `status: "completed"`) frees the client for a **new** active plan (capture 512-e, `201`). There is no way to reopen a plan.

An admin note needs no visit (the plan must be active) and shows `author.kind: "admin"`:

<!-- capture: 509-a-note-add.json response.body -->
```json
{
  "id": "6d066d08-403c-4af5-bfcf-348bcfc6ebaf",
  "category": "special_note",
  "body": "Placeholder review note",
  "author": {
    "name": "Placeholder Reviewer",
    "kind": "admin"
  },
  "created_at": "2026-10-28T16:46:30.128038"
}
```

The caregiver app shows the admin-authored plan and the admin's edits to the nurse on the client's visit (capture 510; `author.kind` is `admin` there too).

### Templates and the go-live gate

<!-- capture: 511-a-templates.json response.body path=items first=1 -->
```json
[
  {
    "id": "46007536-27d3-47a8-b5cf-b0e8e08bc2b1",
    "key": "cardiac_rehabilitation",
    "name": "Cardiac Rehabilitation",
    "summary": "Light activity, resting readings and low-salt meals while recovering from a heart event.",
    "duration_days": 45,
    "review_every_days": 14,
    "default_risk_level": "high",
    "clinical_review_status": "pending",
    "version": 1,
    "goals": [
      {
        "title": "Progressive light activity as cleared by the care team",
        "target": null,
        "frequency": "daily"
      },
      {
        "title": "Resting BP and pulse recorded",
        "target": null,
        "frequency": "daily"
      },
      {
        "title": "Low-salt, regular meals",
        "target": null,
        "frequency": "daily"
      },
      {
        "title": "Report chest symptoms early",
        "target": null,
        "frequency": "ongoing"
      }
    ],
    "tasks": [
      {
        "title": "Check and record BP and pulse",
        "time_slot": "morning",
        "time_of_day": "08:00",
        "days_of_week": [
          1,
          2,
          3,
          4,
          5,
          6,
          7
        ]
      },
      {
        "title": "Check and record BP and pulse",
        "time_slot": "evening",
        "time_of_day": "18:00",
        "days_of_week": [
          1,
          2,
          3,
          4,
          5,
          6,
          7
        ]
      },
      {
        "title": "Light activity as instructed",
        "time_slot": "afternoon",
        "time_of_day": "13:00",
        "days_of_week": [
          1,
          2,
          3,
          4,
          5,
          6,
          7
        ]
      },
      {
        "title": "Weigh and record weight",
        "time_slot": "morning",
        "time_of_day": "08:00",
        "days_of_week": [
          1,
          2,
          3,
          4,
          5,
          6,
          7
        ]
      },
      {
        "title": "Meal support",
        "time_slot": "morning",
        "time_of_day": "08:00",
        "days_of_week": [
          1,
          2,
          3,
          4,
          5,
          6,
          7
        ]
      },
      {
        "title": "Meal support",
        "time_slot": "afternoon",
        "time_of_day": "13:00",
        "days_of_week": [
          1,
          2,
          3,
          4,
          5,
          6,
          7
        ]
      },
      {
        "title": "Meal support",
        "time_slot": "evening",
        "time_of_day": "18:00",
        "days_of_week": [
          1,
          2,
          3,
          4,
          5,
          6,
          7
        ]
      }
    ],
    "special_instructions": [
      "Stop activity and call for help for chest pain, breathlessness or fainting",
      "Follow the cardiac team's activity limits"
    ],
    "is_active": true,
    "reviewed_by": null,
    "reviewed_at": null
  }
]
```

Every template, active or not (`is_active`), with its full goals, tasks and special instructions: a reviewer signs off what they have read. `clinical_review_status` is `pending` until someone approves; nurses see "Not yet clinically reviewed" while it is pending. **Go-live gate: every seeded template is reviewed or removed before real use.** Review:

<!-- capture: 511-b-review-approve.json request.body -->
```json
{
  "status": "approved"
}
```
<!-- capture: 511-b-review-approve.json response.body keys=id,key,name,clinical_review_status,version,is_active,reviewed_by,reviewed_at -->
```json
{
  "id": "9c04f684-dc4d-401c-abc1-fac28a708df3",
  "key": "stroke_recovery",
  "name": "Stroke Recovery",
  "clinical_review_status": "approved",
  "version": 1,
  "is_active": true,
  "reviewed_by": "Placeholder Reviewer",
  "reviewed_at": "2026-10-28T16:46:30.508277"
}
```

`status` is `approved` or `pending` (`pending` takes the sign-off back). **Every call is applied and audited, even when the status does not change**, and records the reviewer and the time again, so a second reviewer's sign-off is on record. There is no template edit or create in v1, so a sign-off never goes stale. An unknown id is `404` `template_not_found` (capture 511-e); any other status is `422`.

### Every error the console must handle

| Status | `detail` | When | Capture |
|---|---|---|---|
| `401` | `Not authenticated` | no admin token | 500-b |
| `403` | `Access denied. Missing permission: careplans.read` (or `careplans.write`, `careplans.templates`) | any role but the Clinical Reviewer and Super Admin | 500-c, 501-f, 511-f |
| `404` | `Care plan not found` (a plain string) | unknown plan, goal, task or medication id; a removed task | 504-b, 507-d |
| `404` | `{"code": "client_not_found", "message": "No such client."}` | create for an unknown or erased client (an inactive patient, or an erased person's own recipient row) | 501-e |
| `404` | `{"code": "template_not_found", "message": "Care plan template not found."}` | create from, or review of, an unknown template | 502-b, 511-e |
| `409` | `{"code": "care_plan_exists", ..., "plan_id"}` | the client already has an active plan | 501-b |
| `409` | `{"code": "version_conflict", ..., "plan"}` | stale `expected_version` | 505-b |
| `409` | `{"code": "plan_not_editable", ...}` | write on a completed or archived plan | 512-b |
| `409` | `{"code": "medication_stopped", ...}` | edit of a stopped medication | 508-d |
| `422` | list (`loc`, `msg`, `type`; no `input`) | bad field, both or neither client id, a bad `status` filter, `limit` over 200 | 501-c, 501-d, 503-c, 508-e, 511-d |
| `422` | `{"code": "invalid_care_plan", "message": ...}` | a limit (too many goals, tasks or active medications), end date before start, no `risk_level` and no template | Test only (`test_the_services_own_refusals_are_invalid_care_plan_422s`); not captured, the same codes as the caregiver guide |

`detail` is a **string** for the permission and plain-404 errors, an **object** `{code, message, ...}` for the care plan refusals, and a **list** for validation. Branch on the status, then on `detail.code` when `detail` is an object.

### The audit trail

Every write is an `admin_audit_log` row with `entity_type` `care_plan` and `entity_id` the plan id (`care_plan_template` and the template id for a review), written in the **same transaction** as the change: `care_plan.plan.create`, `.plan.update`, `.plan.status`, `.template.use`, `.goal.add|update|remove`, `.task.add|update|remove`, `.med.add|update|stop`, `.observation.add`, and `care_plan_template.review`. **Opening a plan (`care_plan.view`) and reading its notes (`care_plan.observations_view`) are audited too**; the list and the template list are not. **So is any response that shows plan text without a write behind it**: a `409 version_conflict` (its body is the current plan) and a write that changed nothing (an empty `PATCH`, removing an already removed goal or task, stopping a stopped medication) each leave a `care_plan.view` row whose `after.via` is `version_conflict` or `unchanged_write`. The rows hold **field names, ids and counts only, never plan text**, because Finance can read the audit log:

<!-- capture: 513-a-audit.json response.body path=items first=3 -->
```json
[
  {
    "id": "623021f7-bbf7-46d5-ae55-769cfc75a102",
    "admin_user_id": "aabd6152-acf0-4036-9978-1d42233bc271",
    "admin_email": "admin-f1f86177c6@example.com",
    "action": "care_plan.plan.create",
    "entity_type": "care_plan",
    "entity_id": "3c427c11-5817-41ee-99b4-ed4ad6457c1a",
    "before": null,
    "after": {
      "goals": 1,
      "tasks": 1
    },
    "reason": null,
    "ip": "testclient",
    "user_agent": "testclient",
    "created_at": "2026-10-28T16:46:30.667765"
  },
  {
    "id": "05cf6e46-0218-4213-b9d2-e0d0777d73d7",
    "admin_user_id": "aabd6152-acf0-4036-9978-1d42233bc271",
    "admin_email": "admin-f1f86177c6@example.com",
    "action": "care_plan.view",
    "entity_type": "care_plan",
    "entity_id": "4de89fc7-ff0c-46ac-8256-c862607dc8ec",
    "before": null,
    "after": {
      "fields": [
        "diagnosis",
        "allergies",
        "special_instructions",
        "goals",
        "medications",
        "tasks"
      ]
    },
    "reason": null,
    "ip": "testclient",
    "user_agent": "testclient",
    "created_at": "2026-10-28T16:46:30.632686"
  },
  {
    "id": "6147333c-28ac-4ee2-9e2d-7779a0065d08",
    "admin_user_id": "aabd6152-acf0-4036-9978-1d42233bc271",
    "admin_email": "admin-f1f86177c6@example.com",
    "action": "care_plan.plan.status",
    "entity_type": "care_plan",
    "entity_id": "4de89fc7-ff0c-46ac-8256-c862607dc8ec",
    "before": null,
    "after": {
      "from": "active",
      "to": "completed"
    },
    "reason": null,
    "ip": "testclient",
    "user_agent": "testclient",
    "created_at": "2026-10-28T16:46:30.595725"
  }
]
```

### UX consequences the console must surface

- **Opening a plan is an audited act, and so is a conflict.** A `409 version_conflict` shows the reviewer the current plan, so it leaves an audit row too; do not trigger writes with a guessed version just to read a plan.
- **Opening a plan is an audited act.** Do not pre-fetch plan details for every row of the list, and do not open plans on hover or to build a search index. The list is deliberately text-free so it can be browsed without audit rows; open a plan only when the reviewer does. Tell the reviewer that opening is recorded.
- **Client names are shown in full** on these routes (the Clinical Reviewer does not hold `people.pii`, and the booking screens mask names for them; care plans do not). Do not copy names into other places, and do not cache any care plan response: every response is `Cache-Control: no-store`, and so are the `404` and `409` errors.
- **A nurse may be editing the same plan.** Always send the `version` you loaded. On `version_conflict`, show the reviewer the current plan from the response and let them redo the change; never retry automatically.
- **Completing or archiving a plan is final.** It cannot be reopened; the client can then get a new plan. Confirm before sending.
- **A stopped medication and a removed goal or task are history, not deleted.** Stopped medications and discontinued goals stay in the plan; show them greyed, as the app does.
- **A template sign-off is a clinical statement.** Show who signed it and when (`reviewed_by`, `reviewed_at`), and confirm before approving. Taking an approval back (`pending`) is allowed and audited.

### Verification (care plans, B4)

| Behaviour | Status | Evidence |
|---|---|---|
| Create for a patient, for a care recipient, from a template; duplicate, both/no id, unknown client, unknown template | Verified live (test harness) | 501, 502 |
| List with `q`, `status`, paging; text-free rows; bad status `422` | Verified live (test harness) | 500-a, 503 |
| Open a plan; unknown id `404` | Verified live (test harness) | 504 |
| Patch, stale `409` with the current plan | Verified live (test harness) | 505 |
| Goal, task and medication add, edit, remove or stop; removed task `404`; stopped medication `409` | Verified live (test harness) | 506, 507, 508 |
| Notes: add, list; the caregiver sees the admin's plan | Verified live (test harness) | 509, 510 |
| Templates: list, approve, take back, bad status, unknown id | Verified live (test harness) | 511 |
| Complete; write on a completed plan `409`; new plan after completing | Verified live (test harness) | 512 |
| Audit rows for the writes and the opens; no plan text in them | Verified live (test harness) / Test only | 513; `test_admin_care_plans_audit.py` |
| A stale-version `409` and a no-change write are audited as reads (`after.via`) | Test only | `test_admin_care_plans_audit.py` (the `409` itself is capture 505-b) |
| `403` for Support on read, write and review; `401` without a token | Verified live (test harness) | 500-b, 500-c, 501-f, 509-c, 511-f |
| Every route against every role | Test only | `test_admin_rbac.py` |
| The audit row and the change roll back together | Test only | `test_admin_care_plans_audit.py` |
| `invalid_care_plan` (no risk level, end before start, 41st active medication) on the admin routes | Test only | `test_admin_care_plans.py` |
| Anything against the compose stack, a browser, or a real authenticator-signed sign-in | **[not live]** | not run |

## 12.14 Caregiver identity review (K5): `/api/v1/admin/caregivers`

A caregiver proves who they are in the app (an ID number, a selfie, a liveness check) through the identity provider (Dojah since 2026-10-30). The backend decides what the result means. **Most caregivers never reach you.** A person is needed when the result is doubtful (a face score in the grey band, a name or date of birth that differs from the profile, an identity already used on another account, three failed tries), and the system never rejects anybody by itself. This section is what the console shows and does for those cases. The caregiver's side is in the [caregiver guide](fe-integration-guide-caregiver.md).

**Nothing here has met the real provider.** The app, the routes, the database, the admin responses and the decision engine below are real, but the identity verifications were served by a fake that follows Dojah's documented shapes and the Timp/Weysure parsers. So the scores and reason codes are what the engine does with the fake's values. Two things can differ with the real provider and the console should not hide either: if it does not return the registry name or date of birth where the backend looks, **every pass lands in review with `identity_data_missing`**; and the real score scale is not confirmed (the thresholds, 85 for a pass and 70 as the floor, are placeholders). **Dojah's sandbox returns no face score** (only a pass flag), so for now `face_confidence` is `null` on every attempt and **Dojah's own pass flag stands in for the face check** (owner decision 2026-10-30): name and date of birth decide, and a score, if one ever arrives, is judged against the thresholds as described below. **Dojah gives no liveness score**, only a pass/fail flag, so `liveness_confidence` is `null` in every attempt; a failed liveness shows as the reason `liveness_failed`. There is no watch-list signal either (the `watchlisted` reason cannot occur for now). Images are **not stored** by Oakly. To see the person, an admin opens the Dojah dashboard and looks the run up by `provider_reference`.

| Route | Permission | What it does |
|---|---|---|
| `GET /admin/caregivers?identity=needs_review\|stuck\|failed` | `caregivers.read` | The queue, filtered by identity bucket; the response also carries two badge counts |
| `GET /admin/caregivers/{id}` | `caregivers.read` | The detail; its `identity` block now lists the attempts. An audited read, as before |
| `POST /admin/caregivers/{id}/identity-decision` | `caregivers.approve` | Verify or fail. Body unchanged. Now also closes the attempt that was waiting for review |
| `POST /admin/caregivers/{id}/identity-reset` | `caregivers.approve` | **New.** Give a failed caregiver, or one whose three attempts ran out, a fresh set of tries |

No new permission was added. `caregivers.read` is held by Operations, Verification, Finance, Support and the Clinical Reviewer (so all of them can see the identity block, including `duplicate_of_caregiver_id`); `caregivers.approve` only by Verification and the Super Admin. **Finance can read but not decide or reset.**

### The queue: a filter and two badges

`profile_status` still picks the tab (default `under_review`). `identity` narrows it further:

| `identity` | Caregivers listed |
|---|---|
| `needs_review` | Identity `needs_review`: a person has to look |
| `stuck` | Identity `pending` or `in_progress` with an attempt open for longer than the session lifetime (24 hours) plus one hour. The cron normally closes those within minutes, so this lists the ones it could not (a provider outage) |
| `failed` | Identity `failed`: an admin failed them; the only bucket where a reset is the usual next step |

<!-- capture: 600-a-queue-all.json response.body keys=total,limit,offset,needs_review_count,stuck_count -->
```json
{
  "total": 8,
  "limit": 25,
  "offset": 0,
  "needs_review_count": 4,
  "stuck_count": 1
}
```

`needs_review_count` and `stuck_count` are **per tab and independent of the `identity` filter**, so a badge on the "Identity needs review" chip always equals the `total` that chip returns on the same tab. They are present on every list response (also an empty one).

<!-- capture: 600-b-queue-needs-review.json response.body path=items first=1 -->
```json
[
  {
    "id": "fd225b4a-45ca-411e-8992-9bf4175ac17c",
    "full_name": "Placeholder Bola Adeyemi",
    "state": "Lagos",
    "specialization": "nurse",
    "years_of_experience": 5,
    "submitted_at": "2026-10-30T10:40:41.390562",
    "age_in_days": 0,
    "document_count": 0,
    "identity_verification_status": "needs_review",
    "background_check_status": "pending",
    "profile_status": "under_review"
  }
]
```

The rows are the same as before. The row's `identity_verification_status` is the caregiver's identity status; the reason is only on the detail.

<!-- capture: 600-e-queue-bad-filter.json response.body -->
```json
{
  "detail": [
    {
      "type": "literal_error",
      "loc": [
        "query",
        "identity"
      ],
      "msg": "Input should be 'needs_review', 'stuck' or 'failed'",
      "input": "everything",
      "ctx": {
        "expected": "'needs_review', 'stuck' or 'failed'"
      }
    }
  ]
}
```

### The identity block on the detail

`GET /admin/caregivers/{id}` keeps its shape except for `identity`, which is now:

<!-- capture: 601-a-detail-duplicate.json response.body path=identity -->
```json
{
  "status": "needs_review",
  "review_reason": "duplicate_identity",
  "attempts_used": 1,
  "attempts_max": 3,
  "can_reset": false,
  "attempts": [
    {
      "id": "d2db4b73-61d0-4db5-bcf2-7567bab3907b",
      "attempt_no": 1,
      "status": "needs_review",
      "created_at": "2026-10-30T10:40:41.395720",
      "started_at": null,
      "completed_at": "2026-10-30T10:40:41.405487",
      "id_type": "nin",
      "face_confidence": 92.0,
      "liveness_confidence": null,
      "face_threshold": 85,
      "name_match": true,
      "dob_match": true,
      "reason_codes": [
        "duplicate_identity"
      ],
      "provider_reference": "OKL-7c45be0a1311414f8852cfcde2f6119a",
      "decided_via": "provider"
    }
  ],
  "duplicate_of_caregiver_id": "d132b3d6-5c07-4220-b555-f1d1cf187a5e"
}
```

| Field | Meaning |
|---|---|
| `status` | The caregiver's identity status: `pending`, `in_progress`, `needs_review`, `verified`, `failed` |
| `review_reason` | **Only while `status` is `needs_review`**, otherwise `null`. The single most serious reason on the attempt waiting for review (order of seriousness: `watchlisted`, `duplicate_identity`, `provider_disagreement`, `identity_data_missing`, `attempts_exhausted`, `face_grey_band`, `name_mismatch`, `dob_mismatch`); the full list is on the attempt |
| `attempts_used`, `attempts_max` | What counts against the cap: expired and errored attempts do not count, and attempts before an admin reset do not count |
| `can_reset` | True when `status` is `failed`, or `needs_review` because the attempts ran out (`review_reason` `attempts_exhausted`); show "Allow another attempt" only then |
| `attempts` | Every attempt, **newest first** (`attempt_no` descending). An empty list, never an absent key, when nothing was started |
| `duplicate_of_caregiver_id` | Another caregiver whose attempt **passed** with the same ID number, or `null`. Always present. It stays after you decide, so keep showing it |

Each attempt:

| Field | Meaning |
|---|---|
| `status` | `created`, `started`, `awaiting_result` (open); `passed`, `failed`, `needs_review`, `expired`, `error` (finished). `expired` and `error` mean nothing was decided about the person |
| `face_confidence`, `liveness_confidence`, `face_threshold` | JSON **numbers** (not strings), `null` when not known. The face score is 0 to 100. `face_threshold` is the pass mark that was applied |
| `name_match`, `dob_match` | `true`, `false` or `null` (`null` = could not be compared). The name and date of birth themselves are **not** returned or stored |
| `id_type` | `nin`, `bvn`, `drivers_licence`, `voters_card`, `passport` or `null`. The ID number is never returned |
| `reason_codes` | Why the attempt ended as it did (words below). May also hold `admin_reset` (see the reset section) |
| `provider_reference` | The reference our server created for this check (`OKL-` followed by 32 hex characters) and gave to Dojah's widget, so the admin can look the run up in the Dojah dashboard. It is not a secret and not a number of the person's. Set when the attempt is created; `null` only for an attempt erased by an account deletion or seeded without one. **Not verified: that the Dojah dashboard searches by it (K8)** |
| `decided_via` | `provider` (the engine) or `admin`; `null` while open or when nothing was decided |
| `created_at`, `started_at`, `completed_at` | `started_at` is set when the widget reports it opened; `completed_at` when a result arrived or an admin decided |

**Reason codes, in plain words** (the codes are ours; show the words, keep the code in a tooltip). The list can grow, so show an unknown code as it is.

| Code | Meaning for the admin |
|---|---|
| `watchlisted` | The ID number is on a watch list. Do not let this caregiver retry with the same number |
| `duplicate_identity` | The same ID number already passed for another account (`duplicate_of_caregiver_id`) |
| `provider_disagreement` | The webhook and the provider's own record disagreed about the result. Look the person up in the provider dashboard |
| `identity_data_missing` | The provider said "pass" but did not return a name or date of birth the check needs (or a face score that was present but unusable, or no selfie result at all), or the caregiver's profile has no date of birth |
| `attempts_exhausted` | Three clean failures in a row |
| `face_grey_band` | The face score is between the floor and the pass mark |
| `name_mismatch`, `dob_mismatch` | The name or date of birth on the ID differs from the profile (a married name and a typo are the usual causes) |
| `face_mismatch`, `liveness_failed` | The selfie does not match the ID photo, or the liveness check failed (clean failure: the caregiver is told to try again) |
| `record_not_found`, `document_unreadable`, `provider_failed` | The ID was not found, could not be read, or the provider failed it without a clearer reason |
| `admin_decision`, `admin_reset` | Added by an admin action (below), never by the provider |

The same detail for a name mismatch (an `identity` block is the same shape in every case; only `identity` is shown):

<!-- capture: 601-b-detail-name-mismatch.json response.body path=identity -->
```json
{
  "status": "needs_review",
  "review_reason": "name_mismatch",
  "attempts_used": 1,
  "attempts_max": 3,
  "can_reset": false,
  "attempts": [
    {
      "id": "35ec66ba-3e97-46f3-ae72-15ce67c58cca",
      "attempt_no": 1,
      "status": "needs_review",
      "created_at": "2026-10-30T10:40:41.665341",
      "started_at": null,
      "completed_at": "2026-10-30T10:40:41.673081",
      "id_type": "bvn",
      "face_confidence": 92.0,
      "liveness_confidence": null,
      "face_threshold": 85,
      "name_match": false,
      "dob_match": true,
      "reason_codes": [
        "name_mismatch"
      ],
      "provider_reference": "OKL-b31e67c64e1f4300b6d60955b8ab73e8",
      "decided_via": "provider"
    }
  ],
  "duplicate_of_caregiver_id": null
}
```

Three clean failures end in review, not in a failed caregiver (the app shows "under review", `attempts_used` equals `attempts_max`):

<!-- capture: 601-f-detail-exhausted.json response.body path=identity -->
```json
{
  "status": "needs_review",
  "review_reason": "attempts_exhausted",
  "attempts_used": 3,
  "attempts_max": 3,
  "can_reset": true,
  "attempts": [
    {
      "id": "9942ebd2-455b-4021-b3f9-5b6401ffbaf7",
      "attempt_no": 3,
      "status": "needs_review",
      "created_at": "2026-10-30T10:40:42.490176",
      "started_at": null,
      "completed_at": "2026-10-30T10:40:42.496824",
      "id_type": "nin",
      "face_confidence": 20.0,
      "liveness_confidence": null,
      "face_threshold": 85,
      "name_match": null,
      "dob_match": null,
      "reason_codes": [
        "face_mismatch",
        "attempts_exhausted"
      ],
      "provider_reference": "OKL-1ebd9a80cc0642b185c9249e667235df",
      "decided_via": "provider"
    },
    {
      "id": "47f73f40-07fe-4c51-8fe1-6a415d5f1cbd",
      "attempt_no": 2,
      "status": "failed",
      "created_at": "2026-10-30T10:40:42.478018",
      "started_at": null,
      "completed_at": "2026-10-30T10:40:42.484374",
      "id_type": "nin",
      "face_confidence": 20.0,
      "liveness_confidence": null,
      "face_threshold": 85,
      "name_match": null,
      "dob_match": null,
      "reason_codes": [
        "face_mismatch"
      ],
      "provider_reference": "OKL-5290cfe9985949d8971f6a958746e0b6",
      "decided_via": "provider"
    },
    {
      "id": "0f12b41b-9ced-4a01-8ce4-416a4e72a74a",
      "attempt_no": 1,
      "status": "failed",
      "created_at": "2026-10-30T10:40:42.465070",
      "started_at": null,
      "completed_at": "2026-10-30T10:40:42.472825",
      "id_type": "nin",
      "face_confidence": 20.0,
      "liveness_confidence": null,
      "face_threshold": 85,
      "name_match": null,
      "dob_match": null,
      "reason_codes": [
        "face_mismatch"
      ],
      "provider_reference": "OKL-cb4533ba06d44fbfac0345e889041144",
      "decided_via": "provider"
    }
  ],
  "duplicate_of_caregiver_id": "d132b3d6-5c07-4220-b555-f1d1cf187a5e"
}
```

A caregiver who has not started:

<!-- capture: 601-g-detail-no-attempts.json response.body path=identity -->
```json
{
  "status": "pending",
  "review_reason": null,
  "attempts_used": 0,
  "attempts_max": 3,
  "can_reset": false,
  "attempts": [],
  "duplicate_of_caregiver_id": null
}
```

An attempt still open after 30 hours (seeded: the app cannot produce one without a provider outage):

<!-- capture: 601-h-detail-stuck.json response.body path=identity -->
```json
{
  "status": "in_progress",
  "review_reason": null,
  "attempts_used": 1,
  "attempts_max": 3,
  "can_reset": false,
  "attempts": [
    {
      "id": "25d4be22-37f5-42a4-84da-f1b7d8981f49",
      "attempt_no": 1,
      "status": "awaiting_result",
      "created_at": "2026-10-29T04:40:42.754993",
      "started_at": null,
      "completed_at": null,
      "id_type": null,
      "face_confidence": null,
      "liveness_confidence": null,
      "face_threshold": null,
      "name_match": null,
      "dob_match": null,
      "reason_codes": [],
      "provider_reference": null,
      "decided_via": null
    }
  ],
  "duplicate_of_caregiver_id": null
}
```

**Breaking change for the console.** `identity.document_verification_result`, `identity.face_verification_status` and `identity.face_verification_result` are **gone**. They had been `null` for every caregiver since the old verification provider was removed. Read `identity.attempts` instead. `identity.status` is unchanged.

### Deciding: `POST /admin/caregivers/{id}/identity-decision`

The body is `{"decision": "verified" | "failed", "notes": "..."}`; `notes` is required and is stored as the audit `reason`. It works on a caregiver in any identity state, as before. **New and optional: `expected_status`**, the `identity.status` the admin was looking at. **Always send it.** If the case moved in between (the provider decided it, or another admin did), nothing is decided and the answer is a 409 `IDENTITY_CASE_CHANGED`; without it a click can resolve a review the admin never saw. What is new is what it does to the attempts:

- The attempt **waiting for review** becomes `passed` (verify) or `failed` (fail), with `decided_via` `admin`. Its `reason_codes` are left as they were, so the detail still shows what the admin overrode.
- Any attempt still **open** is closed as `expired`, so a late provider result cannot settle it. (The provider can no longer change a caregiver an admin has decided in any case.)
- An append-only event row is written for each (the system's decision trail), and the usual `admin_audit_log` row, which now also records the attempt that was resolved.
- Failing a caregiver whose identity the provider had **passed** is an override: the passed attempt is left as it was, an event row is written on it, and the audit row names it as `overridden_verification_id`.
- Verifying moves a caregiver who was stuck on the identity step on to the next one; it never moves anyone back.

A role without `caregivers.approve`, here Finance:

<!-- capture: 602-a-decision-as-finance.json response.body -->
```json
{
  "detail": "Access denied. Missing permission: caregivers.approve"
}
```

<!-- capture: 602-b-decision-no-notes.json response.body -->
```json
{
  "detail": [
    {
      "type": "missing",
      "loc": [
        "body",
        "notes"
      ],
      "msg": "Field required",
      "input": {
        "decision": "verified"
      }
    }
  ]
}
```

<!-- capture: 602-c-decision-unknown-caregiver.json response.body -->
```json
{
  "detail": "Caregiver not found"
}
```

<!-- capture: 602-k-decision-case-changed.json response.body -->
```json
{
  "detail": {
    "message": "The identity case changed since you opened it. Reload and decide again",
    "unmet": [
      {
        "code": "IDENTITY_CASE_CHANGED",
        "message": "Identity is now needs_review"
      }
    ]
  }
}
```

Verify (the response is the same summary as before):

<!-- capture: 602-d-verify.json response.body -->
```json
{
  "id": "09c9f2dc-29e6-4caa-980f-6af168a93f0a",
  "profile_status": "under_review",
  "identity_verification_status": "verified",
  "background_check_status": "pending",
  "auto_suspended": false,
  "affected_bookings": []
}
```

and the detail afterwards: the attempt is `passed` by the `admin`, with the reason it was in review still on it, and `review_reason` is `null`:

<!-- capture: 602-e-detail-after-verify.json response.body path=identity -->
```json
{
  "status": "verified",
  "review_reason": null,
  "attempts_used": 1,
  "attempts_max": 3,
  "can_reset": false,
  "attempts": [
    {
      "id": "35ec66ba-3e97-46f3-ae72-15ce67c58cca",
      "attempt_no": 1,
      "status": "passed",
      "created_at": "2026-10-30T10:40:41.665341",
      "started_at": null,
      "completed_at": "2026-10-30T10:40:41.673081",
      "id_type": "bvn",
      "face_confidence": 92.0,
      "liveness_confidence": null,
      "face_threshold": 85,
      "name_match": false,
      "dob_match": true,
      "reason_codes": [
        "name_mismatch"
      ],
      "provider_reference": "OKL-b31e67c64e1f4300b6d60955b8ab73e8",
      "decided_via": "admin"
    }
  ],
  "duplicate_of_caregiver_id": null
}
```

Fail (`auto_suspended` as before: failing an approved caregiver suspends them):

<!-- capture: 602-f-fail.json response.body -->
```json
{
  "id": "fd225b4a-45ca-411e-8992-9bf4175ac17c",
  "profile_status": "under_review",
  "identity_verification_status": "failed",
  "background_check_status": "pending",
  "auto_suspended": false,
  "affected_bookings": []
}
```

<!-- capture: 602-g-detail-after-fail.json response.body path=identity -->
```json
{
  "status": "failed",
  "review_reason": null,
  "attempts_used": 1,
  "attempts_max": 3,
  "can_reset": true,
  "attempts": [
    {
      "id": "d2db4b73-61d0-4db5-bcf2-7567bab3907b",
      "attempt_no": 1,
      "status": "failed",
      "created_at": "2026-10-30T10:40:41.395720",
      "started_at": null,
      "completed_at": "2026-10-30T10:40:41.405487",
      "id_type": "nin",
      "face_confidence": 92.0,
      "liveness_confidence": null,
      "face_threshold": 85,
      "name_match": true,
      "dob_match": true,
      "reason_codes": [
        "duplicate_identity"
      ],
      "provider_reference": "OKL-7c45be0a1311414f8852cfcde2f6119a",
      "decided_via": "admin"
    }
  ],
  "duplicate_of_caregiver_id": "d132b3d6-5c07-4220-b555-f1d1cf187a5e"
}
```

Deciding for a caregiver who still has an open attempt closes it (seeded: the stuck caregiver):

<!-- capture: 602-i-detail-after-open-attempt.json response.body path=identity -->
```json
{
  "status": "verified",
  "review_reason": null,
  "attempts_used": 0,
  "attempts_max": 3,
  "can_reset": false,
  "attempts": [
    {
      "id": "25d4be22-37f5-42a4-84da-f1b7d8981f49",
      "attempt_no": 1,
      "status": "expired",
      "created_at": "2026-10-29T04:40:42.754993",
      "started_at": null,
      "completed_at": null,
      "id_type": null,
      "face_confidence": null,
      "liveness_confidence": null,
      "face_threshold": null,
      "name_match": null,
      "dob_match": null,
      "reason_codes": [],
      "provider_reference": null,
      "decided_via": null
    }
  ],
  "duplicate_of_caregiver_id": null
}
```

The new bucket after that failure:

<!-- capture: 602-j-queue-failed.json response.body keys=total,needs_review_count,stuck_count -->
```json
{
  "total": 1,
  "needs_review_count": 2,
  "stuck_count": 0
}
```

### Allowing another attempt: `POST /admin/caregivers/{id}/identity-reset`

For a caregiver the system could not verify: `failed` (an admin failed them) or `needs_review` **because the three attempts ran out** (`review_reason` `attempts_exhausted`). A review about the evidence (watch list, duplicate, mismatch, grey-band face) needs a verdict, not another go: fail the caregiver first if that is the verdict, then reset. Body `{"notes": "..."}` (required). It sets the identity status back to `pending` and gives a **fresh set of attempts**.

- Nothing is verified, approved or reinstated. A suspended caregiver stays suspended; approval still needs a verified identity.
- The old attempts stay as history. Each gets `admin_reset` added to its `reason_codes` and stops counting, which is how `attempts_used` goes back to 0. **Show `admin_reset` as "before reset", not as a failure reason.** The attempt numbers carry on (the next attempt is number 4 after three), so numbers are not a count.
- An attempt still open is closed. The caregiver's `profile_completion_step` is left as it is.
- From any other state the answer is a 409 with a code. If a **verified** caregiver's verification must be undone, fail them first (an audited decision), then reset.

Not allowed for Finance, and a missing `notes`:

<!-- capture: 603-a-reset-as-finance.json response.body -->
```json
{
  "detail": "Access denied. Missing permission: caregivers.approve"
}
```

<!-- capture: 603-b-reset-no-notes.json response.body -->
```json
{
  "detail": [
    {
      "type": "missing",
      "loc": [
        "body",
        "notes"
      ],
      "msg": "Field required",
      "input": {}
    }
  ]
}
```

The 409 (the `unmet[0].message` names the current status; show the `message`, switch on the `code`). A doubtful review gets the same answer:

<!-- capture: 603-c-reset-verified.json response.body -->
```json
{
  "detail": {
    "message": "Identity can only be reset when it has failed or its attempts ran out",
    "unmet": [
      {
        "code": "IDENTITY_RESET_NOT_ALLOWED",
        "message": "Identity is verified"
      }
    ]
  }
}
```

<!-- capture: 603-k-reset-doubtful-review.json response.body -->
```json
{
  "detail": {
    "message": "Identity can only be reset when it has failed or its attempts ran out",
    "unmet": [
      {
        "code": "IDENTITY_RESET_NOT_ALLOWED",
        "message": "Identity is needs_review"
      }
    ]
  }
}
```

What the caregiver's app shows **before** (three failures, in review, nothing to tap) and after:

<!-- capture: 603-e-app-state-before-reset.json response.body -->
```json
{
  "status": "needs_review",
  "attempts_used": 3,
  "attempts_max": 3,
  "active_verification_id": null,
  "last_attempt_status": "needs_review",
  "can_start": false,
  "failure_hint": null,
  "provider_available": true,
  "review_eta_hours": 24,
  "policy_version": "kyc-2026-10"
}
```

The reset:

<!-- capture: 603-f-reset.json response.body -->
```json
{
  "id": "b0f156a5-a092-417a-896b-1c58efcaddba",
  "profile_status": "under_review",
  "identity_verification_status": "pending",
  "background_check_status": "pending",
  "attempts_used": 0,
  "attempts_max": 3
}
```

The response is the same summary plus `attempts_used` (always 0 here) and `attempts_max`. The detail afterwards: every attempt carries `admin_reset`, `attempts_used` is 0, `can_reset` is false:

<!-- capture: 603-g-detail-after-reset.json response.body path=identity -->
```json
{
  "status": "pending",
  "review_reason": null,
  "attempts_used": 0,
  "attempts_max": 3,
  "can_reset": false,
  "attempts": [
    {
      "id": "9942ebd2-455b-4021-b3f9-5b6401ffbaf7",
      "attempt_no": 3,
      "status": "needs_review",
      "created_at": "2026-10-30T10:40:42.490176",
      "started_at": null,
      "completed_at": "2026-10-30T10:40:42.496824",
      "id_type": "nin",
      "face_confidence": 20.0,
      "liveness_confidence": null,
      "face_threshold": 85,
      "name_match": null,
      "dob_match": null,
      "reason_codes": [
        "face_mismatch",
        "attempts_exhausted",
        "admin_reset"
      ],
      "provider_reference": "OKL-1ebd9a80cc0642b185c9249e667235df",
      "decided_via": "provider"
    },
    {
      "id": "47f73f40-07fe-4c51-8fe1-6a415d5f1cbd",
      "attempt_no": 2,
      "status": "failed",
      "created_at": "2026-10-30T10:40:42.478018",
      "started_at": null,
      "completed_at": "2026-10-30T10:40:42.484374",
      "id_type": "nin",
      "face_confidence": 20.0,
      "liveness_confidence": null,
      "face_threshold": 85,
      "name_match": null,
      "dob_match": null,
      "reason_codes": [
        "face_mismatch",
        "admin_reset"
      ],
      "provider_reference": "OKL-5290cfe9985949d8971f6a958746e0b6",
      "decided_via": "provider"
    },
    {
      "id": "0f12b41b-9ced-4a01-8ce4-416a4e72a74a",
      "attempt_no": 1,
      "status": "failed",
      "created_at": "2026-10-30T10:40:42.465070",
      "started_at": null,
      "completed_at": "2026-10-30T10:40:42.472825",
      "id_type": "nin",
      "face_confidence": 20.0,
      "liveness_confidence": null,
      "face_threshold": 85,
      "name_match": null,
      "dob_match": null,
      "reason_codes": [
        "face_mismatch",
        "admin_reset"
      ],
      "provider_reference": "OKL-cb4533ba06d44fbfac0345e889041144",
      "decided_via": "provider"
    }
  ],
  "duplicate_of_caregiver_id": "d132b3d6-5c07-4220-b555-f1d1cf187a5e"
}
```

The caregiver's app now (`can_start` is true):

<!-- capture: 603-h-app-state-after-reset.json response.body -->
```json
{
  "status": "pending",
  "attempts_used": 0,
  "attempts_max": 3,
  "active_verification_id": null,
  "last_attempt_status": null,
  "can_start": true,
  "failure_hint": null,
  "provider_available": true,
  "review_eta_hours": null,
  "policy_version": "kyc-2026-10"
}
```

and the next attempt carries on the numbering:

<!-- capture: 603-i-app-starts-new-attempt.json response.body path=attempt_no -->
```json
4
```

### The audit trail

Both actions write `admin_audit_log` rows (`caregiver.identity_decision`, `caregiver.identity_reset`; `reason` is the notes; `after` lists the attempts resolved or wiped), and so does opening a caregiver (`caregiver.view`). They are in `GET /admin/audit?entity_type=caregiver` for the Super Admin and Finance:

<!-- capture: 604-a-audit-identity-actions.json response.body path=items first=3 -->
```json
[
  {
    "id": "82eec944-5366-4e45-b33b-68a5fe3da864",
    "admin_user_id": "f9db366c-f263-4a68-ab09-d53f9fdf6f0b",
    "admin_email": "admin-de502e3f9c@example.com",
    "action": "caregiver.identity_reset",
    "entity_type": "caregiver",
    "entity_id": "fd225b4a-45ca-411e-8992-9bf4175ac17c",
    "before": {
      "profile_status": "under_review",
      "identity_verification_status": "failed",
      "background_check_status": "pending",
      "approved_by": null,
      "approved_at": null,
      "suspended_at": null,
      "suspension_reason": null,
      "rejection_reason": null,
      "changes_requested": null
    },
    "after": {
      "profile_status": "under_review",
      "identity_verification_status": "pending",
      "background_check_status": "pending",
      "approved_by": null,
      "approved_at": null,
      "suspended_at": null,
      "suspension_reason": null,
      "rejection_reason": null,
      "changes_requested": null,
      "attempts_reset": [
        "d2db4b73-61d0-4db5-bcf2-7567bab3907b"
      ]
    },
    "reason": "Placeholder: second account closed, allow the first.",
    "ip": "testclient",
    "user_agent": "testclient",
    "created_at": "2026-10-30T10:40:43.487745"
  },
  {
    "id": "7eca3322-db53-40dd-8d55-90e2f7fe0263",
    "admin_user_id": "f9db366c-f263-4a68-ab09-d53f9fdf6f0b",
    "admin_email": "admin-de502e3f9c@example.com",
    "action": "caregiver.view",
    "entity_type": "caregiver",
    "entity_id": "b0f156a5-a092-417a-896b-1c58efcaddba",
    "before": null,
    "after": null,
    "reason": null,
    "ip": "testclient",
    "user_agent": "testclient",
    "created_at": "2026-10-30T10:40:43.447118"
  },
  {
    "id": "e2a642f1-21bd-42c2-809e-d1251fe07f3d",
    "admin_user_id": "f9db366c-f263-4a68-ab09-d53f9fdf6f0b",
    "admin_email": "admin-de502e3f9c@example.com",
    "action": "caregiver.identity_reset",
    "entity_type": "caregiver",
    "entity_id": "b0f156a5-a092-417a-896b-1c58efcaddba",
    "before": {
      "profile_status": "under_review",
      "identity_verification_status": "needs_review",
      "background_check_status": "pending",
      "approved_by": null,
      "approved_at": null,
      "suspended_at": null,
      "suspension_reason": null,
      "rejection_reason": null,
      "changes_requested": null
    },
    "after": {
      "profile_status": "under_review",
      "identity_verification_status": "pending",
      "background_check_status": "pending",
      "approved_by": null,
      "approved_at": null,
      "suspended_at": null,
      "suspension_reason": null,
      "rejection_reason": null,
      "changes_requested": null,
      "attempts_reset": [
        "0f12b41b-9ced-4a01-8ce4-416a4e72a74a",
        "47f73f40-07fe-4c51-8fe1-6a415d5f1cbd",
        "9942ebd2-455b-4021-b3f9-5b6401ffbaf7"
      ]
    },
    "reason": "Placeholder: photos were dark, allow another go.",
    "ip": "testclient",
    "user_agent": "testclient",
    "created_at": "2026-10-30T10:40:43.426144"
  }
]
```

### Every error the console must handle

| Where | Status | Body | What to do |
|---|---|---|---|
| any | `401` | `{"detail": "Not authenticated"}` | Refresh once, then sign in again |
| any | `403` | `{"detail": "Access denied. Missing permission: caregivers.approve"}` (or `.read`) | No-access message; keep the session; hide the buttons for admins whose `/admin/me` lacks the permission |
| list | `422` | FastAPI validation list (`loc` `["query","identity"]`) | A bug in the console: the bucket name is wrong |
| decision, reset | `404` | `{"detail": "Caregiver not found"}` | The row was removed; reload the queue |
| decision, reset | `422` | FastAPI validation list (`loc` `["body","notes"]`) | `notes` missing or blank; require it before enabling the button |
| decision | `409` | `{"detail": {"message": "...", "unmet": [{"code": "IDENTITY_CASE_CHANGED", "message": "..."}]}}` | The case moved since the admin opened it; reload the detail and let them decide again |
| reset | `409` | `{"detail": {"message": "...", "unmet": [{"code": "IDENTITY_RESET_NOT_ALLOWED", "message": "..."}]}}` | Someone else already acted, or the case needs a verdict; reload the detail (`can_reset` shows the truth) |

### UX consequences the console must surface

- **A decision overrides the system, and is permanent in the history.** Ask for the notes in the admin's own words and say that they are recorded with the admin's name. Verify and Fail both need a confirmation.
- **Failing an approved caregiver suspends them** (`auto_suspended` in the response), exactly as before. Say so on the confirmation.
- **Reset is not a verification.** The button is "Allow another attempt", never "Verify". The caregiver gets a fresh set of tries and is asked to do the check again; they are not notified by this call (the app picks it up on its next poll).
- **Never show or ask for an ID number.** None is stored and none is returned. To look at the person, send the admin to the provider's dashboard with `provider_reference`; the console must not try to render images.
- **`duplicate_of_caregiver_id` is for admins only.** Link it to the other caregiver's detail (which is itself an audited read). The caregiver is never told a duplicate exists; do not put it in anything the caregiver can see.
- **Opening a caregiver is audited**, including this block. Do not pre-fetch details for every row.
- **The badge can lag by a poll.** Counts are read when the list is loaded; reload after a decision or reset.
- **Both counts are per tab.** The badge on a chip must come from the response of the tab the admin is on, not from another tab's response.

### Verification (identity review, K5)

| Behaviour | Status | Evidence |
|---|---|---|
| Queue filter `needs_review`, `stuck`, `failed`, the two counts, unknown bucket `422`, no `caregivers.read` `403`, no token `401` | Verified live (test harness) | 600-a to 600-g, 602-j |
| The identity block for a duplicate, name mismatch, grey band, pass, one failure, three failures, no attempts, a stuck attempt | Verified live (test harness); attempts are decided by the real engine over a **fake provider** (a stuck attempt is seeded) | 601-a to 601-h |
| Verify and fail close the attempt waiting for review and any open attempt; `403` for Finance, `422`, `404` | Verified live (test harness) | 602-a to 602-i |
| Reset from an exhausted `needs_review` and from `failed`; `409` from `verified`, `in_progress` and a doubtful review; `403` for Finance; `422` | Verified live (test harness) | 603-a to 603-d, 603-f, 603-j, 603-k |
| A decision with a stale `expected_status` is refused | Verified live (test harness) | 602-k |
| The caregiver app after a reset: `attempts_used` 0, `can_start` true, next attempt number 4 | Verified live (test harness) | 603-e, 603-h, 603-i |
| Audit rows for decision, reset and view | Verified live (test harness) | 604-a |
| Every route against every role | Test only | `test_admin_rbac.py` |
| A late provider result cannot move a caregiver an admin decided; two resets at once have one winner | Test only | `test_kyc_admin_api.py` |
| Real provider data: the fields the identity block shows (scores, name and date-of-birth match, ID type, `provider_reference`, reason codes) | **[not live]** | Nothing has met the real provider; the fake follows the documented shapes (design section 15). The first live pass (K8) will show whether they are filled |
| A browser, the compose stack, a real authenticator-signed sign-in | **[not live]** | Not run; the calls above used the test client with a valid admin token |

## 13. Pricing settings: `/api/v1/booking-settings`

The price list the app and the WhatsApp flow quote from: base fee per hour, platform fee, per-service multipliers, surge and discount. **Money is in naira here**, as before.

**Versions, not edits.** Every save publishes a **new row with a new `id`**. Old rows are never changed, because quotes and bookings point at the version they were priced with. The newest row is the live price list. So after a `PUT`, the `id` you edited is no longer the one in use; use the `id` from the response.

| Route | Permission | What it does |
|---|---|---|
| `GET /booking-settings/latest` | `bookings.read` | the live version |
| `GET /booking-settings/` | `bookings.read` | every version, newest first (the history) |
| `POST /booking-settings/` | `pricing.write` | create the **first** version only; `409` once any version exists |
| `PUT /booking-settings/{id}` | `pricing.write` | publish a new version that replaces `{id}`; `{id}` must still be the latest |
| `DELETE /booking-settings/{id}` | `pricing.write` | always refused (`409`); to go back, publish the old values again |

Both reads were open to anyone before; they now need an admin token (`401` without one, `403` for an admin without `bookings.read`). The caregiver app and the WhatsApp flow do not call them.

### 13.1 Request body (POST and PUT)

All fields are sent every time (a full version, not a patch). Unknown fields are refused. Numbers must be JSON numbers (`5000`, `5000.5`), never strings, and `surge_pricing` must be `true` or `false`, not `1` or `"true"` (`422`).

| Field | Rule |
|---|---|
| `base_fee` | naira per hour; at least 0.01, at most 1,000,000, at most 2 decimal places |
| `platform_fee_percent` | 0 to 100 |
| `multipliers` | optional object; keys must be `nurse`, `doctor`, `physiotherapist`, `caregiver` or `nutritionist`; each above 0 and at most 10 |
| `surge_pricing` | boolean, default `false` |
| `surge_multiplier` | 1 to 5, default `1` |
| `discount` | 0 to 100, default `0` |
| `reason` | **required**, not blank, at most 2000 characters; stored in the audit log |

### 13.2 Publish a version

`POST` (first version only) and `PUT` return the new row (same shape as the reads). `reason` is not echoed back.

<!-- capture: 145-pricing-create.json request.body -->
```json
{
  "base_fee": 5000,
  "platform_fee_percent": 10,
  "multipliers": {
    "nurse": 1.0,
    "doctor": 2.0
  },
  "surge_pricing": false,
  "surge_multiplier": 1.0,
  "discount": 0,
  "reason": "Opening price list"
}
```

<!-- capture: 145-pricing-create.json response.body -->
```json
{
  "base_fee": 5000.0,
  "platform_fee_percent": 10.0,
  "multipliers": {
    "nurse": 1.0,
    "doctor": 2.0
  },
  "surge_pricing": false,
  "surge_multiplier": 1.0,
  "discount": 0.0,
  "id": "37fab89f-6286-4aa0-b23f-fcd28bd7096b",
  "created_at": "2026-10-07T20:40:03.779220",
  "updated_at": "2026-10-07T20:40:03.779220"
}
```

`PUT` of the row above with `base_fee` 6500 and surge on. Note the new `id`:

<!-- capture: 146-pricing-update.json response.body -->
```json
{
  "base_fee": 6500.0,
  "platform_fee_percent": 10.0,
  "multipliers": {
    "nurse": 1.0,
    "doctor": 2.0
  },
  "surge_pricing": true,
  "surge_multiplier": 1.5,
  "discount": 0.0,
  "id": "35727fd8-5030-48d5-b98f-b11bcdcfbe14",
  "created_at": "2026-10-07T20:40:03.790260",
  "updated_at": "2026-10-07T20:40:03.790260"
}
```

The history keeps both. Version 1 still has its original values:

<!-- capture: 149-pricing-list.json response.body -->
```json
[
  {
    "base_fee": 6500.0,
    "platform_fee_percent": 10.0,
    "multipliers": {
      "nurse": 1.0,
      "doctor": 2.0
    },
    "surge_pricing": true,
    "surge_multiplier": 1.5,
    "discount": 0.0,
    "id": "35727fd8-5030-48d5-b98f-b11bcdcfbe14",
    "created_at": "2026-10-07T20:40:03.790260",
    "updated_at": "2026-10-07T20:40:03.790260"
  },
  {
    "base_fee": 5000.0,
    "platform_fee_percent": 10.0,
    "multipliers": {
      "nurse": 1.0,
      "doctor": 2.0
    },
    "surge_pricing": false,
    "surge_multiplier": 1.0,
    "discount": 0.0,
    "id": "37fab89f-6286-4aa0-b23f-fcd28bd7096b",
    "created_at": "2026-10-07T20:40:03.779220",
    "updated_at": "2026-10-07T20:40:03.779220"
  }
]
```

### 13.3 Errors

Validation errors are the usual list-of-objects `detail` (section 9); the reason rule matches the booking policy's (section 12.9). Examples of the ones that matter:

No `reason` (`422`):

<!-- capture: 139-pricing-create-no-reason.json response.body keys=detail -->
```json
{
  "detail": [
    {
      "type": "missing",
      "loc": [
        "body",
        "reason"
      ],
      "msg": "Field required",
      "input": {
        "base_fee": 5000,
        "platform_fee_percent": 10,
        "multipliers": {
          "nurse": 1.0,
          "doctor": 2.0
        },
        "surge_pricing": false,
        "surge_multiplier": 1.0,
        "discount": 0
      }
    }
  ]
}
```

A negative `base_fee` (`422`):

<!-- capture: 140-pricing-create-negative-fee.json response.body -->
```json
{
  "detail": [
    {
      "type": "greater_than_equal",
      "loc": [
        "body",
        "base_fee"
      ],
      "msg": "Input should be greater than or equal to 0.01",
      "input": -1,
      "ctx": {
        "ge": 0.01
      }
    }
  ]
}
```

An unknown service type in `multipliers` (`422`):

<!-- capture: 142-pricing-create-unknown-service.json response.body -->
```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": [
        "body",
        "multipliers"
      ],
      "msg": "Value error, unknown service type(s): wizard",
      "input": {
        "wizard": 1.0
      },
      "ctx": {
        "error": {}
      }
    }
  ]
}
```

An unexpected field (`422`):

<!-- capture: 144-pricing-create-unknown-field.json response.body -->
```json
{
  "detail": [
    {
      "type": "extra_forbidden",
      "loc": [
        "body",
        "id"
      ],
      "msg": "Extra inputs are not permitted",
      "input": "580f3ec9-3f77-4bc6-9bfb-f02373dc0ee8"
    }
  ]
}
```

Editing a version that is no longer the latest (`409`, `detail` is a string and contains the id of the newer one):

<!-- capture: 147-pricing-update-superseded.json response.body -->
```json
{
  "detail": "A newer pricing version exists (35727fd8-5030-48d5-b98f-b11bcdcfbe14); reload it and edit that one"
}
```

`POST` when a version already exists (`409`; use `PUT` on the latest):

<!-- capture: 153-pricing-create-when-one-exists.json response.body -->
```json
{
  "detail": "Pricing already exists; use PUT on the latest version (35727fd8-5030-48d5-b98f-b11bcdcfbe14)"
}
```

A number sent as a string (`422`):

<!-- capture: 154-pricing-create-string-number.json response.body -->
```json
{
  "detail": [
    {
      "type": "float_type",
      "loc": [
        "body",
        "base_fee"
      ],
      "msg": "Input should be a valid number",
      "input": "5000"
    }
  ]
}
```

Unknown id on `PUT` (`404`), no version yet on `GET /latest` (`404`), `DELETE` (`409`), no token (`401`), no permission (`403`):

<!-- capture: 148-pricing-update-unknown-id.json response.body -->
```json
{
  "detail": "Booking settings not found"
}
```

<!-- capture: 151-pricing-delete.json response.body -->
```json
{
  "detail": "Pricing versions cannot be deleted; publish a new version instead"
}
```

<!-- capture: 138-pricing-create-as-support.json response.body -->
```json
{
  "detail": "Access denied. Missing permission: pricing.write"
}
```

<!-- capture: 136-pricing-get-as-analyst.json response.body -->
```json
{
  "detail": "Access denied. Missing permission: bookings.read"
}
```

### 13.4 What the console must do

- **Use `POST` only when there is no version yet** (`GET /latest` is `404`). Otherwise edit with `PUT` on the latest `id`.
- **After a save, switch to the `id` in the response.** Keep it as the id to send on the next `PUT`. A `PUT` with the old id is a `409`.
- **On `409` from `PUT`, reload `GET /latest` and show the newer values** to the admin before they save again. Someone else published first; do not retry blindly.
- **Ask for a reason before every save.** It is mandatory and is what the audit log shows.
- **Tell the admin a new price applies to quotes made from now on.** Bookings and quotes already made keep the price they were made at; nothing already booked changes.
- **Do not offer delete.** Offer "restore this version" on the history list instead: `POST` that version's values with a reason.
- **Multiplier fields:** offer only the five service types, and limit input to 10.

### 13.5 The audit entry

Every publish writes one `admin_audit_log` entry (`GET /admin/audit?entity_type=booking_settings`): `action` is `booking_settings.create` (`POST`) or `booking_settings.update` (`PUT`), `entity_id` is the **new** version's id, `before` is the version that was live (`null` for the very first) and `after` the new one, both with `base_fee` in naira.

<!-- capture: 152-audit-pricing.json response.body path=items first=1 -->
```json
[
  {
    "id": "17090945-d09f-45d3-928c-3d1362b7c80b",
    "admin_user_id": "25773b68-4c57-49f5-a5fc-091660298541",
    "admin_email": "boss-f722cepr@example.com",
    "action": "booking_settings.update",
    "entity_type": "booking_settings",
    "entity_id": "35727fd8-5030-48d5-b98f-b11bcdcfbe14",
    "before": {
      "id": "37fab89f-6286-4aa0-b23f-fcd28bd7096b",
      "base_fee": 5000.0,
      "platform_fee_percent": 10.0,
      "multipliers": {
        "nurse": 1.0,
        "doctor": 2.0
      },
      "surge_pricing": false,
      "surge_multiplier": 1.0,
      "discount": 0.0
    },
    "after": {
      "id": "35727fd8-5030-48d5-b98f-b11bcdcfbe14",
      "base_fee": 6500.0,
      "platform_fee_percent": 10.0,
      "multipliers": {
        "nurse": 1.0,
        "doctor": 2.0
      },
      "surge_pricing": true,
      "surge_multiplier": 1.5,
      "discount": 0.0
    },
    "reason": "Fuel costs went up",
    "ip": "172.18.0.1",
    "user_agent": "python-httpx/0.28.1",
    "created_at": "2026-10-07T20:40:03.790926"
  }
]
```

## 14. Verification table

"Live" means a request/response was captured from the running docker stack (the capture number is given). "Test only" means covered by pytest but not captured. "Not live" means neither could be exercised end to end; the shape or behaviour comes from the named source.

| Behaviour | Status | Evidence |
|---|---|---|
| Login, MFA required shape | Verified live | 004 |
| Login, enrolment-required shape (secret, `otpauth_uri`) | Verified live | 034, 057 |
| MFA verify, success shape with `admin` | Verified live | 007, 035 |
| Wrong code, bad MFA token | Verified live | 005, 006 |
| Replay of a used code refused | Verified live | 009 |
| Wrong password, unknown email, invited and disabled all identical | Verified live | 001, 002, 029, 053 |
| Password throttle: 5 free failures, `429` + `Retry-After` from the 6th, identical for known and unknown email | Verified live | 063, 064, 065, 066 |
| Right password while throttled is `429` | Verified live | 067 |
| Doubling of the wait, the 15-minute cap, the other-IP-unaffected rule | Test only | `test_admin_auth_review.py` (a second IP cannot be simulated through the docker port) |
| Invited and disabled emails throttle identically to unknown | Test only | `test_admin_auth_review.py` |
| Code-step lock on the 11th attempt, `Retry-After: 900` | Verified live | 068, 069, 070 |
| Right password at `login` while locked `423`; wrong password `401` | Verified live | 071, 072 |
| Lock escalation 1 h then 24 h, reset on success | Test only | `test_admin_auth_review.py` |
| `mfa_token` works once | Verified live | 073, 074, 075 |
| Per-IP `429` with `Retry-After`, on `mfa/verify` and `login` | Verified live | 076, 077 |
| Refresh unaffected by the per-IP limit | Verified live | 078 |
| Real client IP behind `TRUSTED_PROXIES` (`X-Forwarded-For`) | Test only | `test_admin_auth_review.py`; no proxy in the capture stack |
| Logout racing a refresh revokes the new token | Test only | `test_admin_auth_review.py` (two threads) |
| `GET /admin/me` | Verified live | 010, 039 |
| App token on admin route, admin token on app route | Verified live | 014, 015 |
| Refresh rotation | Verified live | 016, 017 |
| Reuse detection revokes all sessions, immediately | Verified live | 018, 019, 020 |
| Logout, and other sessions unaffected | Verified live | 022, 023, 024 |
| Idle timeout | **[db-simulated]** | 062 (expiry moved into the past; the real 30 minutes were not waited) |
| 12-hour session cap | Test only | not captured |
| Access token really expires after 15 minutes | Test only | `expires_in: 900` seen live (007); the expiry itself was not waited |
| Invite create, accept, single use | Verified live | 025, 032, 033 |
| Invite expiry | **[db-simulated]** | 038 |
| Invite delivery by email | **[not live]** | not built; the token is returned in the API response |
| First sign-in forces MFA enrolment | Verified live | 034, 035, 036 |
| Admin list, role change, disable, re-enable, reset MFA | Verified live | 045, 046, 051, 054, 056 |
| Last Super Admin and self-disable refused | Verified live | 049, 050 |
| Two Super Admins removing each other at once | Test only | concurrency test in `test_admin_users.py` |
| Two simultaneous submissions of one code | Test only | `test_admin_auth.py` |
| `403` for a missing permission | Verified live | 040, 041, 043 |
| Every admin route against every role | Test only | `test_admin_rbac.py` (about 50 routes, 7 roles) |
| Shared routes (booking detail, document file, prescriptions) with an admin token | Test only | `test_admin_rbac.py` and the existing access tests |
| Audit viewer, filters, paging | Verified live (as Super Admin) | 059, 060, 061 |
| Audit viewer for Finance `200`; other roles `403` | **[not live]** test only (`test_finance_reads_the_audit_log_but_cannot_manage_admins`) | |
| Bookings list: filters, search, `no_show`, masking without `people.pii`, bad enum `422` | Verified live | 079 to 084 |
| Date-range filters, sort, pagination, search by phone in each format, LIKE-wildcard escaping | Test only | `test_admin_bookings.py` |
| Unmatched queue and the `reason` filter | Verified live | 085, 086 |
| `sla_breached` reason, ordering, exclusions (cancelled, unpaid, accepted), no double counting | Test only | `test_admin_bookings.py` (the live run seeded no booking past the SLA) |
| Booking detail as Operations, Finance, Clinical Reviewer; `404` | Verified live | 087 to 091 |
| Clinical read audited; masked reads leave no audit entry | Verified live / Test only | 090, 092 live; "no entry when masked" and the plain-text `condition` path are tests |
| Payment attempts (`payment.attempts`) | Test only | the capture stack has no wallet transaction for a booking, so `attempts` is `[]` in every capture |
| Notifications and the outbox (`events`, `notification_*` timeline entries) | Test only | `test_admin_bookings.py`; no worker runs in the capture stack, so nothing was queued or delivered live |
| Candidates | Verified live | 093 |
| Re-dispatch: reason required, held `409`, success | Verified live | 094, 095, 096 |
| Re-dispatch with nobody eligible, wider radius, re-offer to earlier caregivers | Test only | `test_admin_bookings.py` |
| Assign offer, unapproved caregiver `409`, force as non-Super-Admin `403`, force | Verified live | 097, 098, 099, 100 |
| Caregiver then accepting an offered booking; admin force-assign racing a caregiver accept (exactly one wins) | Test only | `test_admin_bookings.py` (real threads, real row locks) |
| Reassign, same-caregiver `409`, assign on an accepted booking `409` | Verified live | 101, 102, 103 |
| Push notifications to caregivers on assign, reassign, cancel | Test only | fake Expo in `test_admin_bookings.py`; no push token exists in the capture stack |
| Cancel: full refund, partial without amount, again, unpaid with refund, unpaid | Verified live | 104 to 108 |
| Cancel partial with an amount, `refund: none`, closing open offers, notifying the caregiver | Test only | `test_admin_bookings.py` |
| Confirm address: outside Nigeria `422`, success starts dispatch, again `409`, timeline after | Verified live | 109 to 112 |
| No-show flag and again | Verified live | 113, 114 |
| No-show preconditions (not due yet, not accepted, already started) | Test only | `test_admin_bookings.py` |
| Correct status: as Operations `403`, as Super Admin, again `409` | Verified live | 115, 116, 117 |
| Impossible status moves | Test only | `test_admin_bookings.py` |
| Every action's audit entry | Verified live (one) / Test only (all) | 118; each action's own entry is asserted in `test_admin_bookings.py` |
| Every `/admin/bookings` route against every role | Test only | `test_admin_rbac.py` |
| The patients, caregivers and bookings used | **[seeded]** | 079 note |
| WhatsApp patient told of an admin cancel, refund or reassignment | Test only | the `booking_cancelled_by_admin`, `refund_flagged` and `caregiver_reassigned` events and the delivered text are asserted against a fake Meta; real Meta not exercised |
| First Super Admin and the Analyst in these captures | **[seeded]** | 004, 043 notes |
| `Retry-After` readable from the browser (CORS) | **[not live]** | The API exposes the header, but the capture stack has no allowed origin and no browser was used; check it once the console runs |
| A real authenticator app scanning the QR code | **[not live]** | the captures generate codes with the same TOTP algorithm (`pyotp`), which is what authenticator apps implement |
| Booking policy: read as Support, change as Operations (partial body), read after, audit entry with old and new values and the reason | Verified live | 119, 131, 132, 133 (134 puts the defaults back) |
| Booking policy errors: no reason, empty change, unknown field, percent over 100, late not shorter than free, decision not shorter than re-dispatch lead, Support `403`, no token `401`; the earning terms: share over 100, hold over a week, a cadence that does not exist | Verified live | 120 to 130 |
| Booking policy: the database rejects out-of-range values and a second row | Test only | `test_dropout_migration.py` |
| An edit leaves already-quoted bookings on their old terms | Test only | `test_cancellation_tiers.py`; the Docker e2e (`scripts/whatsapp_local_e2e.py`) checks it through the chat |
| Pricing: reads need an admin (`401`, `403`), empty `404` | Verified live | 135, 136, 137 |
| Pricing: publish with `POST`, `PUT` creates a new row, history keeps the old one | Verified live | 145, 146, 149, 150 |
| Pricing: validation errors (reason, negative fee, percent, unknown service, multiplier cap, unknown field), stale `PUT` `409`, unknown id `404`, `DELETE` `409`, `403` as Support | Verified live | 138 to 144, 147, 148, 151 |
| Pricing: audit entry with before/after | Verified live | 152 |
| Pricing: `POST` when a version exists `409`; a string number `422` | Verified live | 153, 154 |
| Pricing: fee decimals (0.001, 5000.123 refused), strict types (bool, `1`, strings), clock-skew ordering, two simultaneous first `POST`s | Test only | `test_booking_settings_pricing.py` |
| Pricing: two simultaneous `PUT`s on one version (exactly one wins) | Test only | `test_booking_settings_pricing.py` (real threads) |
| Pricing: boundary values, every rejected value, quotes and bookings keep the version they were priced with | Test only | `test_booking_settings_pricing.py` |
| Pricing: every `/booking-settings` route against every role | Test only | `test_admin_rbac.py` |
| Pricing: the empty-state `404` was captured after deleting rows on the throwaway capture stack | **[db-simulated]** | 137 note |
| Earnings: list, filter by booking, newest first; `payments.read` only (Operations `403`, no token `401`, bad status `422`) | Verified live | 158, 159, 160, 161, 162 |
| Earnings: hold, hold again `409`, release the hold, release again `409`; Support `403`; no reason `422`; unknown `404` | Verified live | 163 to 170 |
| Earnings: void as Super Admin, Finance `403`, void again `409`, hold on a voided one `409` | Verified live | 171 to 174 |
| Earnings: a no-show flag, then completing the visit credits the earning already held | Verified live | 175, 176, 177 |
| Earnings: the release job moves due pending rows and leaves held and voided ones; a released row refuses hold | Verified live | 178 (**[db-simulated]** wait), 179, 180, 181 |
| Earnings: audit entries for hold, release of hold and void | Verified live | 182 |
| Earnings: credit amount, rounding, the snapshot ignoring a later policy edit, idempotency under concurrent completes, SKIP LOCKED with two workers, late-cancel compensation rules | Test only | `test_caregiver_earnings.py` (real threads, real row locks) |
| Earnings: the cron schedule (every five minutes) | Test only | `test_caregiver_earnings.py::test_the_job_is_registered_in_the_worker`; not waited for live |
| Payouts: seeding, the batch step (3 of 5 caregivers, minimum and cool-down respected), batch list and detail, filters, `404`, bad status `422`, Operations `403`, no token `401` | Verified live, **against a fake Paystack** | 183 to 193 (the batch step is the real function run in the worker container for a date a week ahead; the schedule itself was not waited for). **Every payout in these captures carries `attempt_history`, `shortfall_kobo`, `returned_kobo`, `netted_kobo` and the two `caregiver_payouts_frozen_*` fields** |
| Payouts: cancel (Operations `403`, no reason `422`, unknown `404`, success as Finance, again `409`), retry of a waiting payout `409` | Verified live | 194 to 199 |
| Payouts: approve (Operations `403`, no or blank reason `422`, unknown `404`, success as Finance, again `409`), cancel after approval `409` | Verified live | 200 to 206 |
| Payouts: the send step posts the transfers; Paystack refusing one (balance too low) fails it and returns the money | Verified live, **against a fake Paystack** | 207, 208 |
| Payouts: signed `transfer.success` webhook; a webhook delivered twice changes nothing | Verified live, **against a fake Paystack** | 209, 210, 217, 220 |
| Payouts: retry (Finance `403`, Operations `403`, no reason `422`, a paid payout `409`, success with a new reference, again `409`) | Verified live | 211 to 216 |
| Payouts: a `transfer.reversed` after a success: payout `needs_attention`, caregiver frozen, **no money returned**; retry refused `409`; resolve as failed returns the money (`returned_kobo`); unfreeze; then retry | Verified live, **against a fake Paystack** | 218 to 224 |
| Payouts: the reconcile job settles a transfer whose webhook never came | Verified live, **against a fake Paystack** (**[db-simulated]** wait: 225) | 225, 226, 227, 228 |
| Payouts: audit entries for approve, cancel and retry | Verified live | 229, 230 |
| Payouts: the send job cancels a payout whose caregiver was suspended after approval and sends nothing for it | Verified live, **against a fake Paystack** (**[db-simulated]** suspension: 234) | 234 to 236 |
| Payouts: resolve (Finance `403`, no reason `422`, bad outcome `422`, unknown `404`, Paystack still at OTP `409`, Paystack contradicts `409`, paid `200`, again `409`) | Verified live, **against a fake Paystack** (**[simulated]** OTP entered in the Paystack dashboard: 242) | 237 to 245 |
| Payouts: a success after the money was returned: money taken back with a shortfall, payout `needs_attention`, caregiver frozen, retry refused, resolve as failed refused, resolve as paid | Verified live, **against a fake Paystack** (**[db-simulated]** balance change: 247) | 246 to 252, 258 |
| Payouts: unfreeze (Finance `403`, no reason `422`, unknown caregiver `404`, not frozen `409`, refused while a payout needs attention `409`, refused with a shortfall the balance cannot cover `409`, success that debits the shortfall from earnings (**[db-simulated]** earnings added just before: stated in the note of 259), success with `write_off_shortfall`, after a reversal, after a no-record flag, after a netted resolve) | Verified live | 223, 253 to 257, 259, 260, 267, 275 (**[db-simulated]** balance change: 271), 301 |
| Payouts: resolve refused (`409`) when a late webhook changes the attempts between Paystack's answer and the lock; a wrong-amount success flagging a payout in any status (and resolving it as `failed`, closed with no money moving, or as `paid`, taking the money back); a `failed` or `reversed` after an admin-forced `paid`; the unfreeze debit when several payouts hold the shortfall | **[not live]**, covered by tests | `test_payout_hardening.py` (the race is injected by committing the webhook on a second session inside the Paystack call) |
| Payouts: no record at Paystack: `needs_attention`, caregiver frozen, money still reserved; resolve as failed refused without `force`, accepted with it and the money returned | Verified live, **against a fake Paystack** (**[db-simulated]** 31-minute wait: 261) | 261 to 266 |
| Payouts: retry asks Paystack first: still pending `409`; reports paid `409` and the payout becomes `needs_attention` with a shortfall; a retry of a transfer Paystack refused is accepted | Verified live, **against a fake Paystack** | 268 to 275; 215, 224 |
| Payouts: cancel a batch (Operations `403`, no reason `422`, unknown `404`, success as Finance, again `409`, approved batch `409`) | Verified live | 276 to 283 |
| Payouts: audit entries for batch cancel and resolve | Verified live | 284, 285 |
| Payouts: netting. A late success while the newer payout was already sent: both `needs_attention`, shortfall on the first; a failure for the newer payout returns nothing; resolve as failed keeps the reservation against the shortfall (`returned_kobo` 0, `netted_kobo` 1000000); resolve the first as paid; unfreeze; the audit entry | Verified live, **against a fake Paystack** (**[db-simulated]** every other caregiver put in the cool-down: 286) | 286 to 302 |
| Payouts: a resolve that returns part of the reservation (`returned_kobo` above 0 with `netted_kobo` above 0); a success for an earlier attempt while a retry holds the reservation; a second success paying twice; a late success while the newer payout is not yet sent (it is cancelled); the wrong-amount success freezing; retry and approval for a frozen caregiver; the send job skipping a frozen caregiver; erasure refused while frozen; success arriving for a `needs_attention` payout of no record; retry refused when Paystack says OTP, no record or unreachable; resolve refused when the money is no longer reserved or the payout changed meanwhile; the send-time check for an inactive or erased account or a profile under review; concurrent late successes taking the money back once; a batch reserving a payout while a late success waits for the wallet; **random event sequences never pay more than was earned or raise what can be paid** | Test only | `test_payout_hardening.py` (real threads, real row locks; the sequence test is seeded) |
| Payouts: approval cancels a payout whose caregiver was suspended or changed bank account; the other 409 texts; wrong-amount webhooks; bad signature; no-answer from Paystack; the not-found grace period; concurrent approve/cancel, concurrent webhooks and two send workers | Test only | `test_payouts.py` (real threads, real row locks) |
| Payouts: every `/admin/payouts` route against every role | Test only | `test_admin_rbac.py` |
| A live Paystack account: transfers, webhooks, balance, OTP setting | **[not live]** | never exercised; shapes are Paystack's documented ones as implemented in `app/tests/fake_meta.py` |
| Refunds: seeding, admin cancel (full and partial) queues a `system` refund | Verified live, seeded bookings | 303 to 306 |
| Refunds: queue list (default open, filters, by booking), bad status `422`, Clinical Reviewer `403`, no token `401`, detail as Finance and Super Admin, unknown `404` | Verified live | 307 to 315 |
| Refunds: approve (Support `403`, Finance above the limit `403`, too much `422`, three decimals `422`, wallet as Finance `403`, a lower amount, again `409`, Super Admin above the limit, within the limit) | Verified live | 316 to 324 |
| Refunds: reject (no reason `422`, success, again `409`) | Verified live | 325 to 327 |
| Refunds: staff request (`201`, open refund `409`, not completed `422`, too much `422`, zero `422`, bad reason `422`, unknown booking `404`, Clinical Reviewer `403`), take-back on a booking that is not completed `422` | Verified live | 328 to 337 |
| Refunds: wallet destination (WhatsApp booker `422`; Super Admin success, refund `succeeded` at once, booking `refunded`) | Verified live | 338 to 340 |
| Refunds: send job, `refund.processing` and `refund.processed` webhooks, booking `refunded` / `partially_refunded` | Verified live, **against a fake Paystack** | 341 to 349 |
| Refunds: `refund.needs-attention`, bank details (approver `403`, Operations `403`, short number `422`, unknown bank `422`, not waiting `409`, another Finance admin succeeds), Support's view with the bank fields `null`, `refund.processed` after, no full account number in any response | Verified live, **against a fake Paystack** | 350 to 361 |
| Refunds: staff refund of a completed visit with take-back (own request `403`, no reason `422`, preview, approve, processed, wallet debited and the rest owed) | Verified live, **against a fake Paystack** (**[db-simulated]** release of the earning and a 1,000.00 wallet: 363) | 362 to 368, 379 to 381 |
| Refunds: failed webhook, retry (not failed `409`, Operations `403`, success, attempt 2, processed) | Verified live, **against a fake Paystack** | 370 to 378 |
| Refunds: failed then switch to wallet (Finance `403`, Super Admin success, again `409`); failed then close (no reason `422`, not failed `409`, success) | Verified live, **against a fake Paystack** | 382 to 389 |
| Refunds: Paystack processes a different amount: `needs_review`; resolve (Finance `403`, no note `422`, not in review `409`, failed while Paystack shows processed `409`, succeeded naming the amount `409`, succeeded with it) | Verified live, **against a fake Paystack** (**[simulated]** Paystack's amount changed: 390) | 390 to 399 |
| Refunds: a send nobody answered stays `processing` and is never resent; reconcile after 11 minutes does nothing; after 31 minutes with no Paystack record it is `needs_review`; resolve as failed | Verified live, **against a fake Paystack** (**[simulated]** job clock moved ahead: 403, 404) | 400 to 406 |
| Refunds: audit entries for approve, bank details, resolve, and the list for the entity | Verified live | 407 to 410 |
| Refunds: the retired routes answer `410` (admin list and decide, patient request); Operations `403` | Verified live | 411 to 415 |
| Booking list filter, list and detail with `partially_refunded` | Verified live (after the fix `c1b9cdd`) | 348; `test_a_partially_refunded_booking_lists_filters_and_opens` |
| Refunds: caregiver cancel of a paid app booking, WhatsApp cancel by the booker, payment after expiry or cancellation each queue a refund | Test only | `test_refunds_create.py`, `test_whatsapp_notifications.py` |
| Refunds: every `503` "couldn't reach Paystack" answer; a `processed` found while retrying; another refund on the payment; earlier attempt paid; the within-total backstop; races (double approve, approve vs webhook, two send workers, webhook vs reconcile, take-back vs payout batch) and 100 random event sequences | Test only | `test_refunds_failures.py`, `test_refunds_final_review.py`, `test_refunds_sequences.py` |
| Refunds: the owed balance recovered from a later release or payout reservation; retry of a payout refused while a debt is owed | Test only | `test_refunds_take_back.py` |
| Refunds: the WhatsApp notices (sent, complete, needs bank details) | **[not live]** | queued and asserted against a fake Meta in tests; not delivered live |
| Refunds: real Paystack refunds, their webhooks, and the up to 10 business days for the family to see the money | **[not live]** | never exercised; shapes are Paystack's documented ones as implemented in `app/tests/fake_meta.py` |
| `GET /admin/me` for a Super Admin lists `refunds.approve_above_limit` | Test only | captures 010 and 039 were taken before the permission existed; `test_admin_rbac.py` |

