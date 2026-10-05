# Proposal: WhatsApp as Oakly's Patient Channel

| | |
|---|---|
| **Status** | Draft for decision |
| **Date** | 2026-09-29 |
| **Scope** | Patients, their family members, nurses and caregivers, and doctors, across the backend, patient and caregiver repos |
| **Launch markets** | Lagos, then Abuja |
| **Decision needed** | Approve the hybrid model (§4) and the phased plan (§11), and answer the open questions (§14) |

> **How to read this document.** Facts from outside sources are dated and linked in §15. Anything I could not confirm against a primary source is marked **⚠️ Unverified**. Every cost figure is an estimate built from the stated assumptions. Statements about Oakly's own code and pricing come from reading the repos and calling the deployed API on 2026-09-29.

---

## 1. Executive summary

**Recommendation: go hybrid, and put WhatsApp first for patients.**

- **Patients and family members** use WhatsApp to sign up, give consent, book, receive a Paystack payment link, get updates and reminders, and fill in the FallGuard check.
- A **small, secure web companion**, opened from links in the chat, handles what WhatsApp can't do or shouldn't carry:
  - live caregiver tracking
  - vitals and charts, and AI insights
  - history and prescriptions
  - video consultations with doctors
- **Nurses and caregivers keep a native app.** They need background GPS, document and ID capture, fast job acceptance and an earnings view.
- **Doctors** get a **web clinician console** for consultations, notes and e-prescriptions.
- **The backend stays the single system of record.** WhatsApp becomes one more channel calling the same booking, payment and vitals services. About 80% of the backend is reused.

**Why not WhatsApp only.** Four hard limits make it impossible (§3):
1. There are no in-chat payments in Nigeria.
2. WhatsApp can't stream live location to a bot.
3. Meta's policy restricts health data, and it prohibits selling drugs.
4. Nigerian data-protection law adds duties for sensitive health data sent through Meta's servers.

**What changes in the roadmap:**
- **Freeze** the patient native app instead of spending effort on its Expo SDK 53 → 57 upgrade.
- **Put that effort into the WhatsApp channel and the web companion.**
- The caregiver app still gets upgraded and fixed.
- The critical backend security fixes are needed **whichever way we go**, and they must come first.

**Rough cost.** Meta's messaging fees come to about **$0.08–$0.17 per booking** (≈ ₦125–260 at the rate assumed in §9). The Paystack fee on a typical booking is about **₦760**. Both are small next to a ₦44,000 booking.

---

## 2. Where Oakly is today

### 2.1 Product

Oakly is a home-healthcare marketplace. A patient, or a family member booking for them, orders a **nurse, doctor, physiotherapist, caregiver or nutritionist** for a home visit of **1–24 hours** and pays through **Paystack** in NGN. Once a patient pays, the backend notifies up to 8 nearby approved caregivers and the first one to accept gets the job. Around this core sit:
- vitals logging (encrypted) with Gemini AI insights
- the **FallGuard** fall-risk questionnaire
- live caregiver tracking
- patient–caregiver chat
- prescriptions
- wallets for both sides

### 2.2 Current pricing (taken from the code and the live API)

The formula comes from `app/services/booking.py:36-115` (preview) and `:128-152` (create):

```
service_fee   = duration_hours × base_fee × multiplier[service_type]   (multiplier defaults to 1.0 when missing)
if surge_pricing:  service_fee ×= surge_multiplier
if discount:       service_fee ×= (1 − discount%)
platform_fee  = service_fee × platform_fee_percent%
total         = service_fee + platform_fee          → charged to the patient through Paystack
```

**Live values.** The backend uses the most recent `booking_settings` row. This one was fetched from `GET /api/v1/booking-settings/latest` on 2026-09-29 and was last changed on 2025-07-06:

| Setting | Value |
|---|---|
| `base_fee` | **₦10,000 per hour** |
| `multipliers` | nurse **1.0**, doctor **1.5**, nutritionist **2.0** |
| `platform_fee_percent` | **10%**, added on top and paid by the patient |
| `surge_pricing` | off (multiplier 1.0) |
| `discount` | 0% |

**Worked examples at today's live settings:**

| Booking | Service fee | Platform fee | **Patient pays** |
|---|---|---|---|
| Nurse, 4 h | ₦40,000 | ₦4,000 | **₦44,000** |
| Doctor home visit, 1 h | ₦15,000 | ₦1,500 | **₦16,500** |
| Nutritionist, 2 h | ₦40,000 | ₦4,000 | **₦44,000** |
| Caregiver, 8 h shift | ₦80,000 | ₦8,000 | **₦88,000** |
| Caregiver, 24 h | ₦240,000 | ₦24,000 | **₦264,000** |

**Prescription service fee.** This comes from `app/services/prescription.py:14-26` and the live `GET /api/v1/prescription/settings`. It's a flat fee per request, not the price of any drug:

```
total_cost = base_fee (₦10,000) × multiplier (urgent 2.0 | regular 1.0 | "Home Delivery" 1.0) × surge 1.5 × (1 − 10% discount)
→ regular ₦13,500 · urgent ₦27,000
```

