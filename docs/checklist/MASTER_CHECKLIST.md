# Oakly — Master Build Checklist

This covers all three repos: `backend` (FastAPI), `patient` (Expo), and `caregiver` (Expo). The backend is the hub, so the master checklist lives here.

**Update (2026-10-07):** Backend PRs #1–#32 and caregiver monorepo PRs #1–#11 are merged. Done since the last roll-up: admin A0 (#21) and A1 console (monorepo #6); tech debt (#23); WhatsApp W1–W3 (#22, #24, #27), default multipliers and geocoding (#28), drop-out re-dispatch with two-tier fees and the booking policy (#30), caregiver cancel window (#32); A2 bookings ops API (#26, backend only); account deletion as anonymisation (#29); pricing settings audit (#31); caregiver app account, wallet, notifications and directions screens (monorepo #7–#11). Migrations `0001`–`0010` are on `main`. Still open: A2 console screens, A3–A5, W4–W5, the remaining caregiver app screens (section 8), and the owner and device items.

**Snapshot (2026-10-07):** 
- **Items:** 160 done (`[x]`), 127 open (`[ ]`), counted over every task line.
- **Sections done:** 8 (Phase C, code complete, device run pending), 11 (tech debt), 12 (account deletion), 13 (pricing audit).
- **Sections in progress:** 0, 6, 7, 9 (A0, A1 and the A2 API done), 10 (W1–W3b done).
- **Sections planned or not started:** 1 and 2 (open items are mostly the owner's secret rotation and the remaining hardening list), 3–5, plus A2 screens, A3–A5, W4 and W5.

**Baseline:** the full review of all three repos on 2026-09-24 (it predates this file).

**Legend:**
- `[x]` done and verified
- `[ ]` not done
- ⚠️ critical: security or money

---

## 0. Foundation / tooling — 🟡 in progress
- [x] Backend on Poetry (`pyproject.toml` + `poetry.lock`), with Docker updated — 2026-09-29, #1, see [SOP](../sop/2026-09-29-poetry-migration.md)
- [x] Backend CI: lock check, bug-class lint, app smoke test — 2026-09-29
- [x] Backend `.gitignore` — 2026-09-29
- [x] Merge `chore/poetry-migration` (the deploy target is Docker). 2026-10-05, #1
- [x] pytest harness: throwaway Postgres (testcontainers), factories, network guard, run in CI. 2026-10-05, #3, [SOP](../sop/2026-10-05-test-harness.md)
- [x] Phase 0 WP-A–D merged to `main` (harness, secrets, access control, money). 2026-10-05, PRs #3–#8
- [x] Phase 0 WP-E (E1–E5 all merged, #11–#14, 2026-10-05): [plan](../plans/2026-10-05-phase-0-security-foundations.md#wp-e-foundations-so-a-booking-can-complete-branches-from-main-which-now-includes-wpad)
  - [x] E2 money in kobo. 2026-10-05, #11, migration `68b8941cfc28`, [SOP](../sop/2026-10-05-money-in-kobo.md)
  - [x] E5 caregiver approval API (mandatory background check) + audit log. 2026-10-05, #12, migration `2e543a04ee5d`, [SOP](../sop/2026-10-05-caregiver-approval.md)
  - [x] E3 Redis + ARQ worker, unpaid-booking expiry. 2026-10-05, #13, migration `9b1d4e7a3c52`, [SOP](../sop/2026-10-05-jobs-and-dispatch.md)
  - [x] E4 skills-based matching + dispatch waves. 2026-10-05, #13, same migration and SOP
  - [x] E1 migration baseline: one migration `0001_baseline`, built from the models; the test harness uses it. 2026-10-05, [SOP](../sop/2026-10-05-migration-baseline.md). No production data exists, so there was nothing to preserve.
- [ ] Clean the ruff backlog, then enforce the full ruleset and `ruff format`
- [x] Python 3.11 → 3.13. 2026-10-06, #23, [SOP](../sop/2026-10-06-tech-debt.md)
- [x] Library swaps: passlib → pwdlib · python-jose → PyJWT · google-generativeai → google-genai. 2026-10-06, same SOP

## 1. Secrets & repo hygiene — 🟡 in progress (code fixes merged; key rotation is an owner action)
- [ ] ⚠️ **Owner:** rotate every secret in the committed backend `.env` ([runbook](../sop/2026-10-05-secrets-and-rotation.md)): JWT secret, SMTP, Gemini, Paystack keys and webhook secret, Cloudinary, Supabase DB password
- [x] Re-encryption script for vitals and FallGuard data with multi-key support. 2026-10-05, `security/secrets`, [SOP + runbook](../sop/2026-10-05-secrets-and-rotation.md)
- [x] `.env` untracked; configuration comes from the server env file (Docker). 2026-10-05, `security/secrets`
- [x] Removed the hardcoded DB password and credentials from the code; required settings fail fast. 2026-10-05, `security/secrets`
- [ ] ⚠️ Patient app: remove the committed release keystore and its passwords (`android/gradle.properties`), and decide whether to reset the Play upload key
- [ ] Decide on a history rewrite (`git filter-repo`) for each repo
- [x] Backend `__pycache__` files untracked. 2026-10-05
- [ ] Untrack the caregiver app's `.env`

## 2. Backend security & money fixes — 🟡 in progress (most fixes merged; remaining items below)
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

## 3. Patient app — ⏸️ paused (patients use WhatsApp, see sections 6 and 10)
- [x] Auth: login, register, verify email, profile edit
- [x] Booking: preview, create, Paystack pay, list, details
- [x] FallGuard: questions, submit, result, history
- [x] Vitals: manual entry, list, history, CSV export
- [ ] ⚠️ Cancel booking calls an endpoint that doesn't exist
- [ ] Profile edit clears `user.patient`, which silently breaks vitals
- [ ] Token refresh can deadlock on a 401
- [ ] Vitals insights and caregiver-access response shapes don't match the backend
- [ ] Password reset flow, full logout. Delete account: the backend is fixed (section 12); the apps still need the screen and the 409 message
- [ ] Replace mocks: home, notifications, chat, AI note taker, tracking map, wallet, prescriptions
- [ ] Remove about 4.5k lines of dead code (`FallguardAssessment1–24`, `VitalDetails`, old hooks)
- [ ] Move tokens to `expo-secure-store`; base URL from `EXPO_PUBLIC_API_URL`
- [ ] Expo SDK 53 → 57, one SDK at a time; new architecture on; MMKV 4

## 4. Caregiver app (old prototype list) — ➡️ superseded by section 8
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

## 6. WhatsApp patient channel — 🟡 in progress (W1–W3b merged; see section 10)
Spec: [PRD](../prd/PRD-whatsapp-patient-channel.md) · [proposal](../proposals/2026-09-29-whatsapp-patient-channel.md). The IDs below refer to requirements in the PRD.
- [ ] **Launch prerequisites (PRD §6):**
  - [x] Caregiver approval flow exists (P1). 2026-10-05, E5
  - [x] Matching uses structured skills (P2). 2026-10-05, E4
  - [ ] Caregiver app reports location (P3). Met in code by C4; needs device verification
  - [x] Dispatch waves and timeout (P4). 2026-10-05, E4
  - [x] Paystack webhook safe to replay and checks the amount (P5). 2026-10-05, WP-D
  - [x] Scheduler, including expiry of unpaid bookings (P6). 2026-10-05, E3
  - [x] Bookings no longer require an email (P7). 2026-10-06, W1 (phone-first users)
  - [x] Caregiver actions check ownership (P8). 2026-10-05, WP-A
  - [ ] Critical security items closed (P9)
  - [x] Money stored in kobo (P10). 2026-10-05, E2
  - [ ] Meta business verified (P11)
- [ ] **Meta onboarding:** business portfolio, verification, dedicated number, display name, WhatsApp Business Account, payment method, templates (PRD §13–14)
- [ ] **Infrastructure:**
  - [x] Redis. 2026-10-05, E3
  - [x] Worker queue and scheduler. 2026-10-05, E3 (ARQ)
  - [ ] Non-sleeping hosting plan
- [ ] **Data model:** contacts, conversations, message metadata, care recipients and relationships, consents, quotes, dispatch waves, ratings, templates (PRD §10). Done: W1 tables, `quotes` and booking columns (W2, `0005_whatsapp_booking`), dispatch waves (earlier). Missing: ratings, a `message_templates` table (the registry is code for now)
- [x] **Webhook, Flow data endpoint and outbound sender** (WA-FR-1–4, §11). Webhook and sender 2026-10-06, W1 (own httpx client, not PyWa); Flow data endpoint (RSA-OAEP + AES-GCM, signature, ping/INIT/data_exchange/BACK) 2026-10-06, W2
- [x] **Consent and opt-out** (WA-FR-5–9). 2026-10-06, W1, #22 (button consent, STOP / STOP DATA / START; the consent Flow is deferred)
- [x] **Menu and navigation** (WA-FR-10–12). Menu and two-miss handover W1; 30-minute idle expiry that never loses a created booking W2
- [x] **Booking Flow and location request** (WA-FR-13–17). 2026-10-06, W2, #24 (verified locally against the fake Meta; not yet on Meta's Flow builder). No pin: the typed address is geocoded (Google, optional key) and a confident in-city result is used; otherwise the booking is held for an address check (2026-10-06, #28)
- [x] **Quote with 30-minute price lock** (WA-FR-18–21). 2026-10-06, W2. A service with no configured multiplier is priced at the default 1.0 in both channels, one shared lookup, logged for ops (2026-10-06, #28); admins make multipliers configurable in A4
- [x] **Paystack link, expiry and webhook confirmation** (WA-FR-22–27). 2026-10-06, W2 (WA-FR-26 callback page waits for the web companion, W4)
- [ ] **Matching and assignment notifications** (WA-FR-28–31). Caregiver-assigned message shipped in W3 (#27, 2026-10-06); `matching_delayed` and `no_caregiver_found` (WA-FR-29/31) are not built
- [ ] **Visit lifecycle notifications and visit code** (WA-FR-32–37). Visit code, on my way, started and completed messages shipped in W3 (#27, 2026-10-06); the 24 h / 1 h reminders are not built
- [x] **My bookings and cancellation policy** (WA-FR-38–39). 2026-10-06, W3, #27; two-tier fee and admin policy 2026-10-07, #30
- [x] **Emergency screen, human handover, no-medical-advice guard** (WA-FR-40–42). 2026-10-06, W1 (state emergency numbers still to be verified)
- [ ] **Web companion:** magic links, step-up authentication, tracking and booking pages (WA-FR-43–48). Planned as W4 (section 10)
- [ ] **Phase 2:**
  - [ ] FallGuard via a WhatsApp Flow, scored on the server (WA-FR-49–51)
  - [ ] Vitals reminders (WA-FR-52)
  - [ ] Recipient consent and care circle (WA-FR-53–55)
  - [ ] Pidgin
  - [ ] Abuja launch
- [ ] **Phase 3:** doctor teleconsultation (WA-FR-56–58)
- [ ] **Compliance:** impact assessment (DPIA), privacy notice, data protection officer, NDPC registration check, retention periods (WA-NFR-9–10)

## 7. Admin Console — 🟡 in progress (A0, A1 merged; A2 API merged; see section 9)
Spec: [PRD](../prd/PRD-admin-console.md). v1 must ship before the WhatsApp closed pilot.
- [x] **Admin auth:** separate admin identity, TOTP MFA, a single `require_permission` check, roles and permissions, invite flow, bootstrap CLI. (AD-FR-81–84) Done in A0 (#21, 2026-10-06), see section 9. `create_test_admin.py` and the default credentials were already removed in Phase 0.
- [x] **Append-only audit log and audit viewer** (AD-FR-85): the log was Phase 0; the viewer API `GET /admin/audit` is A0. The console screen is not built (moved to A5).
- [ ] **Overview:** KPIs, items needing attention, current supply (AD-FR-1–6)
- [x] **Caregiver verification:** queue, detail, document, identity and background decisions, approve/reject, structured skills, suspend (AD-FR-7–14). 2026-10-06, backend E5 (#12) and the A1 console (monorepo #6)
- [ ] **Private document storage:** signed URLs, migrate existing files, remove the public `/uploads` access (AD-FR-15–18)
- [ ] **Bookings:** list and detail, unmatched queue, assign/reassign, admin cancel, status correction (AD-FR-19–25). API merged 2026-10-06 (A2, #26); console screens not built
- [ ] **Payments:** payment list, webhook exceptions, daily reconciliation (AD-FR-28–31)
- [ ] **Refunds v2:** exactly one destination per refund, approval limits, refund webhooks, notifications (AD-FR-32–36)
- [ ] **Caregiver pay:** earnings credited on completion, ledger, Paystack Transfer payout batches, validated withdrawals (AD-FR-38–42)
- [ ] **People:** search, masked view, suspend, consent records (AD-FR-45–48)
- [ ] **Pricing:** (APIs `/booking-settings` and `/admin/booking-policy` exist; console screens are A4) versioned price books with preview and publish; service areas; Nigerian form options (AD-FR-51–56)
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
- [x] **Post-C4 screens and fixes** (caregiver monorepo)
  - [x] Account, wallet, notifications and directions screens. 2026-10-07, #7
  - [x] About and FAQ open the Oakly website. 2026-10-07, #8
  - [x] Fix: every API call failed in the real app (React Native `Response` instanceof in the api-client). 2026-10-07, #9
  - [x] Fixed, width-responsive type scale; no mid-word wraps; onboarding and nav fixes. 2026-10-07, #10
  - [x] On-my-way window; drop-out button only for WhatsApp bookings more than 2 h out. 2026-10-07, #11
  - [x] Expo SDK 57 patch bumps. 2026-10-06, #5
- [ ] **Remaining screens from the [gap analysis](../plans/2026-10-06-caregiver-design-gap-analysis.md)** (backend #25 is the analysis only; none built)
  - [ ] Client profile
  - [ ] Vitals tab: manual entry, charts, report
  - [ ] FallGuard incident log and questionnaire
  - [ ] Care plans: nurse-level create/edit, others tick tasks, templates in admin, family read-only via W4
  - [ ] Chat relay via WhatsApp
  - [ ] Masked calling (no recording)
  - [ ] Change password (needs a backend endpoint)
  - [ ] KYC: ID and face
  - [ ] Learn
  - [ ] Refer and earn
  - [ ] Complaints
  - [ ] Account and data export
- Launch prerequisite P3 (caregiver app reports location) is met in code by C4; it still needs device verification.

## 9. Phase A: Admin Console v1 — 🟡 in progress (A0, A1 merged; A2 backend merged)
Plan: [`docs/plans/2026-10-06-admin-console.md`](../plans/2026-10-06-admin-console.md). PRD: [`docs/prd/PRD-admin-console.md`](../prd/PRD-admin-console.md).
- [x] **A0** Admin identity: separate `admin_users`, TOTP MFA, roles and permissions, `require_permission` on every admin route, invites, bootstrap CLI, rate limits and lockout. merged 2026-10-06, #21, migration `0003_admin_identity`, [SOP](../sop/2026-10-06-admin-auth.md), [FE guide](../fe-integration-guide-admin.md). Opus review findings fixed 2026-10-06 (trusted proxies, password throttle vs MFA lock, single-use `mfa_token`, logout/refresh race); see the SOP addendum.
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
  - [x] 13. Final read of the review-fix diff before merge (merged as #21)
- [x] **A1** Console scaffold (`apps/admin`, Next.js 16): login and MFA, permission-gated shell, caregiver queue and detail with all decisions, admin users — merged 2026-10-06 (innocent98/oaklycare-caregiver-mobile#6, 8267a0b)
  - [x] BFF with httpOnly refresh cookie, single-flight refresh (browser + per-token in the BFF, 2 s late-caller grace)
  - [x] Security review fixes: proxy segment validation + allowlist (`admin/`, `caregiver/documents/`), 1 MB body cap, CSP with per-request nonce, HSTS in production, open-redirect guard; Opus re-review clean
  - [x] Verified: 121 admin tests, Playwright e2e 8/8 against the local docker stack, production build checked in Chromium (no CSP violations)
  - [ ] Audit log viewer page (API exists; moved to A5)
- [ ] **A2** Bookings ops: list and detail with timeline, unmatched queue, assign, reassign, cancel, status correction. **Backend merged 2026-10-06 (#26)**, migration `0006_admin_bookings_ops`, [SOP](../sop/2026-10-06-admin-bookings-ops.md), [FE guide](../fe-integration-guide-admin.md) section 12. Console screens not built (item 10).
  - [x] 1. `GET /admin/bookings`: filters (status, payment, dispatch state, channel, city, service, caregiver, date ranges, address check, no-show), search (code, names, phone), sort, pagination, masking by permission
  - [x] 2. `GET /admin/bookings/{id}`: price, payment, dispatch waves and offers, outbox events, timeline, `available_actions`; address behind `bookings.address`; condition decrypted only with `clinical.read` and every reveal audited
  - [x] 3. Unmatched queue: `address_check` (WhatsApp hold), `unmatched`, `sla_breached` (`UNMATCHED_SLA_MINUTES`)
  - [x] 4. Re-dispatch (wider radius, re-offer), candidates, assign (offer, or Super Admin force), reassign
  - [x] 5. Cancel with reason code and refund choice (flags `refund_required`, moves no money), confirm address (starts dispatch), no-show flag, Super Admin status correction
  - [x] 6. Every action: required reason, permission, booking row lock, audit row in the same transaction
  - [x] 7. Tests: route x role matrix, each action's effects and audit, clinical masking, two race tests (admin assign vs caregiver accept, two simultaneous cancels)
  - [x] 8. FE guide section 12 from live captures 079 to 118 (`capture_admin_flow.py --section bookings`), `--check` clean
  - [x] 9. Opus review of the lock, permission and clinical paths: clean, 2 findings fixed with tests
  - [ ] 10. **A2 console screens:** list, detail with timeline, unmatched queue, action dialogs (backend done)
- [ ] **A3** Money (none built)
  - [ ] Earnings credited on completion
  - [ ] Caregiver ledger
  - [ ] Scheduled Paystack Transfer payouts
  - [ ] 80% caregiver share of qualifying late-cancel fees (policy `caregiver_late_fee_share_percent`, booking flag `late_cancel_compensable`; both are recorded today, nothing is paid)
  - [ ] Refunds v2 queue (`refund_required`), payments and webhook exceptions
  - [ ] Gap: on the app channel a caregiver cancel sets the booking to cancelled with no refund flag, so the patient is not queued for a refund
  - [ ] Bank list and account-name resolve endpoints
- [ ] **A4** Pricing and policy console screens: versioned price books, service areas, form options, policies. The APIs exist (`/booking-settings` #31, `/admin/booking-policy` #30); no screens
- [ ] **A5** People (masked), break-glass clinical access, audit viewer page (API exists), CSV exports
- [ ] **FE integration guide for admin** (`docs/fe-integration-guide-admin.md`), generated from live captures. A0 part done (sign-in, admin management, audit, permissions); extend it with each later package

## 10. Phase W: WhatsApp patient channel (same backend) — 🟡 in progress (W1–W3b merged; W4, W5 planned)
Plan: [`docs/plans/2026-10-06-whatsapp-channel.md`](../plans/2026-10-06-whatsapp-channel.md). Built and tested locally against a fake Meta; real phone tests later need a Meta developer test number (not business verification).
- [x] **W1** Foundations: phone-first users, contacts, conversations, message metadata, consents, care recipients, support conversations; signed webhook and queue; Cloud API client; state machine with consent, STOP/START, menu, emergency screen, handover; fake Meta. 2026-10-06, #22, migration `0004_whatsapp_foundations`, [SOP](../sop/2026-10-06-whatsapp-foundations.md). Verified locally and by `scripts/whatsapp_local_e2e.py` on the Docker stack; not run against real Meta. Opus security review fixes applied 2026-10-06 (no message body in Redis, keyed contact refs, wider emergency lexicon and captions, 503 message-loss, chunked body cap, hostile payloads, blocked-contact emergencies, phone-only Paystack email): see the SOP addendum.
  - [x] Phone-first users (`users.email` nullable, E.164 helper); app registration still requires an email
  - [x] Seven tables; `consents` append-only (trigger + ORM guard); `whatsapp_messages` has no body column
  - [x] Webhook: handshake, HMAC over the raw body (empty secret refused), dedupe on Meta message id, ARQ job, status callbacks
  - [x] `MetaCloudClient`: timeout, retry on 429/5xx and connect errors, metadata recorded
  - [x] Pure conversation engine: first contact, button consent, STOP / STOP DATA / START, menu, two-miss handover, emergency screen (English + Pidgin), clinical-advice guard
  - [x] Fake Meta (`app/tests/fake_meta.py`) and the Docker end-to-end script
  - [ ] **Owner:** real-Meta check with a developer test number and a tunnel ([steps in the SOP](../sop/2026-10-06-whatsapp-foundations.md#local-testing-with-a-meta-developer-test-number--tunnel))
  - [ ] Deferred to W2: consent Flow (checkboxes) replaces the buttons; one-time-code linking of an existing app account; idle expiry (WA-FR-12)
- [x] **W2** Booking in chat: booking Flow and encrypted data endpoint, location, locked quote, Paystack link, templates. 2026-10-06, #24, migration `0005_whatsapp_booking`, [SOP](../sop/2026-10-06-whatsapp-booking.md). Verified locally and by `scripts/whatsapp_local_e2e.py` on the Docker stack against a fake Meta and a fake Paystack; not run against real Meta.
  - [x] Booking Flow JSON (`app/channels/whatsapp/flows/booking.json`, version 6.3) with a structure test; six screens (who, new recipient, service with per-hour price, date and slots, duration/condition/instructions, address and city)
  - [x] Flow data endpoint `POST /api/v1/whatsapp/flows/data`: signature (432), RSA-OAEP + AES-128-GCM with flipped-IV reply (421 on tamper), ping, INIT, data_exchange, BACK; the condition is encrypted straight into the draft and never returned
  - [x] Location request after the Flow; no pin = placeholder coordinates, `needs_address_check`, paid but not dispatched
  - [x] No pin: geocode the typed address (`app/services/geocoding.py`, optional `GOOGLE_MAPS_API_KEY`), accept street-level results inside the city box, run off every lock after the commit; `location_source` pin / geocoded / placeholder on the quote payload. [SOP](../sop/2026-10-06-default-multipliers-and-geocoding.md)
  - [x] Default multiplier 1.0 for every service in both channels (`pricing.service_multiplier`)
  - [x] Service-area check (Lagos, Abuja, from a setting) with a waitlist offer for anywhere else
  - [x] Quote: `price_service` (same `compute_price`, strict about the multiplier), `quotes` row locked 30 minutes, itemised in naira with the cancellation line; price change after the lock does not change the booking
  - [x] Confirm creates one booking (conversation, quote and unique `quotes.booking_id` locks; two-worker test), Paystack link with `paystack_email_for`, "Pay now" CTA
  - [x] Payment journey: reminder at 20 minutes, cancellation at 30 (existing expiry job), `payment_received`; Paystack verify when the person returns first; `booking_events` outbox
  - [x] Template registry in code (5 W2 templates, utility; `payment_refund_pending` added by the security review) and the 24-hour window rule enforced in `send_and_record`
  - [x] Conversation states `booking_flow -> awaiting_location -> quoted -> awaiting_payment -> idle`; 30-minute idle expiry
  - [x] Condition encrypted on the booking (`condition_encrypted`), never in a message, table, log or Redis (asserted)
  - [ ] **Owner:** upload the Flow to Meta's builder, set the endpoint, upload the public key, submit the 4 templates, set `WHATSAPP_BOOKING_FLOW_ID` (steps in the SOP)
  - [ ] Deferred: operator action to clear `needs_address_check` and start dispatch (API in A2, #26; console screen not built); supply-aware time slots; web-companion payment return page (W4); `message_templates` table with Meta approval status
- [x] **W3** Notifications (channel-agnostic) and my bookings/cancel. 2026-10-06, #27, migration `0007_whatsapp_notifications` (on top of A2's `0006_admin_bookings_ops`), [SOP](../sop/2026-10-06-whatsapp-notifications.md). Verified locally and by `scripts/whatsapp_local_e2e.py`; not run against real Meta.
  - [x] `NotificationService` (`app/services/notification_service.py`): domain change -> outbox rows in the same transaction; WhatsApp for the booker, Expo push for the caregiver; nothing sent inline
  - [x] Events from the existing transitions: caregiver assigned, visit code issued, on my way, started, completed, cancelled by the caregiver (refund flagged, booker told), admin cancel and refund started/done (hooks for A2)
  - [x] Visit code to the booker (Fernet field, never logged or stored in the outbox; shown again in My bookings to the booker only); no health data in any message (asserted)
  - [x] My bookings: 5 most recent for the contact, their recipients and their app bookings; detail with status-driven actions; stateless buttons, ownership re-checked on every tap
  - [x] Cancel in chat: policy shown before confirming; full refund before assignment or >12 h out; a fee of the service fee inside 12 h (now two tiers from the admin policy, see the drop-out item below); in progress hands to a human; refund queued with `refund_amount_kobo`; unpaid reuses W2's verify-before-cancel; race with the caregiver tested (mutation-checked)
  - [x] Seven templates added to the registry (essential flags decide who still hears after STOP); migration `0007`
  - [ ] **Owner:** submit the 12 templates (list in the [W3 SOP](../sop/2026-10-06-whatsapp-notifications.md#owner-steps-nothing-here-has-been-done)); decide the cancellation fee; check `visit_code` category with Meta
  - [ ] Deferred: visit reminders 24 h/1 h, `matching_delayed`, `no_caregiver_found` (WA-FR-29/31/32/33); ~~re-dispatch after a caregiver drops out~~ (done, below); rating buttons, photo header, tracking and View-booking links (W4); the A2 admin cancel/refund hooks are wired (#26)
- [x] **W3b** Caregiver drop-out re-dispatch, two-tier cancellation fee, admin-editable booking policy. 2026-10-07, #30, migration `0010_dropout_fee_tiers`, [SOP](../sop/2026-10-07-dropout-redispatch-and-fee-tiers.md). Verified locally and by `scripts/whatsapp_local_e2e.py`; not run against real Meta.
  - [x] Drop-out of a paid WhatsApp booking: reopened and re-dispatched (never back to the caregiver who dropped), booker told with no action needed; app bookings unchanged
  - [x] No replacement by the decision lead, a drop-out inside the re-dispatch lead, or after "on my way": the booker gets Keep waiting / Pick another time / Full refund (stateless `bk:*` buttons, ownership and state re-checked under the lock); Keep waiting hands over to a person at the start time; Pick another time opens a ticket; Full refund is the whole amount, no fee
  - [x] Worker `dropout_tick_job` for the time-driven steps; drop-outs recorded per caregiver (`caregiver_dropouts`)
  - [x] Two-tier family fee (free, 20%, 50% of the service fee), tier stored on the booking, `late_cancel_compensable` decided under the lock from a qualifying on-my-way (movement check unit-tested)
  - [x] `booking_policies` row + `GET`/`PATCH /admin/booking-policy` (reason, audit old to new, `settings.write`); cancellation tiers snapshotted on quote and booking; `WHATSAPP_CANCEL_*` env settings removed
  - [x] Two templates (`dropout_replacing`, `dropout_choice`) added to the registry and the SOP owner list
  - [ ] **Owner:** submit templates 13 and 14; tell the caregiver app team a dropped WhatsApp booking now comes back as `pending` from `cancel`
  - [x] Caregiver cancel window (owner decision; merged 2026-10-07, #32), [SOP](../sop/2026-10-07-caregiver-cancel-window.md): `POST /booking/caregiver/action/{id}` `cancel` on a WhatsApp booking is refused with `409` unless it is `accepted` and more than `dropout_redispatch_min_lead_minutes` before the start; inside that, en route or in progress the caregiver contacts support (admins reassign or cancel through the A2 routes); app bookings unchanged; `channel` now returned on the caregiver booking responses
  - [ ] Deferred (A3): pay the caregiver from `late_cancel_compensable` and `caregiver_late_fee_share_percent`; admin console views of drop-outs and a policy page; rescheduling ("Pick another time" is a person for now)
- [ ] **W4** Web companion: API and `apps/companion` (magic links, step-up auth, tracking and booking pages, Paystack return page; not started)
- [ ] **W5** FallGuard Flow, vitals reminders, care circle, Pidgin (not started)

## 11. Tech debt — 🟢 done, merged 2026-10-06 (#23, [SOP](../sop/2026-10-06-tech-debt.md))
- [x] passlib → pwdlib, python-jose → PyJWT (app tokens), google-generativeai → google-genai
- [x] Python 3.11 → 3.13 (Docker, CI via `.python-version`, ruff target)
- [x] Typed response models for verification-status, form-options, the booking action response and the admin caregiver list/detail (no shape change; golden shape test)
- [x] `TRUSTED_PROXIES=` (as in `.env.example`) crashed Settings; list settings now accept empty, comma-separated or JSON

## 12. Account deletion (erasure as anonymisation) — 🟢 done, merged 2026-10-07 (#29, [SOP](../sop/2026-10-07-account-deletion.md))
- [x] ⚠️ `DELETE /auth/profile` returned 500 for any user with history (bookings, wallet, consents, tickets, WhatsApp contact, vitals). Reproduced with 16 failing tests first
- [x] Erasure request handled as anonymisation in one transaction: PII wiped on User and Caregiver/Patient, account closed, push tokens deleted, payout details removed or masked
- [x] Bookings, wallet and transactions, consents, support tickets, vitals and prescriptions kept; erasure recorded as `account_erasure` consent withdrawals
- [x] 409 with an actionable `detail` for active bookings, a positive wallet balance or a pending withdrawal (caregiver), and paid active bookings or a wallet balance (patient)
- [x] WhatsApp contact unlinked and anonymised through the STOP DATA code (`app/channels/whatsapp/withdrawal.py`, now shared with the processor)
- [x] Profile image deleted from Cloudinary after commit, never failing the request
- [x] Response model `DetailResponse`; caregiver FE guide section 3 re-captured live
- [x] Privacy review fixes (8): unpaid bookings cancelled, erased caregivers never matchable, vitals access revoked, pending money blocks, purge queue, retained-data wipes, locked caregiver check, unlinked WhatsApp contacts. Migration `0009_account_erasure`, on `main`

## 13. Pricing settings hardening — 🟢 done, merged 2026-10-07 (#31, [SOP](../sop/2026-10-07-pricing-settings-audit.md))
- [x] ⚠️ `PUT /booking-settings/{id}` edited the row in place, silently re-pricing quotes that point at it. Every write now inserts a new version; old rows are never changed
- [x] ⚠️ `POST`/`PUT`/`DELETE` wrote no audit row and took no reason. `reason` required; `booking_settings.create` / `booking_settings.update` audit rows with before and after
- [x] ⚠️ Unchecked values (negative fee, percent over 100). Strict typed model with bounds, known service keys, unknown fields refused
- [x] `DELETE` always refused (`409`); stale `PUT` refused (`409`); concurrent writers serialised
- [x] Both `GET`s need an admin with `bookings.read` (no client calls them)
- [x] Admin FE guide section 13 captured live (132 to 149)

## Backlog / upcoming
- [ ] **Founder decisions** for WhatsApp (PRD §16, D1–D11) and the Admin Console (PRD §14, A1–A10), including caregiver payout share, city pricing and the refund approval limit
- [x] Push notifications for caregiver job offers (Expo). 2026-10-05, C3, [SOP](../sop/2026-10-05-caregiver-mobile-support.md). Patient-side pushes and WhatsApp are not built.
- [ ] Medical records and family members endpoints (tables exist, no API)

## Deferred follow-ups
- [ ] (Pricing) `/prescription/settings` routes still edit in place with no audit row; give them the same versioning and audit
- [ ] (Pricing) A public price-preview route if a client ever needs one (none calls `/booking-settings` today)
- [ ] ⚠️ (Erasure review) `Booking.condition` is plaintext for app bookings (WhatsApp bookings encrypt it as `condition_encrypted`); encrypt it and migrate existing rows
- [ ] (Erasure review) A media purge job for `pending_media_purges`, and for caregiver document files (not purged on erasure)
- [ ] (Erasure) Caregiver document files (`file_path` targets) and the ID-verification files at the providers are not deleted from storage, only the database references are blanked; add a purge job. Medical records and care recipients created for other people are kept (clinical and booking retention); decide the retention period with counsel
- [ ] (Erasure) The admin console has no view of erased accounts; they are recognisable by `email LIKE 'deleted+%@example.com'`
- [x] `patient.py` admin refund routes checked `user_type != "admin"` (never true). Fixed: they use `require_permission("refunds.approve")` (A0)
- [ ] Chat and notes routes in `endpoints/booking.py` don't let admins in; decide whether support staff should read them (an admin token is rejected there since A0, as an app token was before)
- [ ] (A0) Admin invites are not emailed: the API returns the token and the console shows the link. Add an email, and an endpoint to re-issue an expired invite (today an expired invite blocks that email)
- [ ] (A0) MFA recovery codes, and an optional IP allowlist for the console (AD-FR-87)
- [ ] (A0) Admin access to the live-tracking WebSocket (admins used to reach it with an app token; the A2 booking detail does not show live tracking yet)
- [x] (A0) Per-IP limits and audit `ip` honour `TRUSTED_PROXIES`; set it to the reverse proxy's address when one is deployed
- [ ] (A0) Admin `POST /prescription/create` is user-only now (an admin used to pass the patient-ownership check); prescription operations move to the console in v1.1
- [ ] (Verification) Real Meta (WhatsApp) and real Paystack have not been exercised; everything runs against fakes and local e2e
- [ ] (Verification) Xcode upgrade so a dev build of the caregiver app works; push and background location are untested on a device
- [ ] (Owner) Content: the WhatsApp support number
- [ ] (Owner) EAS project, plus APNs/FCM credentials, for real push
- [ ] Upgrade Poetry to 2.5.x everywhere (local, Docker, CI)
- [x] Docker image size was 828 MB; now 520 MB after the `google-genai` swap and Python 3.13 (2026-10-06)
- [ ] (C3) `GET /booking/caregiver/bookings?status=new_requests` and `/booking/caregiver/notifications` expose condition, instructions, address and patient contact to caregivers who have not accepted; trim them to the offer fields
- [x] (C3) Typed the caregiver action response (2026-10-06, `CaregiverActionResponse`)
- [ ] (C3) Trim the internal fields from the caregiver action response (`paystack_reference`, `*_kobo`, refund fields) and store `reason` on decline/cancel; the typed model now documents them as served today
- [ ] (C3) Move the Expo push off the settlement request onto the ARQ worker if it ever slows the Paystack webhook; poll Expo receipts to prune late-failing tokens
- [x] (A2) Tell the patient about an admin cancel and a reassignment; a partial refund's amount now has a real column (`refund_amount_kobo`). Closed by W3 on `feat/whatsapp-notifications`. See the [A2 SOP](../sop/2026-10-06-admin-bookings-ops.md)
