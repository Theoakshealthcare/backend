# FE integration guide: admin console, sign-in and admin management

| | |
|---|---|
| **Backend work package** | A0, branch `feat/admin-auth`, migration `0003_admin_identity` |
| **Audience** | `apps/admin` (the console), mainly A1 |
| **Base URL** | `http://127.0.0.1:8000` for the local stack; every path below starts with `/api/v1` |
| **Source of truth** | Every request and response body, status code and error string below was **copied from a real response** captured by `scripts/capture_admin_flow.py` against the running docker compose stack. The files are in [`docs/fe-captures/admin/`](fe-captures/admin/); each example names its file. Anything that could not be exercised live is marked **[not live]** with the reason. |

## 0. How to read this guide

- **Examples are generated, not typed.** Every JSON block below is rendered from the capture named in the comment above it, by `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-admin.md docs/fe-captures/admin` (`--check` fails if the guide is stale; CI runs it). Never edit an example by hand: re-capture, then re-render.
- **Captures are re-derivable.** Start the stack (see [`docs/sop/2026-10-05-secrets-and-rotation.md`](sop/2026-10-05-secrets-and-rotation.md) for `OAKLY_ENV_FILE_PATH`) with `docker compose -p oakly-a0 up --build -d db api`, then run `poetry run python -m scripts.capture_admin_flow --env-file <the same env file>`. The script rewrites the capture directory and takes about five minutes, because it waits for the per-IP rate-limit window and for fresh 30-second TOTP steps.
- **Placeholders.** All data is fake: `example.com` emails, `Placeholder ...` names. UUIDs and timestamps come from the local stack and mean nothing. These values are replaced in the files: access tokens by `<ACCESS_TOKEN_JWT>`, the MFA token by `<MFA_TOKEN_JWT>`, refresh tokens by `<REFRESH_TOKEN>`, invite tokens by `<INVITE_TOKEN>`, TOTP secrets by `<TOTP_SECRET>`, 6-digit codes by `<TOTP_CODE>` (except the deliberate wrong code `000000`) and passwords by `<PLACEHOLDER_PASSWORD>`. The real values are random per run.
- **Some things were seeded or simulated**, because the API cannot do them yet. Each one is marked **[seeded]** or **[db-simulated]** here and has a `note` in its capture:
  - the first Super Admin and a Read-only Analyst were inserted straight into the database (in production the first Super Admin is created with `python -m scripts.create_super_admin`);
  - the 30-minute idle timeout and the 48-hour invite expiry were simulated by moving a timestamp into the past.
- Section 12 is the table of what is verified live and what is not.

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
  "email": "boss-ee5e3e@example.com",
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
  "otpauth_uri": "otpauth://totp/Oakly%20Admin:verifier-ee5e3e%40example.com?secret=<TOTP_SECRET>&issuer=Oakly%20Admin",
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
| `423` | `Account temporarily locked. Try again later.` plus a `Retry-After` header | See section 3.4. | 064 |
| `429` | `Too many requests. Try again later.` plus a `Retry-After` header | See section 3.5. | 066 |

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
    "id": "b182c6ed-f1dd-4d24-ab4b-4f0bcb3dee95",
    "email": "verifier-ee5e3e@example.com",
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
| `401` | `Invalid or expired MFA token` | The `mfa_token` is older than 5 minutes, malformed, not an MFA token, or the admin was disabled meanwhile. Restart from the password step. | 006 |
| `423` / `429` | as above | Wrong codes count towards the lockout, and the same per-IP limit applies. | |

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

### 3.4 Lockout
**10 failed attempts in a row lock the account for 15 minutes.** Wrong passwords and wrong codes both count, and a correct password does **not** reset the counter (only a completed sign-in does). The 10th failure still answers `401`; the 11th attempt gets `423`:

<!-- capture: 063-login-failure-10.json response.body -->
```json
{
  "detail": "Invalid email or password"
}
```

<!-- capture: 064-login-locked.json response.body -->
```json
{
  "detail": "Account temporarily locked. Try again later."
}
```

The response header is `Retry-After: 900` (seconds). It is also `423` for the **correct** password while locked:

<!-- capture: 065-login-locked-correct-password.json response.body -->
```json
{
  "detail": "Account temporarily locked. Try again later."
}
```

**UX consequence (must be shown):** show "Account locked. Try again in N minutes" from `Retry-After`, disable the form until then, and do not auto-retry. A Super Admin can lift a lock early with **Reset MFA** (section 8.4). Do not tell the user whether the email exists.

### 3.5 Per-IP rate limit
The sign-in endpoints (`login`, `mfa/verify`, `refresh`) share one limit of **30 calls per minute per IP address**; `invite/accept` has its own. Past the limit the answer is `429` with `Retry-After` (seconds until the window ends). The call is not processed and does not count as a failed login.

<!-- capture: 066-rate-limited.json response.body -->
```json
{
  "detail": "Too many requests. Try again later."
}
```

