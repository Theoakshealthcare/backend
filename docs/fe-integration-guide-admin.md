# FE integration guide: admin console (sign-in, admin management, bookings operations, booking policy, pricing)

| | |
|---|---|
| **Backend work package** | A0 (sections 3 to 11), branch `feat/admin-auth`, migration `0003_admin_identity`. A2 (section 12), branch `feat/admin-bookings-ops`, migration `0006_admin_bookings_ops`. Booking policy (section 12.9), branch `feat/whatsapp-dropout-redispatch-fee-tiers`. Pricing settings (section 13), branch `fix/booking-settings-audit`, no migration |
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
6. **Every admin action is audited**, and Super Admins can read the audit log.

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
  "email": "boss-1b46d2@example.com",
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
  "otpauth_uri": "otpauth://totp/Oakly%20Admin:verifier-1b46d2%40example.com?secret=<TOTP_SECRET>&issuer=Oakly%20Admin",
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
    "id": "b27c8dbb-0a3c-4566-8e37-65d41a1e3c5e",
    "email": "verifier-1b46d2@example.com",
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
  "id": "b27c8dbb-0a3c-4566-8e37-65d41a1e3c5e",
  "email": "verifier-1b46d2@example.com",
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
  "offset": 0
}
```

| Route | Permission |
|---|---|
| `GET /admin/caregivers`, `GET /admin/caregivers/{id}` | `caregivers.read` |
| `POST /admin/caregivers/documents/{id}/decision`, `.../identity-decision`, `.../background-check-decision`, `PUT .../skills`, `.../approve`, `.../reject`, `.../request-changes` | `caregivers.approve` |
| `POST /admin/caregivers/{id}/suspend`, `.../reinstate` | `caregivers.suspend` |
| `GET /booking-settings/`, `GET /booking-settings/latest` | `bookings.read` (section 13) |
| `POST`/`PUT`/`DELETE /booking-settings/...` | `pricing.write` (section 13) |
| `POST`/`PUT`/`DELETE /prescription/settings/...`, `PATCH /prescription/{id}`, `POST /prescription/{id}/upload_note`, `PATCH /prescription/{id}/status` | `prescriptions.manage` |
| `POST`/`PUT`/`DELETE /fallguard/admin/question...`, `GET /fallguard/admin/questions` | `fallguard.write` |
| `GET /patient/admin/refund/requests`, `PUT /patient/admin/refund/requests/{id}` | `refunds.approve` |
| `GET /booking/ws/stats` | `dashboard.read` |
| `GET /admin/admins`, `POST /admin/admins`, `PATCH /admin/admins/{id}`, `POST /admin/admins/{id}/reset-mfa` | `admins.manage` |
| `GET /admin/audit` | `audit.read` |
| `GET /admin/bookings...`, `POST /admin/bookings/{id}/...` | see section 12.1 |

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
  "email": "verifier-1b46d2@example.com",
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
  "id": "b27c8dbb-0a3c-4566-8e37-65d41a1e3c5e",
  "email": "verifier-1b46d2@example.com",
  "full_name": "Placeholder Verifier",
  "status": "invited",
  "mfa_enabled": false,
  "roles": [
    "verification"
  ],
  "last_login_at": null,
  "locked_until": null,
  "created_at": "2026-10-06T14:09:01.528488",
  "invite_token": "<INVITE_TOKEN>",
  "invite_expires_at": "2026-10-08T14:09:01.527034"
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
  "email": "verifier-1b46d2@example.com"
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
    "id": "65d6a321-d38b-4cd4-a2fb-ae6b8621038c",
    "email": "boss-1b46d2@example.com",
    "full_name": "Placeholder Super Admin",
    "status": "active",
    "mfa_enabled": true,
    "roles": [
      "super_admin"
    ],
    "last_login_at": "2026-10-06T14:08:30.524504",
    "locked_until": null,
    "created_at": "2026-10-06T14:07:44.515392"
  },
  {
    "id": "b27c8dbb-0a3c-4566-8e37-65d41a1e3c5e",
    "email": "verifier-1b46d2@example.com",
    "full_name": "Placeholder Verifier",
    "status": "active",
    "mfa_enabled": true,
    "roles": [
      "verification"
    ],
    "last_login_at": "2026-10-06T14:09:02.377169",
    "locked_until": null,
    "created_at": "2026-10-06T14:09:01.528488"
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
  "id": "b27c8dbb-0a3c-4566-8e37-65d41a1e3c5e",
  "email": "verifier-1b46d2@example.com",
  "full_name": "Placeholder Verifier",
  "status": "active",
  "mfa_enabled": true,
  "roles": [
    "support",
    "verification"
  ],
  "last_login_at": "2026-10-06T14:09:02.377169",
  "locked_until": null,
  "created_at": "2026-10-06T14:09:01.528488"
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
  "id": "b27c8dbb-0a3c-4566-8e37-65d41a1e3c5e",
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
  "id": "b27c8dbb-0a3c-4566-8e37-65d41a1e3c5e",
  "email": "verifier-1b46d2@example.com",
  "full_name": "Placeholder Verifier",
  "status": "active",
  "mfa_enabled": false,
  "roles": [
    "support",
    "verification"
  ],
  "last_login_at": "2026-10-06T14:09:02.377169",
  "locked_until": null,
  "created_at": "2026-10-06T14:09:01.528488"
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
Needs `audit.read` (Super Admin only). Newest first.

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
    "id": "850d2542-4a3a-4179-aea9-575a03aa26d2",
    "admin_user_id": "65d6a321-d38b-4cd4-a2fb-ae6b8621038c",
    "admin_email": "boss-1b46d2@example.com",
    "action": "admin.reset_mfa",
    "entity_type": "admin_user",
    "entity_id": "b27c8dbb-0a3c-4566-8e37-65d41a1e3c5e",
    "before": null,
    "after": null,
    "reason": null,
    "ip": "172.21.0.1",
    "user_agent": "python-httpx/0.28.1",
    "created_at": "2026-10-06T14:09:03.594775"
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
    "id": "c8b9e484-ec10-4339-8ccb-d53a478c9f05",
    "booking_code": "871705FF",
    "service_type": "nurse",
    "scheduled_date": "2026-10-09T08:16:05.556184",
    "scheduled_end_date": "2026-10-09T10:16:05.556184",
    "city": "Lagos",
    "state": "Lagos",
    "patient": {
      "id": "3f2eb4aa-17f3-4af2-9086-f2b4971d9d3f",
      "full_name": "Chidi Eze",
      "phone": "+2340000780003",
      "masked": false
    },
    "caregiver": null,
    "status": "pending",
    "payment_status": "pending",
    "dispatch_state": null,
    "total_amount_kobo": 5500000,
    "channel": "app",
    "created_at": "2026-10-06T19:36:05.554254",
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
    "id": "c8b9e484-ec10-4339-8ccb-d53a478c9f05",
    "booking_code": "871705FF",
    "service_type": "nurse",
    "scheduled_date": "2026-10-09T08:16:05.556184",
    "scheduled_end_date": "2026-10-09T10:16:05.556184",
    "city": "Lagos",
    "state": "Lagos",
    "patient": {
      "id": "3f2eb4aa-17f3-4af2-9086-f2b4971d9d3f",
      "full_name": "Chidi E.",
      "phone": "+234****0003",
      "masked": true
    },
    "caregiver": null,
    "status": "pending",
    "payment_status": "pending",
    "dispatch_state": null,
    "total_amount_kobo": 5500000,
    "channel": "app",
    "created_at": "2026-10-06T19:36:05.554254",
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
    "id": "298911c7-8ff3-470b-895b-218232595b9a",
    "booking_code": "176803E9",
    "service_type": "nurse",
    "scheduled_date": "2026-10-07T01:16:05.548617",
    "scheduled_end_date": "2026-10-07T03:16:05.548617",
    "city": "Lagos",
    "state": "Lagos",
    "patient": {
      "id": "aecb971c-aa74-42d4-9cc9-ab244b17334e",
      "full_name": "Ada Okafor",
      "phone": "+2340000780001",
      "masked": false
    },
    "caregiver": null,
    "status": "pending",
    "payment_status": "paid",
    "dispatch_state": "unmatched",
    "total_amount_kobo": 5500000,
    "channel": "app",
    "created_at": "2026-10-06T19:36:05.547492",
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
  "id": "298911c7-8ff3-470b-895b-218232595b9a",
  "booking_code": "176803E9",
  "service_type": "nurse",
  "assistance_type": "Placeholder assistance",
  "booking_for": "myself",
  "duration_hours": 2,
  "scheduled_date": "2026-10-07T01:16:05.548617",
  "scheduled_end_date": "2026-10-07T03:16:05.548617",
  "actual_start_time": null,
  "actual_end_time": null,
  "status": "pending",
  "channel": "app",
  "cancel_reason": null,
  "created_at": "2026-10-06T19:36:05.547492",
  "updated_at": "2026-10-06T20:16:05.547492",
  "age_minutes": 40,
  "patient": {
    "id": "aecb971c-aa74-42d4-9cc9-ab244b17334e",
    "full_name": "Ada Okafor",
    "phone": "+2340000780001",
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
    "paystack_reference": "BKG_176803E9_72c932bc",
    "failure_reason": null,
    "expires_at": null,
    "refund_required": false,
    "refund_required_reason": null,
    "attempts": []
  },
  "dispatch": {
    "state": "unmatched",
    "started_at": "2026-10-06T18:46:05.549313",
    "waves": [],
    "offers": []
  },
  "events": [],
  "no_show_flagged_at": null,
  "no_show_suspected": false,
  "note_count": 0,
  "timeline": [
    {
      "at": "2026-10-06T18:46:05.549313",
      "kind": "dispatch_started",
      "title": "Dispatch started",
      "actor": "system",
      "detail": null
    },
    {
      "at": "2026-10-06T19:36:05.547492",
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
- `payment.refund_required_reason` is a free-text code: `paid_after_expiry`, `paid_after_cancellation`, `admin_cancelled_full` or `admin_cancelled_partial`. The amount to refund is `payment.refund_amount_kobo` (integer kobo; `null` means the refund module decides). *Not captured live: this field was added after the captures; the shape follows the schema.*

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
    "id": "3e16c514-650b-4e15-b13a-17c848a6dcc0",
    "admin_user_id": "a12f9eae-56fb-493e-b610-910e72a01ad5",
    "admin_email": "clin-7d8bc4ops@example.com",
    "action": "booking.clinical_view",
    "entity_type": "booking",
    "entity_id": "b316785e-b27a-44c6-85bd-3246c2ea9648",
    "before": null,
    "after": {
      "fields": [
        "condition",
        "special_instructions"
      ]
    },
    "reason": null,
    "ip": "172.20.0.1",
    "user_agent": "python-httpx/0.28.1",
    "created_at": "2026-10-06T20:16:05.711881"
  }
]
```

