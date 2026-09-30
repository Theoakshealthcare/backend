# Oakly — Master Build Checklist

This covers all three repos: `backend` (FastAPI), `patient` (Expo), and `caregiver` (Expo). The backend is the hub, so the master checklist lives here.

**Snapshot (2026-09-29):**
- **Done:** 1 foundation item (Poetry migration).
- **In progress:** the critical fixes track.
- **Planned:** SDK upgrades, a shared package, and a decision on a WhatsApp channel.

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
- [ ] Merge `chore/poetry-migration` and switch the Render build and start commands
- [ ] Real pytest suite with a throwaway Postgres, run in CI
- [ ] Clean the ruff backlog, then enforce the full ruleset and `ruff format`
- [ ] Python 3.11 → 3.12 or 3.13
- [ ] Library swaps: passlib → pwdlib · python-jose → PyJWT · google-generativeai → google-genai

## 1. Secrets & repo hygiene — 🔴 critical, not started
- [ ] ⚠️ Rotate every secret in the committed backend `.env`: JWT secret, SMTP, Gemini, Paystack keys and webhook secret, Cloudinary, Supabase DB password
- [ ] ⚠️ Plan re-encryption of vitals and FallGuard data before rotating `VITALS_ENCRYPTION_KEY`
- [ ] ⚠️ Move secrets into Render env vars, then `git rm --cached .env`
- [ ] ⚠️ Remove the hardcoded DB password from `config.py`, `alembic.ini`, `docker-compose.yml` and `scripts/check_booking_patient.py`
- [ ] ⚠️ Patient app: remove the committed release keystore and its passwords (`android/gradle.properties`), and decide whether to reset the Play upload key
- [ ] Decide on a history rewrite (`git filter-repo`) for each repo
- [ ] Remove the committed `__pycache__` files; untrack the caregiver app's `.env`

## 2. Backend security & money fixes — 🔴 critical, not started
- [ ] ⚠️ Authenticate the booking WebSockets (they currently use hardcoded demo users)
- [ ] ⚠️ Require auth and ownership checks on the prescription endpoints
- [ ] ⚠️ Admin approval for caregiver identity; stop accepting the verification result from the client
- [ ] ⚠️ Make the caregiver document uploads private (they're under the public `/uploads` mount)
- [ ] ⚠️ Reject refresh tokens used as access tokens (check the `type` claim)
- [ ] ⚠️ Validate that withdrawal amounts are positive, and lock the wallet row
- [ ] ⚠️ Fix refund approval paying twice (wallet credit plus a Paystack refund)
- [ ] ⚠️ Make the Paystack webhook safe to replay, and check the paid amount against the booking
- [ ] Wallet transactions: remove the nested `db.begin()`
- [ ] Password reset: fix the email arguments that cause a 500, and build a real reset link
- [ ] Vitals access grant: stop comparing a user ID with a patient ID; use the `accepted` status
- [ ] Only the booking's own caregiver can cancel it or change its state; only paid bookings can be accepted
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

## Backlog / upcoming
- [ ] **Decision: WhatsApp-first patient channel (hybrid)**, proposal drafted 2026-09-29: [proposal](../proposals/2026-09-29-whatsapp-patient-channel.md). Blocked on founder decisions (§14) and Meta business verification.
- [ ] Push notifications (configured but never implemented)
- [ ] Medical records and family members endpoints (tables exist, no API)
- [ ] Admin console (caregiver approval, refunds, pricing settings)

## Deferred follow-ups
- [ ] Upgrade Poetry to 2.5.x everywhere (local, Render `POETRY_VERSION`, CI)
- [ ] Docker image size is 828 MB, mostly gRPC and Google libraries; revisit after the `google-genai` swap