### 2.3 Gaps in the current pricing that WhatsApp would make visible

The bot will quote exact prices in chat, so these need fixing before launch:

1. **Physiotherapist and caregiver have no multiplier configured.** They silently fall back to 1.0, the nurse rate. Decide their rates explicitly.
2. **No city dimension.** Lagos and Abuja are priced the same, and there's no transport or distance component.
3. **No doctor teleconsultation product.** A doctor is priced per hour as a *home visit* only. Remote consultations need a new price per session (§7.4).
4. **Caregiver payout is undefined.** The code never writes an `earning` transaction, so caregivers' wallets never receive booking income. We need to decide the payout split and when it's paid.
5. **Hourly only.** There are no packages: recurring visits, live-in care, or monthly elderly-care plans. Family buyers typically want exactly these.
6. **Paystack's fee is taken from the settlement,** so it comes out of Oakly's 10% platform fee. At about ₦760 per ₦44k booking, that's roughly 17% of the platform fee.
7. **Money is stored as `Float`.** Move to integer kobo before scaling payments.
8. **Surge exists but is manual.** The demand-based `get_booking_demand_surge_multiplier` is never called. Decide whether surge applies in chat, and how it's disclosed to the patient.
9. **Unpaid bookings never expire.** `auto_cancel_unpaid_bookings` is never scheduled. Payment links sent in chat need an expiry, for example 30 minutes.

### 2.4 Technical state (from the review on 2026-09-24)

- **Backend:** the most complete part, but it has **critical security and money bugs** that block any launch. See `docs/checklist/MASTER_CHECKLIST.md` §1–2.
- **Patient app:** auth, booking and payment, FallGuard and most of vitals use the real backend; everything else is mocked. It's on Expo SDK 53, four versions behind.
- **Caregiver app:** an early prototype; its core flows don't work end to end.

---

## 3. What WhatsApp can and can't do for a Nigerian health service (September 2026)

| Topic | Finding | What it means for Oakly |
|---|---|---|
| **Healthcare on WhatsApp** | Meta's Business Messaging Policy (updated 2026-09-23) says: *don't use WhatsApp for telemedicine or to send or request health information **if applicable regulations prohibit** distributing it to systems that don't meet heightened requirements.* This depends on local law; it isn't a blanket ban. | Allowed with care. Keep clinical content (vitals, diagnoses, prescriptions) **off WhatsApp** and behind authenticated links. |
| **Commerce Policy** | Prohibits selling or promoting prescription or other drugs and "medical and healthcare products". Nigeria is not one of the markets with an OTC exception. | The bot must never sell or fulfil medication. Prescription requests go to the web companion and a partner pharmacy. |
| **Payments** | Native WhatsApp payments (`order_details`) work only in **India, Brazil and Singapore**. ⚠️ An iOS beta mentions Nigeria (Legit.ng, 2026-08-25), but nothing has launched. | Use **Paystack payment links** sent in chat, confirmed by Paystack's webhook. Asking for card or account numbers in chat is itself against Meta policy. |
| **Location** | The Cloud API receives **static** location pins and can send a "request location" message. It can't receive **live** location. | Collect the visit address as a pin plus text. Live tracking has to happen in the web companion. |
| **WhatsApp Flows** | Native forms: text, dropdowns, radio buttons, checkboxes, **date and calendar pickers**, photo and document pickers, opt-in controls. Up to 50 components per screen. A data endpoint must answer within **10 s** and uses RSA-OAEP + AES-GCM encryption. No map component. | Good for **booking**, **FallGuard** and **consent**. |
| **AI policy** | From 2026-01-15, the Business Solution Terms bar providers whose **primary** functionality is AI, "as determined by Meta in its sole discretion". | A booking bot with AI on the side is outside the ban. **Don't market it as an "AI health assistant".** |
| **24-hour window and templates** | Free-form replies are allowed only within 24 h of the user's last message. Otherwise only pre-approved templates can be sent. Opt-in is required. Each number has a quality rating. | Every notification (reminders, "caregiver on the way") needs an approved **utility** template. Promotional wording gets a template reclassified as **marketing**, which costs about 8× more. |
| **Messaging limits** | Since October 2025 they're counted per business portfolio, only for unique users reached outside the 24 h window. Tiers go 250 → 2K → 10K → 100K → unlimited. | Starting at 250 is fine for a Lagos pilot. Business verification raises the limit. |
| **Encryption** | Messages are encrypted from the user to **Meta's Cloud API**. Meta decrypts them, keeps them for up to 30 days, and processes them in its global data centres. ⚠️ I couldn't confirm Local Storage in any African region. | Legally this is a **cross-border transfer of sensitive data**, which drives the compliance design in §8. |

