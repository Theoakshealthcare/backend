# Plan: Admin Console v1 (Phase A)

| | |
|---|---|
| **Date** | 2026-10-06 |
| **Goal** | Oakly staff can run the marketplace from a secure web console: approve caregivers, watch and fix bookings, pay caregivers, handle refunds, and set prices. Every action is audited. |
| **Spec** | [Admin Console PRD](../prd/PRD-admin-console.md). Requirement IDs `AD-FR-n` refer to it. |
| **Repos** | Backend: this repo (admin APIs under `/api/v1/admin/*`). Console: frontend monorepo `apps/admin`, Next.js. |
| **Environment** | There is no server. Everything runs and is tested locally: the Docker stack, the console with `pnpm dev`, and e2e against the local API. |
| **Open founder decisions** | Payout share, hold period, payout cadence and minimum, and the Finance refund limit are **settings with the PRD's proposed defaults** (A1–A4 in PRD §14), so they can change without code. |

## Ground rules
These are the same rules as Phase 0 and Phase C:
- One PR per work package, merged after CI passes.
- Write tests first.
- Reproduce CI locally before pushing.
- No AI attribution in commits or PRs.
- An SOP for each package, and the master checklist kept current.
- **Every backend endpoint the console uses goes into an FE integration guide** (`docs/fe-integration-guide-admin.md`). Its examples are generated from live captures, the same way as the caregiver guide (`scripts/render_fe_guide.py`, CI `--check`).
- Auth, money and permission code gets an Opus review.
- **Contract first:** the console is built only against `@oakly/api-client` types generated from the backend.

## A0: Admin identity, MFA and permissions (backend; branch `feat/admin-auth`)
Admins are their own accounts, separate from patient and caregiver logins (AD-FR-81–85).

**Tables**
- `admin_users`: email (unique), full name, `password_hash`, `mfa_secret_enc` (Fernet), `mfa_enabled`, `status` (`invited` | `active` | `disabled`), `last_login_at`, `created_by`, `failed_logins`, `locked_until`.
- `admin_roles`, `admin_role_permissions`, `admin_user_roles`.
- `admin_refresh_tokens`: a hashed token per session, so tokens can rotate and be revoked.
- The old `admins` table is dropped. There is no data.

**Roles and permissions:** seeded from the PRD §8.2 catalogue. The roles are Super Admin, Operations, Verification, Finance, Support, Clinical Reviewer and Read-only Analyst.

**Auth endpoints** (contract; the console builds against exactly this):

| Method and path | Body | Result |
|---|---|---|
| `POST /api/v1/admin/auth/login` | `{email, password}` | `200 {"mfa_required": true, "mfa_token": "<5-min token>"}`. If MFA isn't enrolled yet: `{"mfa_enrollment_required": true, "mfa_token", "otpauth_uri", "secret"}`. Bad credentials: `401 {"detail": "Invalid email or password"}`. After 10 failures: `423` with `Retry-After`. |
| `POST /api/v1/admin/auth/mfa/verify` | `{mfa_token, code}` | `200 {"access_token", "refresh_token", "token_type": "bearer", "expires_in": 900, "admin": {id, email, full_name, roles[], permissions[]}}`. Enrollment is completed on the first successful verify. A wrong code returns `401`. |
| `POST /api/v1/admin/auth/refresh` | `{refresh_token}` | A new token pair. The old refresh token is revoked; reusing it revokes every session of that admin. |
| `POST /api/v1/admin/auth/logout` | `{refresh_token}` | `204` |
| `GET /api/v1/admin/me` | | `{id, email, full_name, roles[], permissions[]}` |
| `POST /api/v1/admin/admins` | `{email, full_name, roles[]}` (needs `admins.manage`) | `201`, returns a single-use invite link token, valid 48 h. |
| `POST /api/v1/admin/auth/invite/accept` | `{invite_token, password}` | Then log in. MFA enrollment is forced on first login. |
| `GET /api/v1/admin/admins` | | List admins |
| `PATCH /api/v1/admin/admins/{id}` | `{roles?, status?}` (needs `admins.manage`) | Updates roles or status. The last Super Admin can't be disabled. |
| `POST /api/v1/admin/admins/{id}/reset-mfa` | (needs `admins.manage`) | Forces MFA enrollment again |

**Tokens.** Admin JWTs carry `aud="admin"`, `type="access"` and the admin's ID. They last 15 minutes, with a 30-minute idle limit enforced through refresh. App tokens are rejected on admin routes, and admin tokens on app routes.

**One permission check.** `require_permission("caregivers.approve")` replaces `get_current_admin` on **every** existing admin route:
- caregiver approval;
- FallGuard admin;
- booking-settings and prescription-settings writes;
- the refund admin routes;
- `/booking/ws/stats`;
- admin reads of bookings and documents.

The audit log records `admin_user_id` from `admin_users`.

