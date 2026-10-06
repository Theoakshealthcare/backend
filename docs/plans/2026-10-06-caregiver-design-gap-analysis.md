# Caregiver app: design vs build gap analysis

| | |
|---|---|
| **Date** | 2026-10-06 |
| **Design source** | 33 screenshots in `oakly/ss/caregiver/` (Figma exports, about 110 phone frames) |
| **Compared against** | backend `main` aade294 (after W2) and caregiver monorepo `main` 0f9a96b |
| **Key constraint** | Patients and families are on **WhatsApp**, not a patient app. Anything in the design that assumes the patient has the Oakly app (in-app chat partner, in-app calls, live device vitals, fall detection) needs a WhatsApp-shaped answer or can't ship as drawn. |

**Legend:**
- ✅ **Built:** the backend and app are both wired.
- 🟡 **Partial:** what's missing is said.
- ⬜ **Not built:** the work is possible; it hasn't started.
- 🚫 **Blocked as designed:** the WhatsApp channel or another hard constraint stops it shipping as drawn, so an alternative is proposed.
- ❓ **Needs a decision.**

## Snapshot

| Area | ✅ | 🟡 | ⬜ | 🚫 |
|---|---|---|---|---|
| Sign-up, login, onboarding | 7 | 2 | 1 | 0 |
| Home | 2 | 3 | 1 | 0 |
| Bookings and visits | 4 | 0 | 1 | 0 |
| Communication (chat, call, notes, notifications) | 1 | 3 | 1 | 2 |
| Client care (profile, vitals, FallGuard, care plans) | 1 | 2 | 2 | 2 |
| Wallet and payouts | 0 | 2 | 3 | 0 |
| Profile, settings, support | 0 | 2 | 7 | 0 |
| Learn (courses, certificates) | 0 | 0 | 1 | 0 |

## 1. Sign-up, login and onboarding

| Screen | Backend | App | Status | Notes |
|---|---|---|---|---|
| Splash carousel (4 slides, Skip, Get Started, Login) | n/a | ✅ | ✅ | |
| Create account (name, email/phone, password, terms) | ✅ `POST /auth/register` | ✅ | ✅ | |
| Verify email (OTP) + "account setup complete" | ✅ `/auth/verify-email`, `/auth/resend-verification` | ✅ | ✅ | The design shows 5 empty boxes but 4 digits typed. `OTP_LENGTH` decides; keep 5. |
| Login (email or phone + password) | ✅ `/auth/login/mobile` | ✅ | ✅ | |
| **OTP after login** ("Login Successful" after a code) | ⬜ | ⬜ | ❓ | Not built. Recommendation: ask for a code only on a **new device**, not every login. |
| Recover password: email → OTP → new password | 🟡 the backend emails a **reset link token**, not a code | 🟡 built to the token flow | 🟡 | To match the design: a 5-digit reset code (small backend change), then the app reuses the OTP screen. |
| Setup step 1: DOB, gender, state, address, emergency contact | ✅ `PUT /caregiver/profile/step1` | ✅ | ✅ | |
| Setup step 2: specialisation, years, licence upload, hours, service areas | ✅ step2 + `form-options` + `documents/upload` | 🟡 | 🟡 | Licence upload from step 2 is not confirmed in the app. Check it, then wire it. |
| Setup step 3: **ID document scan + face scan** | 🟡 a `identity-verification/result` endpoint for Prembly; an Onfido service exists but nothing uses it | ⬜ | ❓ | Pick a KYC provider (Prembly / Smile ID / Onfido). The background check is required (founder decision). Today it is decided by staff in the admin console. |
| Background check and data consent | ✅ `PUT /caregiver/consent` | ✅ | ✅ | |
| "Profile setup completed, under review" | ✅ `verification-status` | ✅ (gate) | ✅ | |

## 2. Home

