# Oakly — Master Build Checklist

This covers all three repos: `backend` (FastAPI), `patient` (Expo), and `caregiver` (Expo). The backend is the hub, so the master checklist lives here.

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
- [x] Backend on Poetry (`pyproject.toml` + `poetry.lock`), with Docker and Render updated — 2026-09-29, branch `chore/poetry-migration`, see [SOP](../sop/2026-09-29-poetry-migration.md)
- [x] Backend CI: lock check, bug-class lint, app smoke test — 2026-09-29
- [x] Backend `.gitignore` — 2026-09-29
- [ ] Merge `chore/poetry-migration` (Render is being retired; the deploy target is Docker)
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
- [ ] **Admin auth:** separate admin identity, TOTP MFA, a single `require_permission` check, roles and permissions, invite flow. Remove `create_test_admin.py` and the default credentials. (AD-FR-81–84)
- [ ] **Append-only audit log and audit viewer** (AD-FR-85)
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

## Backlog / upcoming
- [ ] **Founder decisions** for WhatsApp (PRD §16, D1–D11) and the Admin Console (PRD §14, A1–A10), including caregiver payout share, city pricing and the refund approval limit
- [ ] Push notifications (configured but never implemented)
- [ ] Medical records and family members endpoints (tables exist, no API)

## Deferred follow-ups
- [ ] `patient.py:320,362` admin routes check `user_type != "admin"` (never true); use `get_current_admin` (WP-D covers the refund route only)
- [ ] Chat and notes routes in `endpoints/booking.py` don't let admins in; decide whether support staff should read them
- [ ] Upgrade Poetry to 2.5.x everywhere (local, Render `POETRY_VERSION`, CI)
- [ ] Docker image size is 828 MB, mostly gRPC and Google libraries; revisit after the `google-genai` swap