The condition of a WhatsApp booking is stored encrypted and is never returned to anyone without `clinical.read`; the caregiver's visit notes are not part of this response (only `note_count`).

**`timeline`** is every dated thing that happened, oldest first. `kind` is one of `booking_created`, `dispatch_started`, `wave_sent`, `offer_response`, `notification_queued`, `notification_delivered`, `visit_started`, `visit_completed`, `admin_action`. For `admin_action`, `title` is the audit action (`booking.cancel`) and `detail` is the admin's reason. A booking has no status-history table, so a status change by the caregiver or by the expiry job appears only through the fields it sets (`visit_started`, `visit_completed`, `cancel_reason`), not as its own entry. The timeline after confirming an address (dispatch wave, offers, the admin action):

<!-- capture: 112-booking-detail-after-confirm.json response.body path=timeline -->
```json
[
  {
    "at": "2026-10-06T19:36:05.549631",
    "kind": "booking_created",
    "title": "Booking created via whatsapp",
    "actor": "system",
    "detail": null
  },
  {
    "at": "2026-10-06T20:16:05.711881",
    "kind": "admin_action",
    "title": "booking.clinical_view",
    "actor": "admin: Placeholder clinical_reviewer",
    "detail": null
  },
  {
    "at": "2026-10-06T20:16:05.881099",
    "kind": "dispatch_started",
    "title": "Dispatch started",
    "actor": "system",
    "detail": null
  },
  {
    "at": "2026-10-06T20:16:05.881099",
    "kind": "wave_sent",
    "title": "Wave 1 sent",
    "actor": "system",
    "detail": "2 caregiver(s) offered the visit"
  },
  {
    "at": "2026-10-06T20:16:05.881938",
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
- **No action moves money.** Cancelling flags the booking for the refund queue; nothing is refunded until that queue (a later work package) acts.

<!-- capture: 096-redispatch.json response.body -->
```json
{
  "id": "298911c7-8ff3-470b-895b-218232595b9a",
  "booking_code": "176803E9",
  "status": "pending",
  "payment_status": "paid",
  "dispatch_state": "dispatching",
  "caregiver_id": null,
  "needs_address_check": false,
  "refund_required": false,
  "refund_required_reason": null,
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
      "caregiver_id": "ff505a54-e994-4ca8-95fe-24b171529f13",
      "full_name": "Placeholder Nurse One",
      "specialization": "nurse",
      "distance_km": 0.22,
      "estimated_arrival_minutes": 5,
      "match_score": 232.56
    },
    {
      "caregiver_id": "1be82916-b8e1-4d84-9f9e-307d0ff1a569",
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
  "id": "9840bf70-56cd-4786-8a1c-bd7d5628697e",
  "booking_code": "49E0EF92",
  "status": "pending",
  "payment_status": "paid",
  "dispatch_state": "dispatching",
  "caregiver_id": null,
  "needs_address_check": false,
  "refund_required": false,
  "refund_required_reason": null,
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
  "id": "7b87dae5-da81-48e1-b472-3a93ee299227",
  "booking_code": "20EB3DF4",
  "status": "accepted",
  "payment_status": "paid",
  "dispatch_state": "matched",
  "caregiver_id": "1be82916-b8e1-4d84-9f9e-307d0ff1a569",
  "needs_address_check": false,
  "refund_required": false,
  "refund_required_reason": null,
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
  "id": "db418452-c9a7-4da5-9f36-60b2c14ff241",
  "booking_code": "9B7F0978",
  "status": "accepted",
  "payment_status": "paid",
  "dispatch_state": "matched",
  "caregiver_id": "1be82916-b8e1-4d84-9f9e-307d0ff1a569",
  "needs_address_check": false,
  "refund_required": false,
  "refund_required_reason": null,
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
  "id": "195e0918-821a-45dc-a0c2-5a9d30182b95",
  "booking_code": "5939F55D",
  "status": "cancelled",
  "payment_status": "paid",
  "dispatch_state": "dispatching",
  "caregiver_id": null,
  "needs_address_check": false,
  "refund_required": true,
  "refund_required_reason": "admin_cancelled_full",
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

- **Cancelling does not refund.** `refund_required` becomes `true`; the booking's `payment_status` stays `paid`. Say "refund requested", never "refunded".
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
  "id": "b316785e-b27a-44c6-85bd-3246c2ea9648",
  "booking_code": "06C5304A",
  "status": "pending",
  "payment_status": "paid",
  "dispatch_state": "dispatching",
  "caregiver_id": null,
  "needs_address_check": false,
  "refund_required": false,
  "refund_required_reason": null,
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
  "id": "8d27d2d6-3ab0-4fc5-a9c5-6d4fbe0fa88b",
  "booking_code": "5F02D072",
  "status": "accepted",
  "payment_status": "paid",
  "dispatch_state": "matched",
  "caregiver_id": "ff505a54-e994-4ca8-95fe-24b171529f13",
  "needs_address_check": false,
  "refund_required": false,
  "refund_required_reason": null,
  "no_show_flagged_at": "2026-10-06T20:16:05.907604",
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
  "id": "5b2c3e3c-15e8-41c6-a8d7-0f28eda6f905",
  "booking_code": "224AF65E",
  "status": "completed",
  "payment_status": "paid",
  "dispatch_state": "matched",
  "caregiver_id": "1be82916-b8e1-4d84-9f9e-307d0ff1a569",
  "needs_address_check": false,
  "refund_required": false,
  "refund_required_reason": null,
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
    "id": "d47ba049-7ef0-4fc5-9547-ebdc1e24ba3a",
    "admin_user_id": "64f146c8-233c-44dd-acb0-00cb2c2686b2",
    "admin_email": "boss-7d8bc4ops@example.com",
    "action": "booking.correct_status",
    "entity_type": "booking",
    "entity_id": "5b2c3e3c-15e8-41c6-a8d7-0f28eda6f905",
    "before": {
      "status": "in_progress",
      "payment_status": "paid",
      "dispatch_state": "matched",
      "caregiver_id": "1be82916-b8e1-4d84-9f9e-307d0ff1a569",
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
      "caregiver_id": "1be82916-b8e1-4d84-9f9e-307d0ff1a569",
      "needs_address_check": false,
      "refund_required": false,
      "refund_required_reason": null,
      "cancel_reason": null,
      "no_show_flagged_at": null
    },
    "reason": "Caregiver forgot to check out",
    "ip": "172.20.0.1",
    "user_agent": "python-httpx/0.28.1",
    "created_at": "2026-10-06T20:16:05.933118"
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

The cancellation fee tiers and the caregiver drop-out timings are **data an admin edits**, not settings. One policy, eight whole numbers. Reading needs `bookings.read` (Support has it); changing needs `settings.write` (Operations and Super Admin) **and a `reason`**, and writes an audit entry with the old and new values. Errors here are `{"detail": ...}` like the other admin routes.

| Field | Meaning | Default |
|---|---|---|
| `cancel_free_hours` | full refund when the visit is further away than this (or nobody is assigned) | 12 |
| `cancel_late_hours` | inside this, or once the caregiver is on the way, the late fee applies | 2 |
| `cancel_fee_percent` | fee, percent of the service fee, between the two lines | 20 |
| `cancel_late_fee_percent` | fee, percent of the service fee, in the late window | 50 |
| `dropout_redispatch_min_lead_minutes` | a caregiver drop-out further ahead than this is re-dispatched automatically | 120 |
| `dropout_decision_lead_minutes` | no replacement by this long before the start: the family is asked | 60 |
| `onmyway_compensation_window_minutes` | an on-my-way tapped no earlier than this before the start can qualify for compensation | 120 |
| `caregiver_late_fee_share_percent` | caregiver's share of a late fee (stored now; paid by a later work package) | 80 |

Rules the form should mirror (the server enforces them, and so does the database): percents 0 to 100, hours and minutes 0 or more, `cancel_late_hours` shorter than `cancel_free_hours`, `dropout_decision_lead_minutes` shorter than `dropout_redispatch_min_lead_minutes`. Send **only the fields that change**; the merged result is validated as a whole.

**UX consequences to surface.** An edit changes the cancellation terms only for bookings **quoted afterwards**: each quote and booking keeps a copy of the tiers the family was shown. The drop-out timings and the on-my-way window apply immediately. Say so on the save confirmation.

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
  "updated_at": "2026-10-07T10:58:36.331403",
  "updated_by": null
}
```

Change, `PATCH` (Operations):

<!-- capture: 128-policy-update.json request.body -->
```json
{
  "reason": "Raise the late fee while we measure no-shows",
  "cancel_late_fee_percent": 60,
  "dropout_decision_lead_minutes": 45
}
```

<!-- capture: 128-policy-update.json response.body -->
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
  "updated_at": "2026-10-07T10:59:03.679353",
  "updated_by": "ed75f068-d405-423b-9c2f-e9de5b9abb05"
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

Support cannot change it (`403`), and no token is `401`:

<!-- capture: 127-policy-update-as-support.json response.body -->
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

<!-- capture: 130-audit-policy-update.json response.body path=items first=1 -->
```json
[
  {
    "id": "92c9caca-9e2a-454b-88eb-3056b49d0e7b",
    "admin_user_id": "ed75f068-d405-423b-9c2f-e9de5b9abb05",
    "admin_email": "ops-bb44e1pol@example.com",
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
      "caregiver_late_fee_share_percent": 80
    },
    "after": {
      "cancel_free_hours": 12,
      "cancel_late_hours": 2,
      "cancel_fee_percent": 20,
      "cancel_late_fee_percent": 60,
      "dropout_redispatch_min_lead_minutes": 120,
      "dropout_decision_lead_minutes": 45,
      "onmyway_compensation_window_minutes": 120,
      "caregiver_late_fee_share_percent": 80
    },
    "reason": "Raise the late fee while we measure no-shows",
    "ip": "172.21.0.1",
    "user_agent": "python-httpx/0.28.1",
    "created_at": "2026-10-07T10:59:03.680537"
  }
]
```

## 13. Pricing settings: `/api/v1/booking-settings`

The price list the app and the WhatsApp flow quote from: base fee per hour, platform fee, per-service multipliers, surge and discount. **Money is in naira here**, as before.

**Versions, not edits.** Every save publishes a **new row with a new `id`**. Old rows are never changed, because quotes and bookings point at the version they were priced with. The newest row is the live price list. So after a `PUT`, the `id` you edited is no longer the one in use; use the `id` from the response.

| Route | Permission | What it does |
|---|---|---|
| `GET /booking-settings/latest` | `bookings.read` | the live version |
| `GET /booking-settings/` | `bookings.read` | every version, newest first (the history) |
| `POST /booking-settings/` | `pricing.write` | publish a new version |
| `PUT /booking-settings/{id}` | `pricing.write` | publish a new version that replaces `{id}`; `{id}` must still be the latest |
| `DELETE /booking-settings/{id}` | `pricing.write` | always refused (`409`); to go back, publish the old values again |

Both reads were open to anyone before; they now need an admin token (`401` without one, `403` for an admin without `bookings.read`). The caregiver app and the WhatsApp flow do not call them.

### 13.1 Request body (POST and PUT)

All fields are sent every time (a full version, not a patch). Unknown fields are refused.

| Field | Rule |
|---|---|
| `base_fee` | naira per hour; above 0 and at most 1,000,000 |
| `platform_fee_percent` | 0 to 100 |
| `multipliers` | optional object; keys must be `nurse`, `doctor`, `physiotherapist`, `caregiver` or `nutritionist`; each above 0 and at most 10 |
| `surge_pricing` | boolean, default `false` |
| `surge_multiplier` | 1 to 5, default `1` |
| `discount` | 0 to 100, default `0` |
| `reason` | **required**, not blank, at most 2000 characters; stored in the audit log |

### 13.2 Publish a version

`POST` and `PUT` return the new row (same shape as the reads). `reason` is not echoed back.

<!-- capture: 142-pricing-create.json request.body -->
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

<!-- capture: 142-pricing-create.json response.body -->
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
  "id": "1e19d47f-6089-4d9b-9408-e524c8341da9",
  "created_at": "2026-10-07T14:13:03.912400",
  "updated_at": "2026-10-07T14:13:03.912400"
}
```