**Market fit:**
- Nigeria has **109M internet users** (45.5% penetration) and **165M mobile connections** (DataReportal, Digital 2026).
- ⚠️ About **98% of internet users are on WhatsApp**, from secondary sources.
- Comparable products:
  - **AwaDoc** (Nigeria, April 2025) runs AI symptom checking on WhatsApp and upsells consultations and home care, with apps planned later.
  - **MomConnect/Turn.io** (South Africa) has about 5M users and roughly 10× the engagement of SMS.
  - **WellaHealth** chose to live inside other companies' apps (Palmpay, MTN, Airtel) instead of its own. Distribution matters more than owning an app.

---

## 4. The recommended model

```
                         ┌────────────────────────────── Oakly Backend (FastAPI, Postgres) ─────────────────────────────┐
                         │  Booking · Pricing · Payments (Paystack) · Matching · Vitals · FallGuard · Prescriptions     │
                         │  Consent registry · Audit log · Notifications (channel-agnostic)                             │
                         └──────▲───────────────────▲──────────────────────▲─────────────────────▲─────────────────────┘
                                │                   │                      │                     │
          WhatsApp channel ─────┘   Web companion ──┘   Caregiver app ─────┘   Clinician console ┘
          (patients, families)      (patients, via      (nurses, caregivers,    (doctors: consults,
          booking, pay links,       magic links:        physios: jobs, GPS,     notes, e-prescriptions)
          reminders, FallGuard      tracking, vitals,   earnings)
                                    video consults)
```

| Who | Channel | Why |
|---|---|---|
| Patient / family booker | **WhatsApp** | No app to install. The phone number is already verified. It's where families already coordinate care. |
| Patient (sensitive views) | **Web companion** (PWA, opened by magic link) | Live tracking, charts, clinical data kept off Meta's servers, video consultations |
| Nurse / caregiver / physio | **Native caregiver app** | Background GPS, KYC documents, instant job acceptance, earnings. Staff can be required to install it. Job alerts can *also* go to them on WhatsApp. |
| Doctor | **Web clinician console** | Consultation queue, video, clinical notes, e-prescription; works on a desktop in a clinic |
| Oakly operations | **Admin console** (web) | Caregiver approval, refunds, pricing, disputes. This is missing today and is needed for any model. |

---

## 5. Who the users are and their main journeys

### 5.1 Personas

- **Self-booking patient.** An adult booking care for themselves, such as post-operative nursing or chronic-condition monitoring.
- **Family booker.** An adult child, often in another city or abroad, booking and paying for a parent. **This is probably our biggest paying segment.** They need updates without the parent having to do anything.
- **Care recipient.** Often elderly, and may not use WhatsApp. **They're a separate person from whoever is paying.**
- **Nurse, caregiver or physio.** Field staff who need job alerts, directions, check-in and check-out, and pay.
- **Doctor.** Does home visits (existing product) and remote consultations (new).

### 5.2 The data model has to change: separate the booker from the care recipient

Today a `Patient` belongs to a single `User` login, and `patient_family_members` has a table but no API. For WhatsApp:

- **`contacts`**: a WhatsApp identity (`wa_id`, phone number, opt-in timestamp, preferred language), linked to a `User`.
- **`care_recipients`**: the person who receives care (name, date of birth, address, conditions, emergency contact). A contact can manage **several** recipients, for example "Mum" and "Dad".
- **`care_relationships`**: links a contact to a recipient, with a role (`self`, `family`, `guardian`) and the **recipient's consent status**.
- Bookings, vitals and FallGuard belong to the **care recipient**. The paying contact is recorded separately on the booking.

> **Consent for someone else's health data.** Under the NDPA, processing health data needs a lawful basis. When a family member books for a parent, we need the **recipient's own consent** (captured on their phone, or signed at the first visit by the caregiver in the app), or a documented guardian or legal basis. Until that consent is recorded, the family member can book and pay, but **can't see the recipient's vitals or clinical data**. ⚠️ Confirm this approach with legal counsel.

### 5.3 Journey A: booking a home visit on WhatsApp