**Bootstrap.** `python -m scripts.create_super_admin --email ...` reads the password from a prompt or the `OAKLY_BOOTSTRAP_PASSWORD` environment variable, and forces MFA on first login.

**Rate limits.** Login and MFA are limited per IP and per account; the counters live in Postgres, and Redis is optional.

**Tests:**
- every route against every role (allowed vs 403);
- app token used on an admin route, and the reverse;
- refresh rotation and reuse detection;
- lockout;
- an MFA replay inside the same 30-second window is refused.

⚠️ An Opus review is required.

## A1: Console scaffold and caregiver verification (frontend `apps/admin`; branch `feat/admin-console`)
- **Stack:**
  - Next.js 16 (App Router), React 19.2.3 (the same single React as the Expo app), TypeScript strict.
  - Tailwind with shadcn/ui on Radix, and TanStack Query.
  - `@oakly/api-client` in **browser mode**, where the session token lives in memory.
  - The refresh token is held server-side, in an httpOnly cookie, via a tiny Next route handler (BFF) that calls `/admin/auth/refresh`. The browser never stores it in JS-readable storage.
- **Screens:**
  - Login → MFA (with an enrollment screen that shows the QR code from `otpauth_uri`).
  - App shell: left navigation grouped as in the PRD §9, items hidden when the role lacks the permission, a session timeout warning, and an admin menu with logout.
  - **Overview**, a placeholder until A2/A3 add data.
  - **Caregiver applications queue** (AD-FR-7): filters, sorted by age.
  - **Application detail** (AD-FR-8–14):
    - the profile, and consents with timestamps;
    - documents shown inline through the existing authorized download route;
    - the document, identity and **background-check** decisions;
    - the skills editor;
    - approve, which shows the 409 "unmet" list inline;
    - reject, request changes, suspend and reinstate, each with a confirmation dialog and a required reason;
    - the caregiver's audit history.
  - **Admin users** (AD-FR-83): invite, edit roles, disable, reset MFA.
  - **Audit log viewer**, if `GET /admin/audit` lands in A0. Otherwise it moves to A5.
- **Tests:** component tests (Vitest + Testing Library) for the auth flow, permission gating and the approve-with-unmet display. A Playwright e2e test runs against the local Docker backend for login → MFA (a TOTP generated in the test) → approve a seeded caregiver.

## A2: Bookings operations (backend + console)
Covers AD-FR-19–27:
- the bookings list with filters and search;
- booking detail with its timeline (status changes, dispatch waves, notifications, payments);
- the **unmatched queue** (`dispatch_state='unmatched'`, and paid bookings unaccepted past the SLA);
- re-dispatch, assign, reassign and cancel with a reason code and refund choice, plus a Super Admin-only status correction that triggers the normal side effects;
- no-show flags.

Every action is audited.

## A3: Money (backend + console)
⚠️ An Opus review is required.
- **Earnings credited on completion** (AD-FR-38). Today caregivers are never credited. On completion, an `earning` transaction of `service_fee × PAYOUT_SHARE` (default 0.80) is created as `pending_clearance` and becomes available after `EARNING_HOLD_HOURS` (default 24). This is a worker job.
- **Caregiver ledger view and payout batches through Paystack Transfers** (AD-FR-39–44). This means transfer recipients, a bulk transfer and the `transfer.*` webhooks. It's tested with a mocked Paystack, plus Paystack test mode locally. The minimum and cadence are settings.
- **Refunds v2** (AD-FR-32–37):
  - a refund queue fed by bookings with `refund_required`, and by patient requests;
  - each refund goes to exactly one destination;
  - the Finance approval limit is a setting;
  - a reconcile action resolves refunds stuck in `processing`.
- **Payments list and webhook exceptions** (AD-FR-28–30).

## A4: Pricing and settings (backend + console)
- **Versioned price books** (AD-FR-51–54): preview, then publish. Every service type needs a multiplier. They replace "latest row wins".
- **Service areas** (Lagos and Abuja) and form options (AD-FR-55–56).
- **Policies:** cancellation, SLA and payment TTL, as audited config (AD-FR-57).

## A5: People, audit and exports (backend + console)
- **People search** with masked views and suspend/reactivate (AD-FR-45–48).
- **Break-glass clinical access** (AD-FR-73–74): a reason is required, access is time-boxed, and every use is audited.
- **Audit viewer and CSV exports**, with masking and audit entries of their own (AD-FR-85, 88).

## Later (with the WhatsApp channel)
- Support inbox (AD-FR-59–64).
- WhatsApp ops (AD-FR-75–78).
- Ratings and quality (AD-FR-65–67).

## Order
1. **A0 (backend) and A1 (console) in parallel.** A1 builds against the A0 contract above. Once A0 merges, the API client is synced and A1 wires real auth.
2. Then A2, A3, A4 and A5. Each is a backend PR followed by a console PR. A3 can run alongside A2.