`PUT` of the row above with `base_fee` 6500 and surge on. Note the new `id`:

<!-- capture: 143-pricing-update.json response.body -->
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
  "id": "1db3d7e3-c7ad-4a7b-964b-e2775c061390",
  "created_at": "2026-10-07T14:13:03.923383",
  "updated_at": "2026-10-07T14:13:03.923383"
}
```

The history keeps both. Version 1 still has its original values:

<!-- capture: 146-pricing-list.json response.body -->
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
    "id": "1db3d7e3-c7ad-4a7b-964b-e2775c061390",
    "created_at": "2026-10-07T14:13:03.923383",
    "updated_at": "2026-10-07T14:13:03.923383"
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
    "id": "1e19d47f-6089-4d9b-9408-e524c8341da9",
    "created_at": "2026-10-07T14:13:03.912400",
    "updated_at": "2026-10-07T14:13:03.912400"
  }
]
```

### 13.3 Errors

Validation errors are the usual list-of-objects `detail` (section 9); the reason rule matches the booking policy's (section 12.9). Examples of the ones that matter:

No `reason` (`422`):

<!-- capture: 136-pricing-create-no-reason.json response.body keys=detail -->
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

<!-- capture: 137-pricing-create-negative-fee.json response.body -->
```json
{
  "detail": [
    {
      "type": "greater_than",
      "loc": [
        "body",
        "base_fee"
      ],
      "msg": "Input should be greater than 0",
      "input": -1,
      "ctx": {
        "gt": 0.0
      }
    }
  ]
}
```

