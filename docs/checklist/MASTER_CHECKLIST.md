# Oakly — Master Build Checklist

This covers all three repos: `backend` (FastAPI), `patient` (Expo), and `caregiver` (Expo). The backend is the hub, so the master checklist lives here.

**Update (2026-10-06):** Phase 0 is done. Phase C (caregiver app) is code-complete: C0–C4 merged. A device/Simulator run is pending a newer Xcode, and real push needs an EAS project. See section 8.

**Snapshot (2026-09-30):**
- **Done:** 1 foundation item (Poetry migration, #1).
- **In progress:** the critical fixes track.
- **Specified (PRDs in #2):** the WhatsApp patient channel and the Admin Console.
- **Planned:** SDK upgrades and a shared package.

**Baseline:** the full review of all three repos on 2026-09-24 (it predates this file).

**Legend:**
- `[x]` done and verified
- `[ ]` not done
- ⚠️ critical: security or money

---

## 0. Foundation / tooling — 🟡 in progress
- [x] Backend on Poetry (`pyproject.toml` + `poetry.lock`), with Docker updated — 2026-09-29, branch `chore/poetry-migration`, see [SOP](../sop/2026-09-29-poetry-migration.md)
- [x] Backend CI: lock check, bug-class lint, app smoke test — 2026-09-29
- [x] Backend `.gitignore` — 2026-09-29
- [ ] Merge `chore/poetry-migration` (the deploy target is Docker)
- [x] pytest harness: throwaway Postgres (testcontainers), factories, network guard, run in CI. 2026-10-05, branch `test/harness`, [SOP](../sop/2026-10-05-test-harness.md)
- [x] Phase 0 WP-A–D merged to `main` (harness, secrets, access control, money). 2026-10-05, PRs #3–#8
- [ ] Phase 0 WP-E: [plan](../plans/2026-10-05-phase-0-security-foundations.md#wp-e-foundations-so-a-booking-can-complete-branches-from-main-which-now-includes-wpad)
  - [x] E2 money in kobo. 2026-10-05, branch `foundations/kobo`, migration `68b8941cfc28`, [SOP](../sop/2026-10-05-money-in-kobo.md)
  - [x] E5 caregiver approval API (mandatory background check) + audit log. 2026-10-05, `foundations/caregiver-approval`, migration `2e543a04ee5d`, [SOP](../sop/2026-10-05-caregiver-approval.md)
  - [x] E3 Redis + ARQ worker, unpaid-booking expiry. 2026-10-05, `foundations/dispatch`, migration `9b1d4e7a3c52`, [SOP](../sop/2026-10-05-jobs-and-dispatch.md)
  - [x] E4 skills-based matching + dispatch waves. 2026-10-05, `foundations/dispatch`, same migration and SOP
  - [x] E1 migration baseline: one migration `0001_baseline`, built from the models; the test harness uses it. 2026-10-05, [SOP](../sop/2026-10-05-migration-baseline.md). No production data exists, so there was nothing to preserve.
- [ ] Clean the ruff backlog, then enforce the full ruleset and `ruff format`
- [ ] Python 3.11 → 3.12 or 3.13
- [ ] Library swaps: passlib → pwdlib · python-jose → PyJWT · google-generativeai → google-genai

## 1. Secrets & repo hygiene — 🔴 critical, not started
- [ ] ⚠️ **Owner:** rotate every secret in the committed backend `.env` ([runbook](../sop/2026-10-05-secrets-and-rotation.md)): JWT secret, SMTP, Gemini, Paystack keys and webhook secret, Cloudinary, Supabase DB password
- [x] Re-encryption script for vitals and FallGuard data with multi-key support. 2026-10-05, `security/secrets`, [SOP + runbook](../sop/2026-10-05-secrets-and-rotation.md)
- [x] `.env` untracked; configuration comes from the server env file (Docker). 2026-10-05, `security/secrets`
- [x] Removed the hardcoded DB password and credentials from the code; required settings fail fast. 2026-10-05, `security/secrets`
- [ ] ⚠️ Patient app: remove the committed release keystore and its passwords (`android/gradle.properties`), and decide whether to reset the Play upload key
- [ ] Decide on a history rewrite (`git filter-repo`) for each repo
- [x] Backend `__pycache__` files untracked. 2026-10-05
- [ ] Untrack the caregiver app's `.env`

## 2. Backend security & money fixes — 🔴 critical, not started
- [x] ⚠️ Authenticate the booking WebSockets (they currently use hardcoded demo users) — `security/access-control`, 2026-10-05
- [x] ⚠️ Require auth and ownership checks on the prescription endpoints — `security/access-control`, 2026-10-05
- [ ] ⚠️ Admin approval for caregiver identity; stop accepting the verification result from the client
- [x] ⚠️ Make the caregiver document uploads private (they're under the public `/uploads` mount) — 2026-10-05, `security/access-control` (no legacy files to migrate)
- [x] ⚠️ Reject refresh tokens used as access tokens (check the `type` claim) — `security/access-control`, 2026-10-05
- [x] ⚠️ Validate that withdrawal amounts are positive, and lock the wallet row — 2026-10-05, `security/money`
- [x] ⚠️ Fix refund approval paying twice (wallet credit plus a Paystack refund) — 2026-10-05, `security/money`
- [x] ⚠️ Make the Paystack webhook safe to replay, and check the paid amount against the booking — 2026-10-05, `security/money` (also `verify-payment`)
- [x] Wallet transactions: remove the nested `db.begin()` — 2026-10-05, `security/money`
- [ ] Password reset: fix the email arguments that cause a 500, and build a real reset link
- [ ] Vitals access grant: stop comparing a user ID with a patient ID; use the `accepted` status
- [x] Only the booking's own caregiver can cancel it or change its state; only paid bookings can be accepted — `security/access-control`, 2026-10-05
- [x] Public diagnostics removed: `/booking/ws/stats` is admin-only, `/auth/test-cloudinary` deleted — 2026-10-05
- [x] `GET /caregiver/verification-status` returned 500 instead of 403/404 — 2026-09-29
- [ ] Rate limiting on login, OTP and password recovery
- [ ] Fix the Alembic migration graph (two roots) so `upgrade head` works on an empty DB
- [ ] Share WebSocket state across workers (Redis), or run a single worker
- [ ] Move document storage off the local disk (Cloudinary or S3)

## 3. Patient app — 🟠 partial
- [x] Auth: login, register, verify email, profile edit
- [x] Booking: preview, create, Paystack pay, list, details
- [x] FallGuard: questions, submit, result, history
- [x] Vitals: manual entry, list, history, CSV export
- [ ] ⚠️ Cancel booking calls an endpoint that doesn't exist
- [ ] Profile edit clears `user.patient`, which silently breaks vitals
- [ ] Token refresh can deadlock on a 401
- [ ] Vitals insights and caregiver-access response shapes don't match the backend
- [ ] Password reset flow, delete account, full logout
- [ ] Replace mocks: home, notifications, chat, AI note taker, tracking map, wallet, prescriptions
- [ ] Remove about 4.5k lines of dead code (`FallguardAssessment1–24`, `VitalDetails`, old hooks)
- [ ] Move tokens to `expo-secure-store`; base URL from `EXPO_PUBLIC_API_URL`
- [ ] Expo SDK 53 → 57, one SDK at a time; new architecture on; MMKV 4

## 4. Caregiver app — 🔴 prototype
- [ ] Expo SDK 53 → 57 (before the bug fixes); `expo-av` → `expo-audio`
- [ ] Login navigation after the first success; session restore and auth gate; token refresh
- [ ] Setup step 1 sends `house_address` but the backend expects `address`
- [ ] Service calls: throw on failure, or check `.success`; unwrap `.data`
- [ ] Bookings status filter; register the `BookingDetails`, chat and notes screens
- [ ] Real Home: earnings, wallet, availability, job requests
- [ ] Identity verification against the real backend flow

## 5. Cross-repo — ⚪ planned
- [ ] Shared package: UI kit, theme, auth store, API client (about 30 files are duplicated today)
- [ ] API types generated from `/api/v1/openapi.json`
- [ ] Single source for docs: the backend `docs/` replaces the copied `*_INTEGRATION_GUIDE.md` files
- [ ] FE integration guides rebuilt from captured live responses

## 6. WhatsApp patient channel — 📝 specified, not started
Spec: [PRD](../prd/PRD-whatsapp-patient-channel.md) · [proposal](../proposals/2026-09-29-whatsapp-patient-channel.md). The IDs below refer to requirements in the PRD.
- [ ] **Launch prerequisites (PRD §6):**
  - [ ] Caregiver approval flow exists (P1, see §7 below)
  - [ ] Matching uses structured skills (P2)
  - [ ] Caregiver app reports location (P3)
  - [ ] Dispatch waves and timeout (P4)
  - [ ] Paystack webhook safe to replay and checks the amount (P5)
  - [ ] Scheduler, including expiry of unpaid bookings (P6)
  - [ ] Bookings no longer require an email (P7)
  - [ ] Caregiver actions check ownership (P8)
  - [ ] Critical security items closed (P9)
  - [x] Money stored in kobo (P10). 2026-10-05, E2
  - [ ] Meta business verified (P11)
- [ ] **Meta onboarding:** business portfolio, verification, dedicated number, display name, WhatsApp Business Account, payment method, templates (PRD §13–14)
- [ ] **Infrastructure:**
  - [ ] Redis
  - [ ] Worker queue and scheduler
  - [ ] Non-sleeping hosting plan
- [ ] **Data model:** contacts, conversations, message metadata, care recipients and relationships, consents, quotes, dispatch waves, ratings, templates (PRD §10)
- [ ] **Webhook, Flow data endpoint and outbound sender** using PyWa (WA-FR-1–4, §11)
- [ ] **Consent and opt-out** (WA-FR-5–9)
- [ ] **Menu and navigation** (WA-FR-10–12)
- [ ] **Booking Flow and location request** (WA-FR-13–17)
- [ ] **Quote with 30-minute price lock** (WA-FR-18–21)
- [ ] **Paystack link, expiry and webhook confirmation** (WA-FR-22–27)
- [ ] **Matching and assignment notifications** (WA-FR-28–31)
- [ ] **Visit lifecycle notifications and visit code** (WA-FR-32–37)
- [ ] **My bookings and cancellation policy** (WA-FR-38–39)
- [ ] **Emergency screen, human handover, no-medical-advice guard** (WA-FR-40–42)
- [ ] **Web companion:** magic links, step-up authentication, tracking and booking pages (WA-FR-43–48)
- [ ] **Phase 2:**
  - [ ] FallGuard via a WhatsApp Flow, scored on the server (WA-FR-49–51)
  - [ ] Vitals reminders (WA-FR-52)
  - [ ] Recipient consent and care circle (WA-FR-53–55)
  - [ ] Pidgin
  - [ ] Abuja launch
- [ ] **Phase 3:** doctor teleconsultation (WA-FR-56–58)
- [ ] **Compliance:** impact assessment (DPIA), privacy notice, data protection officer, NDPC registration check, retention periods (WA-NFR-9–10)

## 7. Admin Console — 📝 specified, not started
Spec: [PRD](../prd/PRD-admin-console.md). v1 must ship before the WhatsApp closed pilot.
- [x] **Admin auth:** separate admin identity, TOTP MFA, a single `require_permission` check, roles and permissions, invite flow, bootstrap CLI. (AD-FR-81–84) Done in A0, see section 9. `create_test_admin.py` and the default credentials were already removed in Phase 0.
- [x] **Append-only audit log and audit viewer** (AD-FR-85): the log was Phase 0; the viewer API `GET /admin/audit` is A0. The console screen is A1.
- [ ] **Overview:** KPIs, items needing attention, current supply (AD-FR-1–6)
- [ ] **Caregiver verification:** queue, detail, document, identity and background decisions, approve/reject, structured skills, suspend (AD-FR-7–14)
- [ ] **Private document storage:** signed URLs, migrate existing files, remove the public `/uploads` access (AD-FR-15–18)
- [ ] **Bookings:** list and detail, unmatched queue, assign/reassign, admin cancel, status correction (AD-FR-19–25)
- [ ] **Payments:** payment list, webhook exceptions, daily reconciliation (AD-FR-28–31)
- [ ] **Refunds v2:** exactly one destination per refund, approval limits, refund webhooks, notifications (AD-FR-32–36)
- [ ] **Caregiver pay:** earnings credited on completion, ledger, Paystack Transfer payout batches, validated withdrawals (AD-FR-38–42)
- [ ] **People:** search, masked view, suspend, consent records (AD-FR-45–48)
- [ ] **Pricing:** versioned price books with preview and publish; service areas; Nigerian form options (AD-FR-51–56)
- [ ] **Support inbox:** WhatsApp handovers, 24-hour window enforcement, urgent alerts (AD-FR-59–63)
- [ ] **Ratings list and low-rating review tasks** (AD-FR-65)
- [ ] **FallGuard question editor with versioning;** remove the admin calls from the patient app (AD-FR-68–69)
- [ ] **Break-glass clinical access, with every access audited** (AD-FR-73–74)
- [ ] **WhatsApp ops:** templates, number health, opt-outs (AD-FR-75–77)
- [ ] **CSV exports with masking** (AD-FR-88)
- [ ] **v1.1:** partial refunds, scheduled payouts, city pricing, prescription ops, quality card, charts, live map
- [ ] **v2:** clinicians and MDCN checks, pharmacy partners (AD-FR-79–80, 72)

## 8. Phase C: Caregiver app (supply side) — ✅ code complete (device verification pending)
Plan: `docs/plans/2026-10-05-caregiver-app.md` in the caregiver monorepo. Goal: nurses and caregivers sign up, get approved, go available, receive offers by push, accept, and run the visit.
- [x] **C0 Monorepo scaffold**: pnpm workspace, `packages/api-client`, CI. Merged 2026-10-06, caregiver#1
- [x] **C1 Expo SDK 57 upgrade and cleanup**: continuous native generation, unused dependencies removed, `expo-secure-store`, typecheck/lint/test in CI. Merged 2026-10-06, caregiver#2. ⚠️ The iOS Simulator run is pending a newer Xcode (26.2 can't build SDK 57).
- [x] **C3 Backend support for the app**: push tokens and job-offer push, availability, `on_my_way` and the visit code, `GET /caregiver/home`, offer-view redaction, FE guide generated from live captures. Merged 2026-10-06, backend#15, [SOP](../sop/2026-10-05-caregiver-mobile-support.md), migration `0002_caregiver_mobile`.
  - [x] 1. Push tokens: `push_tokens`, `POST`/`DELETE /devices/push-token`, `PushService` (Expo, batches, prunes `DeviceNotRegistered`, mocked in tests)
  - [x] 2. Job offers by push from dispatch waves (after commit, best effort, no patient data)
  - [x] 3. Availability: `POST /caregiver/availability` (approved only; going unavailable stops new offers)
  - [x] 4. Visit lifecycle: `en_route` status, `on_my_way`, 4-digit visit code (encrypted; `start` requires it; 3 wrong tries lock it for 15 minutes). Partly covers WA-FR-34/35 (the WhatsApp message with the code is not built)
  - [x] 5. `GET /caregiver/home`
  - [x] 6. [FE integration guide](../fe-integration-guide-caregiver.md) with live captures (`docs/fe-captures/caregiver/`, `scripts/capture_caregiver_flow.py`)
  - [x] 7. One Alembic migration on top of `0001_baseline`
  - [x] 8. Opus review (approve-with-fixes; offer-view, ping-availability and suspension fixes applied), merged as backend#15
- [x] **C2 Core flows on the typed client**: React Query, single-flight refresh, auth gate, onboarding and verification status, bookings and offers (offer view only), 51 tests. Merged 2026-10-06, caregiver#3. The review also fixed an api-client retry bug for requests with a body.
- [x] **C4 Supply features**: availability toggle, foreground location (and background only from on my way until complete; pings never carry `is_available`), serialised tracking controller, push registration and tap-to-open, visit flow with code and lockout, live Home on `/caregiver/home`. 168 tests. Merged 2026-10-06, caregiver#4
  - [ ] Verify on a device or Simulator against a local backend (blocked: Xcode 26.2 can't build SDK 57)
  - [ ] Real push: EAS project + `projectId`, APNs/FCM credentials (owner)
- Launch prerequisite P3 (caregiver app reports location) is met in code by C4; it still needs device verification.

## 9. Phase A: Admin Console v1 — 🟡 starting
Plan: [`docs/plans/2026-10-06-admin-console.md`](../plans/2026-10-06-admin-console.md). PRD: [`docs/prd/PRD-admin-console.md`](../prd/PRD-admin-console.md).
- [x] **A0** Admin identity: separate `admin_users`, TOTP MFA, roles and permissions, `require_permission` on every admin route, invites, bootstrap CLI, rate limits and lockout. 2026-10-06, branch `feat/admin-auth` (not yet merged), migration `0003_admin_identity`, [SOP](../sop/2026-10-06-admin-auth.md), [FE guide](../fe-integration-guide-admin.md). Opus review findings fixed 2026-10-06 (trusted proxies, password throttle vs MFA lock, single-use `mfa_token`, logout/refresh race); see the SOP addendum.
  - [x] 1. Tables: `admin_users`, `admin_roles`, `admin_role_permissions`, `admin_user_roles`, `admin_refresh_tokens`, `admin_rate_limits`; old `admins` dropped; audit and approval columns re-pointed at `admin_users`
  - [x] 2. Roles and 33 permissions seeded from PRD 8.2 (code catalogue + frozen copy in the migration, drift test)
  - [x] 3. Admin JWT (`aud=admin`, 15 minutes) and rotating refresh tokens with reuse detection, 30-minute idle timeout, 12-hour cap
  - [x] 4. TOTP MFA (Fernet-encrypted secret, forced enrolment, replay refusal per time step)
  - [x] 5. Login lockout (10 failures, 423 + `Retry-After`) and per-IP rate limit in Postgres
  - [x] 6. `require_permission` on every existing admin route (caregivers, booking settings, prescription settings and writes, FallGuard, refund admin, `ws/stats`); shared booking, document and prescription reads accept an admin token with the permission
  - [x] 7. Invites (single use, 48 hours), admin list, role and status changes, MFA reset, last-Super-Admin guard
  - [x] 8. `GET /admin/audit` (filter by entity, admin, action, date; paginated)
  - [x] 9. Bootstrap CLI `python -m scripts.create_super_admin`
  - [x] 10. Tests: permission matrix (about 40 routes x 7 roles), token audience both ways, MFA replay, refresh reuse, lockout, invite, last Super Admin (with a race test), CLI, migration
  - [x] 11. FE guide from live captures (`scripts/capture_admin_flow.py`, `docs/fe-captures/admin/`), `--check` in CI
  - [x] 12. Opus review (auth and permission code): 1 high, 1 medium, 4 low findings fixed with tests, 2026-10-06
  - [ ] 13. Final read of the review-fix diff before merge
- [ ] **A1** Console scaffold (`apps/admin`, Next.js 16): login and MFA, permission-gated shell, caregiver queue and detail with all decisions, admin users
- [ ] **A2** Bookings ops: list and detail with timeline, unmatched queue, assign, reassign, cancel, status correction
- [ ] **A3** Money: earnings credited on completion, caregiver ledger, Paystack Transfer payouts, refunds v2 queue (`refund_required`), payments and webhook exceptions
- [ ] **A4** Pricing: versioned price books, service areas, form options, policies
- [ ] **A5** People (masked), break-glass clinical access, audit viewer, CSV exports
- [ ] **FE integration guide for admin** (`docs/fe-integration-guide-admin.md`), generated from live captures. A0 part done (sign-in, admin management, audit, permissions); extend it with each later package

## Backlog / upcoming
- [ ] **Founder decisions** for WhatsApp (PRD §16, D1–D11) and the Admin Console (PRD §14, A1–A10), including caregiver payout share, city pricing and the refund approval limit
- [x] Push notifications for caregiver job offers (Expo). 2026-10-05, C3, [SOP](../sop/2026-10-05-caregiver-mobile-support.md). Patient-side pushes and WhatsApp are not built.
- [ ] Medical records and family members endpoints (tables exist, no API)

## Deferred follow-ups
- [x] `patient.py` admin refund routes checked `user_type != "admin"` (never true). Fixed: they use `require_permission("refunds.approve")` (A0)
- [ ] Chat and notes routes in `endpoints/booking.py` don't let admins in; decide whether support staff should read them (an admin token is rejected there since A0, as an app token was before)
- [ ] (A0) Admin invites are not emailed: the API returns the token and the console shows the link. Add an email, and an endpoint to re-issue an expired invite (today an expired invite blocks that email)
- [ ] (A0) MFA recovery codes, and an optional IP allowlist for the console (AD-FR-87)
- [ ] (A0) Admin access to the live-tracking WebSocket (admins used to reach it with an app token; the console gets its own booking views in A2)
- [x] (A0) Per-IP limits and audit `ip` honour `TRUSTED_PROXIES`; set it to the reverse proxy's address when one is deployed
- [ ] (A0) Admin `POST /prescription/create` is user-only now (an admin used to pass the patient-ownership check); prescription operations move to the console in v1.1
- [ ] Upgrade Poetry to 2.5.x everywhere (local, Docker, CI)
- [ ] Docker image size is 828 MB, mostly gRPC and Google libraries; revisit after the `google-genai` swap
- [ ] (C3) `GET /booking/caregiver/bookings?status=new_requests` and `/booking/caregiver/notifications` expose condition, instructions, address and patient contact to caregivers who have not accepted; trim them to the offer fields
- [ ] (C3) Type the caregiver action response (it is a raw dict with money as strings and internal fields); store `reason` on decline/cancel
- [ ] (C3) Move the Expo push off the settlement request onto the ARQ worker if it ever slows the Paystack webhook; poll Expo receipts to prune late-failing tokens