| Element | Status | Notes |
|---|---|---|
| Availability toggle | ✅ | Wired to `POST /caregiver/availability` and location tracking. |
| **Week calendar strip** ("Weekly Availability, March 2025") | ⬜ | The backend only has preferred hours from setup. A per-day schedule needs a small model and endpoint. ❓ Is it "days I work" or just a date picker for appointments? |
| Profile completion card | 🟡 | The status exists (`verification-status`); the card isn't built. |
| Upcoming appointments with Clock in / Clock out (timer modal, "clocked out") | ✅ | Built as on-my-way → start (with the **visit code**) → complete. The design has no visit code; keep ours (it proves the caregiver is at the door). W3 sends the code to the family on WhatsApp. |
| New job request with Accept / Decline + "are you sure" modal | ✅ | |
| Earnings summary (weekly, pending) | 🟡 | Only the wallet balance shows. **Caregivers are not credited for completed visits until A3 (money) ships**, so any earnings number is 0 today. |
| "CarePlan — Let's help you get health care for your loved one" / "Start Booking Journey" | n/a | **This is patient copy left in the caregiver design.** Ask the designer. |
| "Client care access: client profiles, vitals & FallGuard" | see §5 | |

## 3. Bookings and visits

| Screen | Status | Notes |
|---|---|---|
| Bookings tabs: New request / Upcoming / Completed | ✅ | |
| Job details (service, client, time, duration, payment, location, special instructions, Accept / Decline) | ✅ | The condition stays hidden until the caregiver is assigned (privacy rule). ❓ The design shows the **patient's price** (₦250,000) to the caregiver. Should it show the caregiver's earning instead? |
| Start service / clock in / clock out | ✅ | Visit-code flow with lockout. |
| **Get directions / map** | ⬜ | Cheapest path: open Google Maps / Apple Maps with the booking's coordinates (deep link). An in-app map isn't needed. Live tracking for the family belongs on the W4 web page, not in this app. |
| Decline confirmation modal | ✅ | |

## 4. Communication: chat, calls, notes, notifications

| Screen | Backend | App | Status | Notes |
|---|---|---|---|---|
| **Chat list + chat with the patient** (booking card, quick replies "I'm on my way", "I've arrived", voice note) | ✅ REST per booking | ✅ | 🚫 as drawn | The app chat works, but **nobody is on the other side**: the family is on WhatsApp. Proposal below. |
| "Oakly Admin" thread / live support chat | 🟡 support tickets exist (WhatsApp handover) | ⬜ | ⬜ | Reuse `support_conversations` for caregivers; needs the admin support inbox (later Phase A). |
| **Voice / video call** ("Audio Recording is Active", timer) | ⬜ | 🟡 UI only, no calling | 🚫 as drawn | See *Calling* below. **Video to a WhatsApp user isn't possible today.** |
| AI note-taker (voice to text) | 🟡 notes + Gemini analysis of text; no audio upload / transcription | 🟡 records audio, says transcription unavailable | 🟡 | Add an audio upload and transcription endpoint (Gemini accepts audio). Keep audio short-lived and encrypted; it is health data. |
| Notifications list | ✅ `GET /booking/caregiver/notifications` | ⬜ | 🟡 | A frontend screen only. |
| Push on the lock screen | ✅ | ✅ | ✅ | |

### Chat with a WhatsApp family: proposal
A **relay through Oakly's WhatsApp number**:
1. The caregiver types in the app.
2. The family receives it on WhatsApp from *Oaklycare*, prefixed with the caregiver's first name and the booking.
3. Their replies come back into the app thread, routed by the active booking.

Nobody's personal number is shared.
- **The 24-hour rule:** Meta allows free-form messages only within 24 h of the family's last message. Outside that, only approved templates can be sent. The design's quick replies ("I'm on my way", "I've arrived", "I'll be there in 10 mins") fit templates exactly. Free text outside the window waits, or the app shows "they need to reply first".
- **Safety:** messages pass the existing emergency screen. Bodies follow the W1 no-storage rule, or a short retention for dispute handling. ❓ Retention is a decision.
- **Scope:** only for an active booking (assigned → completed + a grace period).