The limit is per IP, and behind a reverse proxy every admin may share one address, so the proxy setup matters (see the SOP).

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
- **Refresh single-flight.** If two requests hit a `401` at the same time and both call refresh with the same token, the second one looks like theft and signs the admin out everywhere. The console must send **at most one refresh at a time** and make every waiting request reuse its result. With a BFF, serialise on the session.
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
  "id": "b182c6ed-f1dd-4d24-ab4b-4f0bcb3dee95",
  "email": "verifier-ee5e3e@example.com",
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
| `POST`/`PUT`/`DELETE /booking-settings/...` | `pricing.write` |
| `POST`/`PUT`/`DELETE /prescription/settings/...`, `PATCH /prescription/{id}`, `POST /prescription/{id}/upload_note`, `PATCH /prescription/{id}/status` | `prescriptions.manage` |
| `POST`/`PUT`/`DELETE /fallguard/admin/question...`, `GET /fallguard/admin/questions` | `fallguard.write` |
| `GET /patient/admin/refund/requests`, `PUT /patient/admin/refund/requests/{id}` | `refunds.approve` |
| `GET /booking/ws/stats` | `dashboard.read` |
| `GET /admin/admins`, `POST /admin/admins`, `PATCH /admin/admins/{id}`, `POST /admin/admins/{id}/reset-mfa` | `admins.manage` |
| `GET /admin/audit` | `audit.read` |

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
  "email": "verifier-ee5e3e@example.com",
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
  "id": "b182c6ed-f1dd-4d24-ab4b-4f0bcb3dee95",
  "email": "verifier-ee5e3e@example.com",
  "full_name": "Placeholder Verifier",
  "status": "invited",
  "mfa_enabled": false,
  "roles": [
    "verification"
  ],
  "last_login_at": null,
  "locked_until": null,
  "created_at": "2026-10-06T13:09:01.522951",
  "invite_token": "<INVITE_TOKEN>",
  "invite_expires_at": "2026-10-08T13:09:01.521843"
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
  "email": "verifier-ee5e3e@example.com"
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
    "id": "b35b9297-18d5-4e1c-b57e-c30def0a2ceb",
    "email": "boss-ee5e3e@example.com",
    "full_name": "Placeholder Super Admin",
    "status": "active",
    "mfa_enabled": true,
    "roles": [
      "super_admin"
    ],
    "last_login_at": "2026-10-06T13:08:30.514986",
    "locked_until": null,
    "created_at": "2026-10-06T13:07:56.117093"
  },
  {
    "id": "b182c6ed-f1dd-4d24-ab4b-4f0bcb3dee95",
    "email": "verifier-ee5e3e@example.com",
    "full_name": "Placeholder Verifier",
    "status": "active",
    "mfa_enabled": true,
    "roles": [
      "verification"
    ],
    "last_login_at": "2026-10-06T13:09:02.398476",
    "locked_until": null,
    "created_at": "2026-10-06T13:09:01.522951"
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
  "id": "b182c6ed-f1dd-4d24-ab4b-4f0bcb3dee95",
  "email": "verifier-ee5e3e@example.com",
  "full_name": "Placeholder Verifier",
  "status": "active",
  "mfa_enabled": true,
  "roles": [
    "support",
    "verification"
  ],
  "last_login_at": "2026-10-06T13:09:02.398476",
  "locked_until": null,
  "created_at": "2026-10-06T13:09:01.522951"
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
  "id": "b182c6ed-f1dd-4d24-ab4b-4f0bcb3dee95",
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
  "id": "b182c6ed-f1dd-4d24-ab4b-4f0bcb3dee95",
  "email": "verifier-ee5e3e@example.com",
  "full_name": "Placeholder Verifier",
  "status": "active",
  "mfa_enabled": false,
  "roles": [
    "support",
    "verification"
  ],
  "last_login_at": "2026-10-06T13:09:02.398476",
  "locked_until": null,
  "created_at": "2026-10-06T13:09:01.522951"
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
    "id": "90e6dcac-8dac-41e2-841c-0445d60ab444",
    "admin_user_id": "b35b9297-18d5-4e1c-b57e-c30def0a2ceb",
    "admin_email": "boss-ee5e3e@example.com",
    "action": "admin.reset_mfa",
    "entity_type": "admin_user",
    "entity_id": "b182c6ed-f1dd-4d24-ab4b-4f0bcb3dee95",
    "before": null,
    "after": null,
    "reason": null,
    "ip": "172.21.0.1",
    "user_agent": "python-httpx/0.28.1",
    "created_at": "2026-10-06T13:09:03.667082"
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

## 12. Verification table

"Live" means a request/response was captured from the running docker stack (the capture number is given). "Test only" means covered by pytest but not captured. "Not live" means neither could be exercised end to end; the shape or behaviour comes from the named source.

| Behaviour | Status | Evidence |
|---|---|---|
| Login, MFA required shape | Verified live | 004 |
| Login, enrolment-required shape (secret, `otpauth_uri`) | Verified live | 034, 057 |
| MFA verify, success shape with `admin` | Verified live | 007, 035 |
| Wrong code, bad MFA token | Verified live | 005, 006 |
| Replay of a used code refused | Verified live | 009 |
| Wrong password, unknown email, invited and disabled all identical | Verified live | 001, 002, 029, 053 |
| Lockout on the 11th attempt, `Retry-After: 900` | Verified live | 063, 064, 065 |
| Per-IP `429` with `Retry-After` | Verified live | 066 |
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
| Every admin route against every role | Test only | `test_admin_rbac.py` (about 40 routes, 7 roles) |
| Shared routes (booking detail, document file, prescriptions) with an admin token | Test only | `test_admin_rbac.py` and the existing access tests |
| Audit viewer, filters, paging | Verified live | 059, 060, 061 |
| First Super Admin and the Analyst in these captures | **[seeded]** | 004, 043 notes |
| `Retry-After` readable from the browser (CORS) | **[not live]** | The API exposes the header, but the capture stack has no allowed origin and no browser was used; check it once the console runs |
| A real authenticator app scanning the QR code | **[not live]** | the captures generate codes with the same TOTP algorithm (`pyotp`), which is what authenticator apps implement |