1. **First message:** the patient sends "Hi" to the Oakly number, arriving from a Click-to-WhatsApp ad, a QR code or a website button.
2. **Emergency check first:** the bot always scans for emergency keywords and answers them before anything else (§7.6).
3. **Consent (first time only):** a Flow shows a short notice (what we collect, that it's processed through Meta, and a link to the privacy policy) with explicit checkboxes:
   - (a) terms and privacy
   - (b) processing health data
   - (c) cross-border processing through WhatsApp

   The patient can't continue without (a) and (b).
4. **Menu:** Book a visit · My bookings · Talk to a doctor · FallGuard check · My health dashboard · Help.
5. **Booking Flow** (one form):
   - who it's for: *Myself* or *Someone else*, then choose or add a recipient
   - service type
   - date and time (calendar picker, only available slots)
   - duration
   - notes on the condition
   - address

   Then the bot asks for a **location pin** for the address.
6. **Quote:** the backend `preview` is shown in chat as a breakdown (service fee, platform fee, total). The patient taps **Confirm**.
7. **Payment:**
   - The backend creates the booking and a Paystack transaction, and the bot sends the **payment link** (card, transfer or USSD, whatever Paystack offers).
   - **The link expires after 30 minutes.** The bot sends one reminder, then cancels the booking automatically.
8. **Paystack webhook:**
   - Check the amount against the booking.
   - Handle a repeated webhook for the same payment safely.
   - Mark the booking paid, then send a **"Payment received ✅"** template and start matching caregivers.
9. **Caregiver accepts** in the caregiver app. The patient gets a **"Nurse Ada has been assigned"** template with first name, photo, rating, and a **link to the web companion**.
10. **Day of the visit:** reminders 24 h and 1 h before. When the caregiver taps "On my way", the patient gets a message with the **live tracking link**.
11. **Arrival and check-in:** the caregiver checks in with a code the patient reads out from the chat, which confirms the visit took place.
12. **After the visit:** a summary link (visit notes in the web companion), a 1–5 rating using quick-reply buttons, and a "Book again" button.

### 5.4 Journey B: a family member booking remotely

Same as Journey A, but with *Someone else* at step 5.
- Updates go to the **booker**. Optionally, the **recipient** also gets them if they have WhatsApp and have agreed.
- Clinical visit notes are only visible to the booker once the recipient's consent is recorded.
- A **"care circle"** feature (later phase) lets several family members follow the same recipient.

### 5.5 Journey C: talking to a doctor (teleconsultation)

1. Menu → *Talk to a doctor*. A short triage Flow asks about symptoms, the emergency screen, and whether this is urgent or routine.
2. A price per session is quoted (a new product, §7.4), followed by a Paystack link.
3. On payment, a doctor on the clinician console accepts the consult. The patient gets a **video link** that opens in the web companion (§7.4). There is **no clinical conversation over WhatsApp**.
4. After the call, the patient receives links to a consultation summary and, if needed, an **e-prescription**. Drugs are fulfilled through a partner pharmacy **outside WhatsApp**.

### 5.6 Journey D: FallGuard and vitals

- **FallGuard:** the questionnaire runs as a Flow. The **score is calculated on the server** (today the client sends it, which must change). The result is shown **in the web companion**; the chat only gets "Your FallGuard result is ready → link".
- **Vitals:** entered in the web companion, or by a caregiver during a visit. WhatsApp only sends reminders ("Time to log your blood pressure → link"). **Vitals values never go through WhatsApp.**

---

## 6. Functional scope by phase

| Capability | MVP (Lagos pilot) | Phase 2 | Phase 3 |
|---|---|---|---|
| Consent and sign-up on WhatsApp | ✅ | | |
| Book a home visit (nurse, caregiver, physio) | ✅ | | |
| Quote and Paystack payment link, webhook confirmation | ✅ | | |
| Utility notifications and reminders | ✅ | | |
| My bookings, cancel, reschedule | ✅ list and cancel | reschedule | |
| Emergency redirect | ✅ | | |
| Human handover to Oakly support | ✅ (shared inbox) | | |
| Web companion: booking status and **live tracking** | ✅ | | |
| Booking for someone else (care recipients) | ✅ basic | care circle, recipient consent on their own phone | |
| FallGuard via Flow with the result in the web companion | | ✅ | |
| Vitals in the web companion and reminders | | ✅ | |
| Doctor teleconsultation (clinician console, video, e-prescription) | | | ✅ |
| Doctor home visits | ✅ (as today) | | |
| Packages and recurring plans | | | ✅ |
| Abuja launch | | ✅ | |
| Yoruba / Hausa / Pidgin | | ✅ Pidgin | ✅ more |

---

## 7. Technical design

### 7.1 New backend components

| Component | Responsibility |
|---|---|
| `POST /whatsapp/webhook` + `GET` verification | Receive messages and status updates from Meta. **Check `X-Hub-Signature-256`**, reply 200 immediately, and put the work on a queue. |
| Job queue + workers (Redis + ARQ or Celery) | Meta expects a fast 200. Processing, outbound sends, retries and scheduled reminders run in the background. This also gives us the scheduler that's missing today (auto-cancel, reminders). |
| Conversation state service | A state machine per contact (`idle`, `consent`, `booking_flow`, `awaiting_payment`, …). Stored in Postgres, with a Redis cache. Tracks `last_inbound_at` to enforce the 24 h window. |
| Flow data endpoint | Serves live data to Flows (available slots, recipients, price preview) within the **10 s** limit. Encrypted with RSA-OAEP + AES-GCM. |
| Outbound sender | Template registry, rate limiting, retries, **idempotency on Meta message IDs**, and tracking delivery and read status. |
| Channel-agnostic notification service | Domain events ("booking paid", "caregiver en route") map to a WhatsApp template, a caregiver-app push notification, or email. The existing booking code calls this instead of calling WhatsApp directly. |
| Magic-link auth | Signed, single-use, short-lived (e.g. 15 min) tokens tied to a contact, which open a web companion session. Sensitive views need a WhatsApp OTP (**authentication** template) or a device-bound session. |
| Consent registry | Stores the consent version, timestamp, channel, and who gave consent for whom. Records withdrawals. **Read before any processing of health data.** |
| Audit log | Records who viewed or changed clinical data and when. NDPA accountability, and required anyway for health data. |

### 7.2 Library and vendor choices

| Decision | Recommendation | Why |
|---|---|---|
| Meta direct vs a BSP | **Meta Cloud API directly** | No platform fee. We already run a backend. BSP fees: Twilio +$0.005/msg; 360dialog €49–249/month; Gupshup +$0.001/msg and 6% on marketing from 2026-01-01; Infobip custom. ⚠️ Nigerian BSPs reportedly charge ₦90–100k/month. |
| Python SDK | **PyWa** | Async, fits FastAPI, handles Flows encryption, actively maintained |
| Support inbox | Start with a simple ops console in the admin app, or a low-cost shared inbox | Handover to a human must work from day one in a health service |
| Web companion | Next.js or a lightweight React PWA | Shares types with the caregiver app; installable; browser geolocation not needed on the patient side |
| Video consultations | A managed video provider with a BAA or DPA and a data-location statement (e.g. Daily, Whereby), or self-hosted Jitsi | Keeps clinical video off WhatsApp. ⚠️ WhatsApp's Business Calling API exists, but I haven't confirmed it for Nigeria. I'd **avoid** it for clinical calls anyway (records, consent, policy). |

### 7.3 Paystack in a chat flow

- **Paystack requires an email** to create a transaction. WhatsApp users may not have given one.
  - Collect an **optional email** in the booking Flow for receipts.
  - Otherwise use a deterministic internal address such as `wa-<hash>@pay.oaklycare.com` and send receipts in chat.
  - ⚠️ Confirm Paystack's terms allow this.
  - Today `create_booking` **fails** when the patient has no email (`services/booking.py`), so this has to change.
- **Before the WhatsApp launch** (these are already on the checklist):
  - check the webhook amount
  - handle repeated webhooks safely
  - expire links and cancel unpaid bookings automatically
  - move amounts to integer kobo
  - fix the refund double-payment
- **Fees ⚠️:** Paystack's published local card fee is 1.5% + ₦100, with the ₦100 waived under ₦2,500 and a ₦2,000 cap. Confirm the current rate, then decide whether the patient or Oakly pays it.

### 7.4 Doctor teleconsultation (new product)

- **Pricing:** a price per session (e.g. 15 or 30 min), separate from the hourly home-visit rate. Add a `service_mode` (`home_visit` | `teleconsult`) to bookings and pricing.
- **Clinician onboarding:**
  - check the doctor's **MDCN licence** (number, annual practising licence, expiry)
  - record their specialty
  - **the admin console must approve them** before they can accept consultations
- **During the consultation:** a video room link opens for the patient and the doctor. Notes are written in the clinician console and stored encrypted. An e-prescription is produced as a signed PDF or record and shown in the web companion.
- **Regulation** ⚠️:
  - There's no specific telemedicine law in Nigeria. Practice rests on the MDCN Code of Medical Ethics, the Medical and Dental Practitioners Act, and the NHIA Act 2022.
  - A vendor blog claims MDCN guidance rules out plain WhatsApp for consultations. I **couldn't find the primary MDCN document**. Our design doesn't run consultations over WhatsApp, so either way we're on the safe side.
  - **Get a Nigerian healthcare lawyer's sign-off before Phase 3.**

### 7.5 Message templates to submit for approval (utility unless noted)

| Template | Trigger |
|---|---|
| `booking_payment_link` | Booking created (usually sent inside the 24 h window, but kept as a template for resends) |
| `payment_received` | Paystack webhook confirms payment |
| `payment_link_expiring` | 20 min after the link was sent, if still unpaid |
| `booking_cancelled_unpaid` | Auto-cancel |
| `caregiver_assigned` | A caregiver accepts |
| `no_caregiver_found` | Matching timed out, with refund or rebook options |
| `visit_reminder_24h`, `visit_reminder_1h` | Scheduler |
| `caregiver_en_route` | The caregiver taps "On my way", with the tracking link |
| `visit_completed_feedback` | The caregiver checks out |
| `result_ready` | FallGuard or consultation summary ready (link only, **no clinical content**) |
| `vitals_reminder` | Recurring reminder set by the patient |
| `consult_starting` | 5 min before a teleconsultation, with the video link |
| `refund_update` | Refund approved or paid |
| `login_code` (**authentication**) | OTP for sensitive web companion views |
| `reengagement_offer` (**marketing**) | Only with marketing opt-in; sparingly |

**Rules for writing templates:**
- no diagnoses, drug names or vitals values in any template
- no promotional wording in utility templates, because Meta reclassifies them automatically
- always offer an opt-out ("Reply STOP")

### 7.6 Safety rules in the bot (not optional for a health service)

- **Emergency screen on every inbound message.** Keywords like "chest pain", "can't breathe", "unconscious", "bleeding", "stroke", "suicide", plus Pidgin variants, trigger an immediate reply: *"This may be an emergency. Call 112 now…"* with the relevant Lagos and Abuja emergency lines, then a handover to a human.
  - ⚠️ Confirm the current emergency numbers for each state before launch.
- **The bot never diagnoses or recommends treatment.** Gemini-generated text is used only for summaries inside the web companion, reviewed as today's vitals insights are, and always carries a "not medical advice" note.
- **Human handover** is always available from the menu ("Talk to a person") and triggers automatically after repeated failures to understand the user.

---

## 8. Compliance and privacy

| Requirement | How we meet it |
|---|---|
| **NDPA 2023:** health data is *sensitive personal data* and needs explicit consent or another lawful basis | An explicit consent Flow with separate checkboxes. Consent is stored with its version. Withdrawing is as easy as giving consent (the "STOP DATA" command and a web setting). |
| **Cross-border transfer** (Meta processes data outside Nigeria) | Informed consent that names Meta or WhatsApp processing abroad. **Minimise what goes through WhatsApp**: operational messages only, with clinical data kept in our systems and reached by links. |
| **GAID 2025** (in force 2025-09-19): explicit, informed, withdrawable consent; data-protection impact assessments where required | Run a **DPIA** before the pilot, covering the WhatsApp channel, the web companion and AI insights |
| **Controller registration and DPO** ⚠️ | We process health data at scale, so Oakly is likely a "data controller of major importance". It would need to **register with the NDPC** and appoint a **DPO**. Confirm with counsel. |
| **Retention** | Keep the message log to what operations need (IDs, template names, timestamps), not message content. Define retention periods for chat logs versus clinical records. |
| **Security baseline** | The critical backend fixes (checklist §1–2) are a **precondition**: rotate the leaked secrets, authenticate the WebSockets, lock down the prescription endpoints, make uploads private. |
| **Meta policy** | Opt-in before any outbound message; no drug sales; no card or account numbers in chat; utility templates stay non-promotional; don't position the bot as an AI assistant |

---

## 9. Cost model

**Meta pricing** (per delivered template message, by category and recipient country, since 2025-07-01). ⚠️ The Nigeria rates below come from a secondary source quoting Meta's April 2026 rate card. **Check Meta's official rate card before budgeting.**

| Category | USD per message (Nigeria) |
|---|---|
| Utility | ~$0.0067 |
| Authentication | ~$0.0067 |
| Marketing | ~$0.0516 |
| Service (free-form replies inside the 24 h window) | **Free until 2026-09-30.** From **2026-10-01** they're billed at the utility rate. ⚠️ 360dialog reports 1,000 free service messages per number per month; Meta's page doesn't confirm this. |

> **Action for October 1, 2026:** Meta stops delivering service messages for accounts **without a payment method on file**. We don't have a Business account yet, so this applies from our first day.

**Cost per booking.** About 12–25 outbound messages per booking (menu, Flow, quote, payment link, confirmation, assignment, two reminders, en route, feedback, plus back-and-forth). Inbound messages from users are free.

| | Low | High |
|---|---|---|
| Outbound messages per booking | 12 | 25 |
| Meta cost (USD) at ~$0.0067 | **$0.08** | **$0.17** |
| In NGN at an **assumed ₦1,550/$** ⚠️ (illustrative; replace with the current rate) | ≈ ₦125 | ≈ ₦260 |
| Paystack fee on a ₦44,000 booking ⚠️ (1.5% + ₦100, capped at ₦2,000) | ≈ ₦760 | ≈ ₦760 |
| **Channel cost as a share of a ₦44,000 booking** | **≈ 2.0%** | **≈ 2.3%** |

**Monthly illustration for the pilot:** 500 bookings per month is about $40–85 in Meta fees. A full-time support agent will cost far more than the messaging, so plan headcount before plumbing.

**One-off and fixed costs:**
- **Meta:** business verification is free.
- **Phone number:** a dedicated number that isn't registered on the WhatsApp app.
- **Video provider:** usage-based; estimate this once the Phase 3 volume is known.
- **Web companion hosting:** marginal.
- **Legal review:** DPIA, privacy notice, telemedicine sign-off. Budget for it.

---

## 10. Prerequisites and Meta onboarding (we don't have a verified Business account yet)

1. **A Meta Business portfolio** (business.facebook.com) owned by the company, not a personal account.
2. **Business verification:** CAC certificate of incorporation, proof of address, a matching website domain (oaklycare.com) and a business email on that domain. This can take **days to weeks**, so **start now, while the fixes are underway**.
3. **A dedicated phone number** that isn't registered on the WhatsApp or WhatsApp Business app, able to receive an SMS or voice OTP. Ideally a number that will be used long term (it becomes the brand).
4. **Display name** "Oakly" or "Oaklycare", matching the website branding. Meta reviews it.
5. **A WhatsApp Business Account** on the Cloud API, with a system user and a permanent access token stored as a secret (**never committed**), plus the webhook subscription.
6. **A payment method on file** before sending any messages, required from 2026-10-01.
7. **Official Business Account (green tick)** later. It's optional and needs brand notability.
8. **Templates** (§7.5) submitted early; approval is usually quick, but rejections cost iterations.
9. **Privacy policy and terms** on the website, covering WhatsApp processing, as the opt-in screens will link to them.

---

## 11. Delivery plan

> Rough engineering estimates for a small team (1–2 backend, 1 frontend). They assume the Phase 0 fixes land first.

| Phase | Duration | Deliverables | Exit criteria |
|---|---|---|---|
| **0: Foundations** | 2–3 weeks (in parallel with Meta verification) | Critical security and money fixes (checklist §1–2); job queue and scheduler; notification service; integer-kobo money; pricing gaps decided (§2.3); DPIA and privacy notice drafted | Security items closed; payments safe to replay and amount-checked; Meta business verified |
| **1: Lagos MVP** | 4–6 weeks | Webhook, state machine, consent Flow, booking Flow, quote, Paystack link, templates, reminders, emergency screen, human handover, care recipients (basic), web companion (booking status + live tracking); caregiver app fixed and upgraded to SDK 57 | 50 real bookings end to end; payment completion rate measured; zero clinical data in WhatsApp logs |
| **2: Care and Abuja** | 3–4 weeks | FallGuard via Flow with the result in the web companion; vitals in the web companion with reminders; care circle and recipient consent on their own phone; reschedule; Pidgin; **Abuja launch** (city pricing, caregiver supply) | Abuja live; repeat booking rate tracked |
| **3: Doctors** | 4–6 weeks | Clinician console, MDCN checks, teleconsult pricing, video, e-prescription, partner pharmacy handoff (off WhatsApp); legal sign-off | Lawyer sign-off; first paid teleconsultations |
| **Later** | — | Packages and subscriptions, OBA green tick, more languages, Click-to-WhatsApp ad campaigns (72 h free window), analytics | — |

**Patient native app:** frozen from Phase 0. We revisit after Phase 2 using data from real use. If users ask for it, it can come back as a wrapper around the web companion.

---

## 12. Success metrics

| Metric | Why it matters | Pilot target (to be agreed) |
|---|---|---|
| First message → paid booking | Main measure of whether the channel converts | ≥ 25% |
| Drop-off at each Flow step | Shows where the form loses people | Measured |
| Payment-link completion | Shows friction on the Paystack side | ≥ 70% |
| Time to book (first message → paid) | The whole promise of "no app" | < 5 min median |
| Time from payment to caregiver assigned | Supply health | < 30 min (Lagos) |
| Template quality rating | Risk of Meta throttling or blocking us | Stays "High" |
| Opt-out rate | Whether we message too much | < 2% |
| Repeat bookings within 60 days | Retention, especially among family bookers | Measured |
| Channel cost per booking | Unit economics | < 3% of booking value |
| Clinical data found in WhatsApp payloads (audit) | Compliance guardrail | **0** |

---

## 13. Risks

| Risk | Likelihood / impact | Mitigation |
|---|---|---|
| Dependence on Meta: account restricted, number banned, policy change | Medium / **High** | Keep quality high (opt-in only, useful messages). Keep the web companion as a fallback that works on its own. Keep our own contact list. Consider a backup number. |
| Health-data regulation tightens, or counsel reads the rules strictly | Medium / High | Design for data minimisation now (links, not data); DPIA; counsel review before Phase 3 |
| Prices rise from 2026-10-01, or Nigeria rates change | High / Low | Budget with headroom; keep the conversation design lean; track the actual cost per booking |
| No live location in WhatsApp | Certain / Medium | Web companion tracking link (in the design) |
| Paystack needs an email; payment links are more friction than in-app payment | Medium / Medium | Optional email with an internal fallback; link expiry and reminders; show bank-transfer and USSD options |
| Elderly recipients don't use WhatsApp | High / Medium | The booker model (§5.2) covers it; consent is captured at the first visit through the caregiver app |
| The AI-provider ban is applied to us | Low / High | Keep AI out of sight in the chat; never call it an AI assistant |
| Emergencies handled badly by a bot | Low / **Critical** | Emergency screen on every message; human handover; disclaimers; staff coverage agreed |
| Caregiver supply in Abuja | Medium / High | Recruit before the Phase 2 launch; city-specific pricing |
| Security debt carried into the new channel | **High today** / Critical | Phase 0 comes first. No WhatsApp launch until checklist §1–2 is closed. |

---

## 14. Open decisions for the founder

1. **Payout split:** what share of the service fee does a caregiver or doctor receive, and when (per visit or weekly)?
2. **City pricing:** do Lagos and Abuja get different `base_fee` values, and is there a transport fee?
3. **Missing rates:** set explicit multipliers for **physiotherapist** and **caregiver**, since they currently fall back to the nurse rate.
4. **Teleconsult price:** the fee per 15 or 30 minute session, and the split with the doctor.
5. **Packages:** do we offer recurring or live-in care plans in Phase 3, and at what discount?
6. **Who pays the Paystack fee:** Oakly (as today, out of the platform fee) or the patient?
7. **Support hours:** who takes human handovers, and during which hours? For health, we need an answer for nights and weekends.
8. **Legal:** which law firm does the DPIA and the telemedicine review, and do we register as a data controller of major importance?
9. **Brand and number:** the display name, and which phone number becomes the official WhatsApp line.

---

## 15. Sources

Research was done on 2026-09-29. ⚠️ marks secondary or unconfirmed sources.

- WhatsApp Business Messaging & Commerce Policy (updated 2026-09-23): https://whatsappbusiness.com/policy/
- Healthcare industries allowed on WhatsApp (tyntec FAQ, undated): https://www.tyntec.com/helpcenter/docs/faqs/whatsapp-business/whatsapp-commerce-policy/what-industries-in-the-health-sector-are-allowed-on-whatsapp/
- Meta pricing: https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing
- Meta, non-template (service) message pricing: https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing/non-template-messages
- 360dialog, October 2026 service message charging: https://360dialog.com/blog/whatsapp-service-message-charging-october-2026/
- ⚠️ Nigeria rate breakdown (Cylique, secondary): https://cylique.com/whatsapp-business-api-nigeria-full-pricing-breakdown/
- Twilio WhatsApp pricing: https://www.twilio.com/en-us/whatsapp/pricing
- Gupshup fee change (January 2026): https://support.gupshup.io/hc/en-us/articles/53700207494169
- Payment links in Nigeria: https://bossbot.uk/blog/payment-links-ng
- ⚠️ WhatsApp payments in Nigeria, beta sighting (Legit.ng, 2026-08-25): https://www.legit.ng/business-economy/technology/1727162-whatsapp-payments-expand-nigeria-iphone-feature-reveals-global-plans/
- WhatsApp payments overview (Infobip): https://www.infobip.com/blog/whatsapp-payments
- Paystack WhatsApp pilot (Techpoint): https://techpoint.africa/brandpress/jollof-boss-launches-whatsapp-paystack-pilot-testing-chat-commerce-for-african-smes/
- PyWa Flows docs: https://pywa.readthedocs.io/en/latest/content/flows/overview.html
- WhatsApp Flows spec reference: https://github.com/gokapso/agent-skills/blob/master/skills/integrate-whatsapp/references/whatsapp-flows-spec.md
- Live location not supported (tyntec): https://www.tyntec.com/helpcenter/docs/faqs/whatsapp-business/message-types-templates/use-cases-can-we-receive-the-live-location-from-our-customers/
- Cloud API data privacy and security: https://developers.facebook.com/documentation/business-messaging/whatsapp/data-privacy-and-security/
- Messaging limits: https://developers.facebook.com/documentation/business-messaging/whatsapp/messaging-limits
- GAID 2025 (NDPC PDF): https://ndpc.gov.ng/wp-content/uploads/2025/03/NDP-ACT-GAID-2025-MARCH-20TH.pdf
- GAID analysis (DLA Piper, June 2025): https://privacymatters.dlapiper.com/2025/06/nigeria-ndpc-issues-gaid-key-compliance-insights/
- NDPA overview (Mondaq): https://www.mondaq.com/nigeria/privacy-protection/1627428/
- Nigeria's telemedicine framework (ThisDay, May 2025): https://www.thisdaylive.com/index.php/2025/05/10/nigerias-telemedicine-regulatory-framework/
- ⚠️ MDCN telemedicine guidelines (vendor blog, unverified): https://clinikehr.com/blog/mdcn-telemedicine-guidelines-nigeria-2026
- DataReportal Digital 2026, Nigeria: https://datareportal.com/reports/digital-2026-nigeria
- ⚠️ WhatsApp penetration in Africa (Yazi): https://www.askyazi.com/articles/whatsapp-penetration-across-africa-statistics-by-country
- AwaDoc (Techpoint, April 2025): https://techpoint.africa/feature/awadoc-aproko-doctor-healthtech-startup/
- Turn.io 2025 accelerator: https://www.turn.io/news/turn-ios-2025-chat-for-health-ai-accelerator-wraps-up-with-ai-enabled-solutions-ready-to-scale-across-35-million-strong-networks
- General-purpose AI chatbot ban (respond.io, October 2025): https://respond.io/blog/whatsapp-general-purpose-chatbots-ban
- AI provider ban (MediaNama, October 2025): https://www.medianama.com/2025/10/223-whatsapp-bans-external-ai-providers-business-api/
- EU interim measures (June 2026): https://hansberrytomkiel.com/en/european-commission-orders-interim-measures-against-metas-exclusion-of-rival-ai-chatbots/

**Internal sources:**
- `app/services/booking.py`, `app/services/prescription.py`, `app/db/models/booking_settings.py`, `app/schemas/booking.py`
- Live `GET /api/v1/booking-settings/latest` and `GET /api/v1/prescription/settings` (2026-09-29)
- `docs/checklist/MASTER_CHECKLIST.md`