### Calling: what's possible
Facts from Meta's WhatsApp Business Calling API docs, checked 2026-10-06:
- **The family calling Oakly** (user-initiated) works everywhere the Cloud API works, Nigeria included.
- **Oakly calling the family** (business-initiated) is **not available for Nigerian business numbers**. Meta's docs: *"The business phone number's country code must be in this supported list"*, and Nigeria is excluded. It also needs the family's permission first.
- **Voice only.** Video is listed as planned.

| Option | How it works | Pros | Cons |
|---|---|---|---|
| **A. Masked phone call (recommended for v1)** | The caregiver taps Call → a telephony provider (e.g. Africa's Talking, Twilio) rings the caregiver and the family's phone and bridges them. Both see a proxy number. | Works on any phone, for elderly users, with no data. No numbers are shared. Simple to build (an outbound API + a webhook). | Per-minute cost. A normal phone call, not WhatsApp. |
| B. Family calls Oakly on WhatsApp → routed to the caregiver's app | WhatsApp user-initiated call → Oakly's WebRTC bridge → the caregiver app (react-native-webrtc). | Feels native to the family. | Hard: a media server / SIP bridge, VoIP push on iOS, and only *inbound*. The caregiver still can't call the family. |
| C. Caregiver → family on WhatsApp from a non-Nigerian Oakly number | Business-initiated call from, say, a UK number. | WhatsApp-native. | A foreign number on a Nigerian health service, a permission request first, and the same WebRTC bridge as B. Not recommended. |
| D. Plain `tel:` link | The app dials the family's number directly. | Free, trivial. | Exposes personal numbers both ways. Not recommended. |

**Recommendation:** ship **A** (masked voice, caregiver ↔ family, only during an active booking). Drop **video** from the caregiver design for now. Revisit **B** once the business is verified and volume justifies it.

**Recording ("Audio Recording is Active"):** recording health calls needs a clear spoken notice and a lawful basis under the NDPA, plus encrypted storage and a retention limit. ❓ Recommendation: no recording in v1.

## 5. Client care: profile, vitals, FallGuard, care plans

| Screen | Backend | App | Status | Notes |
|---|---|---|---|---|
| Client profile (age, sex, tag, care plan summary, latest vitals, medical history, emergency contact, Add notes) | 🟡 the data is spread over patient models; no caregiver-facing endpoint | 🟡 booking details only | 🟡 | Needs a caregiver-scoped `GET /caregiver/clients/{id}`, allowed only while assigned. With WhatsApp booking, **medical history isn't collected**; the caregiver or a nurse adds it. |
| Vitals: client vitals grid, per-vital charts (day/week/month), insights | ✅ `/vitals/*` incl. insights and download | ⬜ (tab commented out) | ⬜ | Buildable now. ❓ "Critical/Normal" thresholds need clinical sign-off. |
| **Manual vital entry** (HR, BP, temp, RR, glucose with mg/dL/mmol/L + RBS/FBS/PP, sleep) | ✅ `POST /vitals` | ⬜ | ⬜ | **With patients on WhatsApp, this is the main source of readings.** It is the most valuable screen in this area. |
| "Live Performance Active" (continuous readings) | — | — | 🚫 | Needs a wearable or device integration on the patient. There is no patient app. Show "last recorded by …" instead. |
| Download / share vital report | ✅ download endpoint | ⬜ | ⬜ | The share sheet is native. |
| **FallGuard: "Fall detected / near fall", risk factors** | 🟡 a risk **questionnaire** only | ⬜ | 🚫 detection / ⬜ the rest | Automatic fall detection needs a sensor on the patient: not possible via WhatsApp. Feasible: the caregiver **logs an incident** and runs the risk questionnaire. The family can also do the questionnaire in a WhatsApp Flow (W5). |
| **Care plans** (templates, create plan, goals, medication schedules, daily tasks with check-offs per day, notes and observations, allergies and special instructions) | ⬜ no models | ⬜ | ⬜ | The largest missing module (new tables, about 15 endpoints, about 10 screens). ❓ Who can create or edit (any caregiver, a nurse only, or a clinician review)? Should the family see it, via the W4 web companion? |
| Care notes | ✅ | ✅ (typed) | ✅ | |

## 6. Wallet and payouts

| Screen | Backend | App | Status | Notes |
|---|---|---|---|---|
| Wallet balance, total paid in / withdrawn | ✅ `GET /caregiver/wallet` | 🟡 balance on Home | 🟡 | **The numbers stay 0 until A3** credits earnings on completion. |
| Earnings history by month + download payslip (PDF in the design) | ✅ transactions + payslip **CSV** | ⬜ | 🟡 | A PDF payslip needs a renderer. |
| Bank details: bank list, account number, account name | 🟡 get/put bank account; no bank-list or account-name-resolve endpoint | ⬜ | ⬜ | Add endpoints over Paystack `bank` and `bank/resolve`; the account name comes from the bank, not typed. |
| Withdraw | ✅ `POST /wallet/withdraw` | ⬜ | ❓ | **A3 plans admin-run payout batches**, and the design has no withdraw button. Decide: scheduled payouts only (recommended), or self-withdraw. |
| "Refund request sent successfully" | n/a | n/a | n/a | **A patient screen**, not caregiver. Drop it. |

## 7. Profile, settings and support

| Screen | Backend | App | Status |
|---|---|---|---|
| Profile settings menu | n/a | ⬜ | ⬜ |
| Edit profile (photo, name, email, phone, address, gender, age, emergency contact) | ✅ `PUT /auth/profile`, image upload, step1 | ⬜ | ⬜ |
| Change password (in Edit profile / Security) | ⬜ no endpoint | ⬜ | ⬜ |
| Logout / Delete account | ✅ logout logic, `DELETE /auth/profile` | ⬜ no entry point | 🟡 |
| About us / FAQs / Contact us (chat, email, WhatsApp DM) | static | ⬜ | ⬜ (content needed) |
| Refer and earn | ⬜ | ⬜ | ⬜ ❓ |
| Complaints | ⬜ (could reuse support tickets) | ⬜ | ⬜ |
| Account and data (download my data, NDPA) | ⬜ | ⬜ | ⬜ |

Refer and earn needs a policy: the reward, when it pays ("first transaction of a referred friend"), and fraud rules.

## 8. Learn

| Screen | Status | Notes |
|---|---|---|
| Learn home, categories, your learning, course details, modules, resources, certificate (ID + QR), download / share | ⬜ backend and app | A self-contained LMS: courses, modules, progress, resources, certificate PDF with verification. ❓ Who writes the content? Is completion required before approval or for some specialisations? Later phase. |

## Suggested order

1. **Quick wins** (the backend mostly exists, so this is app work):
   - the notifications list;
   - the settings menu with logout and delete account;
   - edit profile and change password (plus a small backend endpoint);
   - directions deep link;
   - wallet, history and payslip screens;
   - bank details (plus backend bank-list and resolve endpoints);
   - static About / FAQ / Contact.
2. **A3 money**, so earnings are real.
3. **Client care v1**: the client-profile endpoint, the Vitals tab with manual entry, charts and report, and the FallGuard incident log and questionnaire.
4. **Family communication**: the WhatsApp chat relay, then masked calling (option A).
5. **Care plans** module, after the "who can edit" decision.
6. **KYC** ID + face scan, after the provider decision.
7. **Later:** Learn, Refer and earn, Complaints, Account and data.

## Decisions needed

1. Login code: on a new device only (recommended), or every login?
2. KYC provider for ID + face match.
3. The caregiver sees the patient price, or their own earning, on job cards.
4. Payouts: scheduled batches only (recommended), or self-withdraw.
5. Calling: masked phone calls (recommended). Recording: none in v1 (recommended).
6. Chat relay message retention.
7. Care plans: who creates and edits them, and whether the family sees them.
8. Clinical thresholds for "Critical" vitals.
9. Learn: content owner, and whether it is mandatory.
10. Refer and earn policy.

Design clean-ups to send back to the designer:
- patient copy left on the caregiver Home and the notifications empty state;
- the refund-request screen;
- the OTP box count;
- the week-strip meaning.