An unknown service type in `multipliers` (`422`):

<!-- capture: 139-pricing-create-unknown-service.json response.body -->
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

<!-- capture: 141-pricing-create-unknown-field.json response.body -->
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
      "input": "9d76032d-dea8-4f25-9a47-20c8964b4dcb"
    }
  ]
}
```

Editing a version that is no longer the latest (`409`, `detail` is a string and contains the id of the newer one):

<!-- capture: 144-pricing-update-superseded.json response.body -->
```json
{
  "detail": "A newer pricing version exists (1db3d7e3-c7ad-4a7b-964b-e2775c061390); reload it and edit that one"
}
```

Unknown id on `PUT` (`404`), no version yet on `GET /latest` (`404`), `DELETE` (`409`), no token (`401`), no permission (`403`):

<!-- capture: 145-pricing-update-unknown-id.json response.body -->
```json
{
  "detail": "Booking settings not found"
}
```

<!-- capture: 148-pricing-delete.json response.body -->
```json
{
  "detail": "Pricing versions cannot be deleted; publish a new version instead"
}
```

<!-- capture: 135-pricing-create-as-support.json response.body -->
```json
{
  "detail": "Access denied. Missing permission: pricing.write"
}
```

<!-- capture: 133-pricing-get-as-analyst.json response.body -->
```json
{
  "detail": "Access denied. Missing permission: bookings.read"
}
```

### 13.4 What the console must do

- **After a save, switch to the `id` in the response.** Keep it as the id to send on the next `PUT`. A `PUT` with the old id is a `409`.
- **On `409` from `PUT`, reload `GET /latest` and show the newer values** to the admin before they save again. Someone else published first; do not retry blindly.
- **Ask for a reason before every save.** It is mandatory and is what the audit log shows.
- **Tell the admin a new price applies to quotes made from now on.** Bookings and quotes already made keep the price they were made at; nothing already booked changes.
- **Do not offer delete.** Offer "restore this version" on the history list instead: `POST` that version's values with a reason.
- **Multiplier fields:** offer only the five service types, and limit input to 10.

### 13.5 The audit entry

Every publish writes one `admin_audit_log` entry (`GET /admin/audit?entity_type=booking_settings`): `action` is `booking_settings.create` (`POST`) or `booking_settings.update` (`PUT`), `entity_id` is the **new** version's id, `before` is the version that was live (`null` for the very first) and `after` the new one, both with `base_fee` in naira.

<!-- capture: 149-audit-pricing.json response.body path=items first=1 -->
```json
[
  {
    "id": "74b84ba1-3f2b-4dff-8fe0-15160608f570",
    "admin_user_id": "c1df2ac6-f4df-434b-a3ef-815d307073a2",
    "admin_email": "boss-8ff147pr@example.com",
    "action": "booking_settings.update",
    "entity_type": "booking_settings",
    "entity_id": "1db3d7e3-c7ad-4a7b-964b-e2775c061390",
    "before": {
      "id": "1e19d47f-6089-4d9b-9408-e524c8341da9",
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
      "id": "1db3d7e3-c7ad-4a7b-964b-e2775c061390",
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
    "created_at": "2026-10-07T14:13:03.923953"
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
| Audit viewer, filters, paging | Verified live | 059, 060, 061 |
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
| Booking policy: read as Support, change as Operations (partial body), read after, audit entry with old and new values and the reason | Verified live | 119, 128, 129, 130 (131 puts the defaults back) |
| Booking policy errors: no reason, empty change, unknown field, percent over 100, late not shorter than free, decision not shorter than re-dispatch lead, Support `403`, no token `401` | Verified live | 120 to 127 |
| Booking policy: the database rejects out-of-range values and a second row | Test only | `test_dropout_migration.py` |
| An edit leaves already-quoted bookings on their old terms | Test only | `test_cancellation_tiers.py`; the Docker e2e (`scripts/whatsapp_local_e2e.py`) checks it through the chat |
| Pricing: reads need an admin (`401`, `403`), empty `404` | Verified live | 132, 133, 134 |
| Pricing: publish with `POST`, `PUT` creates a new row, history keeps the old one | Verified live | 142, 143, 146, 147 |
| Pricing: validation errors (reason, negative fee, percent, unknown service, multiplier cap, unknown field), stale `PUT` `409`, unknown id `404`, `DELETE` `409`, `403` as Support | Verified live | 135 to 141, 144, 145, 148 |
| Pricing: audit entry with before/after | Verified live | 149 |
| Pricing: two simultaneous `PUT`s on one version (exactly one wins) | Test only | `test_booking_settings_pricing.py` (real threads) |
| Pricing: boundary values, every rejected value, quotes and bookings keep the version they were priced with | Test only | `test_booking_settings_pricing.py` |
| Pricing: every `/booking-settings` route against every role | Test only | `test_admin_rbac.py` |
| Pricing: the empty-state `404` was captured after deleting rows on the throwaway capture stack | **[db-simulated]** | 134 note |
