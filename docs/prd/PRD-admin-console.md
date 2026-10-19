# PRD — Oakly Admin Console

| | |
|---|---|
| **Product** | Oakly (Oaklycare) home-healthcare marketplace — internal operations console |
| **Document owner** | Product / Engineering |
| **Status** | Draft v1 for review |
| **Date** | 2026-09-30 |
| **Related** | [WhatsApp PRD](PRD-whatsapp-patient-channel.md) · [Proposal](../proposals/2026-09-29-whatsapp-patient-channel.md) · [Master checklist](../checklist/MASTER_CHECKLIST.md) |

> **Conventions**
> - **Requirement IDs.** Every requirement is labelled `AD-FR-n` (functional), `AD-NFR-n` (non-functional) or `AD-US-n` (user story).
> - **Priorities** use MoSCoW: Must, Should, Could.
> - **[DECISION]** marks a choice the founder still has to make. These are collected in §14.
> - **`file:line` references** point to `backend/app/…` as of 2026-09-30 and describe the *current* state.

---

## 1. Summary

The **Admin Console** is a secure web app for Oakly staff. It is used to run the marketplace:
- approve caregivers and doctors
- review their documents
- watch bookings and step in when something goes wrong
- process refunds and caregiver payouts
- set prices
- manage FallGuard content and prescriptions
- answer patients handed over from WhatsApp
- see how the business is doing

Every action is limited by role and recorded in an audit log.

## 2. Problem: what can't be done today

The backend has almost no operational tooling. The review on 2026-09-30 found:

| Gap | Consequence | Evidence |
|---|---|---|
| **No way to approve a caregiver.** Nothing ever sets `profile_status='approved'`, and documents and background checks are never marked verified. | Matching requires approval, so **no booking can be matched**. The caregiver app's "under review" screen is a dead end. | `services/location.py:48`, `services/caregiver.py:91-160` |
| **Caregiver documents are public.** `uploads/` is served by `StaticFiles` without authentication. | ID and licence files can be downloaded by anyone who has the URL. | `main.py:34`, `services/caregiver.py:62-74` |
| **No earnings credited, and withdrawals never settle.** No code creates `earning` transactions, and withdrawals stay `pending` forever. | Caregivers can't be paid through the platform. | `endpoints/caregiver.py:392-432` |
| **Refund admin routes can't be used.** They check `user_type=="admin"`, which no user can have. Approving a refund also both credits the wallet *and* calls a Paystack refund. | Staff can't refund, and if they could, the patient would be paid twice. | `endpoints/patient.py:312-390`, `services/patient_wallet.py:408-474` |
| **No booking operations.** Staff can't list, search, reassign, re-dispatch or cancel bookings. Nothing handles bookings that no caregiver accepts. | Paid bookings can stay `pending` forever with no one noticing. | `services/booking.py:222-394` |
| **Two different admin checks and no roles.** `Admin.permissions` is unused text, and there is no audit trail. The only admin is created by a script with hardcoded credentials. | No least-privilege access and no accountability. | `api/deps.py:88-98`, `create_test_admin.py`, `core/config.py:73-75` |
| **Settings only reachable through the API.** Every price change inserts a new row. The GET routes are public. | Price changes are error-prone and have no history view. | `endpoints/booking_settings.py`, `endpoints/prescription.py:20-71` |
| **FallGuard admin endpoints are called from the patient app.** | Admin features ship inside a consumer app. | `patient/src/services/fallguard.service.ts:61-90` |
| **No support, dispute or rating models.** | Complaints and quality issues can't be tracked. | — |
| **Caregiver form options are hardcoded.** The `states` list contains US states. | Wrong onboarding data. | `services/caregiver.py:162-211` |

## 3. Goals and non-goals

### Goals

| # | Goal | Measure |
|---|---|---|
| G1 | Get approved caregivers into the marketplace | Median time from submitted application to decision under 48 hours |
| G2 | Every paid booking is either served or refunded | 0 paid bookings older than 2 hours still unmatched without an owner |
| G3 | Caregivers are paid reliably | 100% of completed visits credited within 1 hour; payouts processed within the SLA **[DECISION]** |
| G4 | Money is correct | Daily reconciliation against Paystack shows 0 unexplained differences |
| G5 | Least privilege and accountability | 100% of admin writes and clinical reads are audited; MFA for every admin |
| G6 | Support is responsive | First response to a WhatsApp handover under 10 minutes during support hours |

### Non-goals (v1)

- A clinical record system (EHR). Clinicians use the separate clinician console (Phase 3 of the WhatsApp PRD). The admin console only shows clinical data when a documented reason is given (AD-FR-73).
- Marketing automation and a CRM.
- Full accounting. The console exports to the finance tools instead.
- Tools for patients or caregivers themselves. They use WhatsApp, the web companion or the caregiver app.

## 4. Users and roles

| Role | Who | Can do |
|---|---|---|
| **Super Admin** | Founders, CTO | Everything, including managing admin users and roles, pricing, and break-glass access to clinical data |
| **Operations** | Ops leads and dispatchers | Bookings (view, reassign, re-dispatch, cancel), caregiver availability, unmatched queue, patients (view with masking) |
| **Verification** | Onboarding and compliance officers | Caregiver and doctor applications, documents, identity and background-check decisions |
| **Finance** | Finance staff | Payments, reconciliation, refunds (approve up to a limit **[DECISION]**), caregiver earnings and payouts, exports |
| **Support** | Support agents | WhatsApp handover inbox, tickets, booking view (masked), cancel-and-refund requests (Finance approves), consent requests |
| **Clinical Reviewer** | Licensed nurse or doctor on staff | FallGuard content, prescription operations, clinical escalations, masked-by-default clinical views with break-glass |
| **Read-only Analyst** | Management and investors (internal) | Overview and reports with aggregated, de-identified data |

- **Roles can be combined.** One person may hold several roles.
- **Permissions are fine-grained and roles bundle them.** Examples of permissions: `bookings.read`, `bookings.reassign`, `refunds.approve`, `payouts.execute`, `caregivers.approve`, `pricing.write`, `clinical.read` (§8.2).

## 5. Scope and phasing

| Module | v1 (needed for the WhatsApp Lagos MVP) | v1.1 | v2 |
|---|---|---|---|
| Admin authentication, roles and permissions, audit log | ✅ | | |
| Overview dashboard | ✅ basic KPIs | charts, trends | cohort and retention reports |
| Caregiver onboarding and verification queue | ✅ | re-verification when documents expire | automated KYC (Prembly/Onfido) review assist |
| Documents (private storage and viewer) | ✅ | | |
| Bookings operations and unmatched queue | ✅ | map view | demand heatmap |
| Payments and reconciliation | ✅ list and reconcile | automated daily report | |
| Refunds | ✅ | partial refunds | |
| Caregiver earnings and payouts | ✅ manual batch through Paystack Transfers | scheduled weekly payouts | |
| Patients and care recipients | ✅ masked view, suspend | merge duplicates | |
| Pricing settings | ✅ versioned editor with preview | city pricing | packages and plans |
| Support inbox (WhatsApp handover) | ✅ | canned replies, SLAs | CSAT |
| Ratings and quality | ✅ list, flag | caregiver quality score | |
| FallGuard content | ✅ | versioning | |
| Prescriptions operations | | ✅ | pharmacy partner portal |
| WhatsApp operations (templates, opt-outs, consent) | ✅ read-only | template sync | |
| Clinicians (doctors) | | | ✅ (with WhatsApp Phase 3) |
| Reports and exports | ✅ CSV | scheduled | |
| Settings: service areas, form options | ✅ | | |

---

## 6. User stories (selection)

- **AD-US-1** — As a Verification officer, I can see all caregiver applications waiting for review, open one, see the profile, documents and identity results side by side, and approve or reject it with a reason, so that qualified caregivers can start taking jobs.
- **AD-US-2** — As an Ops dispatcher, I can see every paid booking that has not been matched within the SLA, and either re-dispatch it or assign a specific approved caregiver.
- **AD-US-3** — As Finance, I can review refund requests together with the booking and payment context, approve or reject them, and know the money goes back **once**.
- **AD-US-4** — As Finance, I can see caregivers' earned balances, build a payout batch, execute it through Paystack Transfers, and see which transfers succeeded or failed.
- **AD-US-5** — As a Support agent, I get WhatsApp conversations handed over by the bot, see the contact's bookings, reply in the thread, and hand back to the bot.
- **AD-US-6** — As a Super Admin, I can change pricing with a preview of the effect on sample bookings, schedule when it takes effect, and see who changed what.
- **AD-US-7** — As a Super Admin, I can invite an admin, give them roles, require MFA, and deactivate them.
- **AD-US-8** — As a Clinical Reviewer, I can edit FallGuard questions and answer scores, and publish a new version without breaking past assessments.
- **AD-US-9** — As management, I can see today's and this month's bookings, revenue, the match rate and supply at a glance.
- **AD-US-10** — As any admin, whenever I open a patient's clinical data, I must give a reason, and the access is logged.

---

## 7. Functional requirements by module

### 7.1 Overview dashboard

| ID | Requirement | Priority |
|---|---|---|
| AD-FR-1 | **KPI tiles** for today, last 7 days and month to date: bookings created, bookings paid, match rate (accepted ÷ paid), completed visits, cancellations (by who cancelled), GMV (paid `total_amount`), platform revenue (`platform_fee`), refunds issued, new patients, new approved caregivers. Filters: **city** and **service type**. | Must |
| AD-FR-2 | **Attention panel**, a list of items that need someone to act, each linking to its queue: unmatched paid bookings past the SLA, applications waiting more than 48 hours, refund requests waiting, payout failures, webhook amount mismatches, open urgent WhatsApp handovers, and a template quality-rating drop. | Must |
| AD-FR-3 | **Supply now:** approved caregivers online and available, per city and service type, based on `caregiver_locations.updated_at` within the last 15 minutes. | Must |
| AD-FR-4 | **Funnel** from the WhatsApp PRD §15: first message through to paid booking, with conversion per step. | Should |
| AD-FR-5 | **Time-series charts** for bookings, GMV and match time (median and p90). | Should |
| AD-FR-6 | Dashboard numbers are **aggregates only**. No patient names appear on the Overview. | Must |

### 7.2 Caregiver onboarding and verification

**Current fields used:** `caregivers.profile_completion_step`, `profile_status` (incomplete, under_review, approved, rejected), `identity_verification_status` (pending, in_progress, verified, failed), `background_check_status`, consent flags, `document_verification_result`/`face_verification_result` JSON, `caregiver_documents.verification_status` (pending, verified, rejected).

| ID | Requirement | Priority |
|---|---|---|
| AD-FR-7 | **Applications queue.** Lists caregivers whose `profile_status='under_review'` (and optionally `incomplete` with how far they've got). Columns: name, city/state, specialization, years of experience, submitted date, age in days, document count, identity status, background-check status. Sort by oldest first. Filter by city and specialization. | Must |
| AD-FR-8 | **Application detail.** Shows the profile (step 1 and 2 fields), consents with timestamps, service areas, preferred hours, every document shown inline (AD-FR-15), Prembly/Onfido results shown as readable fields rather than raw JSON (ID numbers masked; Verification role sees the last 4 digits), and the application's history. | Must |
| AD-FR-9 | **Document decisions.** Each document can be marked **Verified** or **Rejected** with a reason, which sets `verification_status` and `verified_at`. Expiry date is captured for licences. | Must |
| AD-FR-10 | **Identity and background decisions.** Set `identity_verification_status` to `verified` or `failed`, and `background_check_status` to `cleared` or `failed` (new value). Each decision needs evidence notes. The status the client sends through `POST /caregiver/identity-verification/result` is **not trusted for approval**; it is stored only as a hint. | Must |
| AD-FR-11 | **Approve.** Only enabled when the required documents are verified, identity is verified **and the background check is cleared** (a background check is **mandatory before approval**, decided 2026-09-30). Approval sets `profile_status='approved'` and records `approved_by`/`approved_at`. It also sends the caregiver a push notification and email. | Must |
| AD-FR-12 | **Reject / request changes.** Rejection needs a reason chosen from a list plus free text. "Request changes" sends the caregiver back to fix specific items, and the app shows those items. | Must |
| AD-FR-13 | **Skills.** When approving, the officer assigns **structured service skills** (`nurse`, `caregiver`, `physiotherapist`, `doctor`, `nutritionist`) and sub-specialties (Elderly Care, Dementia, Post-operative, …). This replaces matching on free-text specialization (WhatsApp PRD P2). | Must |
| AD-FR-14 | **Suspend / reinstate** an approved caregiver, with a reason. A suspended caregiver is not matched and their active future bookings go to the Ops queue for reassignment. | Must |
| AD-FR-14a | **Expiry monitoring.** Licences expiring within 30 days are flagged, and the caregiver is reminded. When a licence expires, the caregiver is automatically set to `suspended` pending re-verification. | Should |

### 7.3 Documents (private storage)

| ID | Requirement | Priority |
|---|---|---|
| AD-FR-15 | Caregiver documents move from the public `uploads/` mount to **private object storage** (Cloudinary authenticated assets or S3). The public `/uploads` mount is removed for documents. | Must |
| AD-FR-16 | Documents open through **short-lived signed URLs** (5 minutes) issued only to roles with `documents.read`. Each view is audited. | Must |
| AD-FR-17 | The viewer handles images and PDFs inline, with zoom and rotate. There is no bulk download. Downloading requires the Super Admin role and is audited. | Must |
| AD-FR-18 | A migration moves existing files, and Ops verifies the count. | Must |

### 7.4 Bookings operations

**Current fields used:** booking `status` (pending, accepted, in_progress, completed, cancelled, declined), `payment_status` (pending, paid, failed, refunded), plus the new fields from the WhatsApp PRD §10 (`channel`, `care_recipient_id`, dispatch waves).

| ID | Requirement | Priority |
|---|---|---|
| AD-FR-19 | **Bookings list.** Search by booking code, patient phone or name, or caregiver. Filter by status, payment status, city, service type, channel, and scheduled or created date range. Columns: code, scheduled time (WAT), service, city/area, patient (masked), caregiver, status, payment, total, channel, age. | Must |
| AD-FR-20 | **Booking detail.** Timeline of every state change with who made it, price breakdown, payment attempts (Paystack reference, status, amount), dispatch waves and caregiver responses (`booking_notifications.response`, `response_at`), chat or handover link, rating, and refund. The address is shown at area level; the full address requires the `bookings.address` permission. The **condition** and **notes** are masked (clinical, see AD-FR-73). | Must |
| AD-FR-21 | **Unmatched queue.** Paid bookings not accepted within the SLA (default 30 minutes; WhatsApp PRD WA-FR-29), sorted by scheduled time. Actions: **Re-dispatch** (widen radius or skills), **Assign caregiver**, **Contact patient** (opens the support thread), **Cancel & refund**. | Must |
| AD-FR-22 | **Assign caregiver.** Picks from approved, available caregivers with the right skill, sorted by the matching score, showing distance and ETA. The caregiver still has to accept in the app, unless a Super Admin forces the assignment with a reason. | Must |
| AD-FR-23 | **Cancel booking (admin).** Requires a reason code (patient request, no supply, caregiver no-show, safety, duplicate, other) and a refund choice (full, partial, none), which creates a refund (§7.6). The patient is notified through their channel. | Must |
| AD-FR-24 | **Reassign** an accepted booking to another caregiver, for example after a no-show. The original caregiver is notified. | Must |
| AD-FR-25 | **Manual status correction** (Super Admin only). For example, mark `completed` when the caregiver forgot to check out. A reason is required, it is audited, and it triggers the normal side effects: crediting earnings (AD-FR-38) and the feedback request. | Must |
| AD-FR-26 | **No-show and lateness flags.** Automatically flag a booking when the caregiver hasn't checked in 15 minutes after the scheduled start. The flag appears in the Attention panel. | Should |
| AD-FR-27 | **Live map** of active visits and online caregivers (city view). | Could (v1.1) |

**The booking state machine the console relies on** (this replaces today's partial one). **Bold** marks a new state or transition:

```
pending(payment_pending) ──paid──▶ pending(dispatching) ──accept──▶ accepted ──en_route──▶ **en_route** ──check-in──▶ in_progress ──check-out──▶ completed
        │                               │                             │                                                  │
        └─expire (30m)─▶ cancelled      └─no accept (SLA)─▶ **unmatched** (admin queue)                                    └─(admin correction)
                                         any pre-start state ──cancel(by patient/admin/caregiver, with reason)──▶ cancelled (+ refund per policy)
```

The existing `declined` status is dropped from use: a decline is recorded on the notification, not on the booking.

### 7.5 Payments and reconciliation

| ID | Requirement | Priority |
|---|---|---|
| AD-FR-28 | **Payments list.** All Paystack transactions (booking and wallet) with reference, type, amount (kobo shown as ₦), status, channel (card, transfer, USSD), fees, linked booking, created and paid times. | Must |
| AD-FR-29 | **Webhook exceptions.** A queue of events that failed validation: amount mismatch, unknown reference, repeated event, signature failure (count only). Finance can resolve an item by linking, refunding or ignoring it, with a note. | Must |
| AD-FR-30 | **Daily reconciliation.** Compare Oakly's `paid` records with the Paystack transaction list and settlement report for the day. Show matched, missing-in-Oakly and missing-in-Paystack items, and export to CSV. | Must |
| AD-FR-31 | **All money in integer kobo** throughout the console and the APIs. This depends on the kobo migration (WhatsApp PRD P10). | Must |

### 7.6 Refunds

**Current state:** `patient_refund_requests` (pending, approved, rejected) only covers wallet transactions. Refund requests can't be tied to Paystack booking payments. Approving one both credits the wallet and calls a Paystack refund.

| ID | Requirement | Priority |
|---|---|---|
| AD-FR-32 | **Refund model v2.** A refund is attached to a **payment** (a booking payment or a wallet deposit) and has: amount in kobo, reason code, requester (patient, support, system), destination (**original payment method** through a Paystack refund, *or* **wallet credit** — **exactly one**), status (requested, approved, processing, succeeded, failed, rejected), approver, and Paystack refund ID. | Must |
| AD-FR-33 | **Refund queue** with booking context (status, cancellation policy result, whether the caregiver was assigned or had arrived) and the refund amount the policy suggests. | Must |
| AD-FR-34 | **Approve.** Finance approves up to a limit **[DECISION: e.g. ₦100,000]**; above that, a Super Admin must approve. Approval starts **one** refund. The wallet is never credited and refunded to the card at the same time. | Must |
| AD-FR-35 | **Refund status** updates from the Paystack `refund.processed` / `refund.failed` webhooks (today these are only logged, `payment.py:108-110`). The booking's `payment_status` becomes `refunded`, or partially refunded (new state). | Must |
| AD-FR-36 | The patient is notified at each refund status change through their channel (the WhatsApp `refund_update` template, or email). | Must |
| AD-FR-37 | **Partial refunds** (for example, a cancellation fee is kept). | Should (v1.1) |

### 7.7 Caregiver earnings and payouts

**Current state:** `caregiver_wallets.balance` (kobo) and `caregiver_transactions` (earning or withdrawal; pending, successful, failed). Earnings are never credited, and withdrawals never settle.

| ID | Requirement | Priority |
|---|---|---|
| AD-FR-38 | **Earnings are credited automatically on completion.** When a booking moves to `completed`, the system creates an `earning` transaction of `service_fee × payout_share` **[DECISION: payout share]**, in `pending_clearance` status for a hold period **[DECISION: e.g. 24 h, the dispute window]**, after which it becomes available. | Must |
| AD-FR-39 | **Caregiver ledger view.** Opening balance, earnings, adjustments, withdrawals, available vs pending balance, and bank account (account number masked to the last 4 digits). | Must |
| AD-FR-40 | **Payout batches.** Finance selects caregivers with an available balance of at least the minimum **[DECISION]**, reviews the batch total, and executes it through **Paystack Transfers**: create a transfer recipient for each bank account, then a bulk transfer. Each transfer's status comes from the `transfer.success` / `transfer.failed` / `transfer.reversed` webhooks. | Must |
| AD-FR-41 | **Caregiver-requested withdrawals.** Today's `POST /caregiver/wallet/withdraw` becomes a request that joins the next batch, or is executed individually if Finance chooses. It validates `amount > 0` and ≤ the available balance, with a row lock. Today's endpoint accepts negative amounts. | Must |
| AD-FR-42 | **Adjustments.** Manual credits or debits (bonus, penalty, correction) need a reason and Super Admin approval, and are audited. | Must |
| AD-FR-43 | **Bank account verification.** Account numbers are resolved through Paystack's resolve-account before they're saved; a name mismatch is flagged for Verification. | Should |
| AD-FR-44 | Payslip or statement CSV for each caregiver per period. The existing `GET /caregiver/wallet/payslip` is reused. | Should |

### 7.8 Patients, contacts and care recipients

| ID | Requirement | Priority |
|---|---|---|
| AD-FR-45 | **People search** by phone, email, name or booking code. Results show the **contact** (booker) and linked **care recipients**, WhatsApp opt-in and consent state, number of bookings, lifetime spend, and flags. | Must |
| AD-FR-46 | **Profile view (masked by default).** Name and phone are partly masked for Support (for example `+234 803 ••• 4521`); the Operations and Super Admin roles see them in full. Medical fields (allergies, medications, conditions, blood group) are **hidden** unless unlocked through AD-FR-73. | Must |
| AD-FR-47 | **Suspend or reactivate** a user account (`users.is_active`), with a reason. A suspended contact's WhatsApp messages go to Support. | Must |
| AD-FR-48 | **Consent records**, read-only: the full consent history per contact and recipient (from the WhatsApp PRD `consents` table). Recording a **guardian or legal-basis** consent needs an uploaded document and the Clinical Reviewer or Super Admin role. | Must |
| AD-FR-49 | **Data subject requests.** Record access, correction and deletion requests, and export a contact's data. Deletion is a **soft delete plus anonymisation**, subject to retention rules. It replaces today's hard `DELETE /auth/profile`. **[Counsel]** | Should |
| AD-FR-50 | **Merge duplicates** (the same person via the app and WhatsApp). | Could |

### 7.9 Pricing and service settings

**Current state:** `booking_settings`: `base_fee` 10,000, `platform_fee_percent` 10, multipliers nurse 1.0, doctor 1.5, nutritionist 2.0, surge off, discount 0. Each save inserts a new row and the latest row wins. `prescription_settings` has several rows.

| ID | Requirement | Priority |
|---|---|---|
| AD-FR-51 | **Pricing editor**, using **versioned price books**. Each version has: `base_fee` per hour (per city from v1.1), a multiplier **for every service type** (the form won't save while any is missing), platform fee %, discount %, surge on/off and multiplier, an effective-from time, and a comment. | Must |
| AD-FR-52 | **Preview before publishing.** Shows the change for sample bookings (each service type at 1, 4, 8 and 24 hours) as current price, new price and the difference. | Must |
| AD-FR-53 | **Publish** needs the Super Admin role, or `pricing.write` plus approval from a second admin **[DECISION]**. Quotes already locked keep their price (WhatsApp WA-FR-20). The version history is viewable, and rolling back is done by publishing an earlier version again. | Must |
| AD-FR-54 | The public GET routes stay (the apps need prices) but return **only the active version**. Write routes move under `/api/v1/admin/pricing`. | Must |
| AD-FR-55 | **Service areas.** Manage cities and areas (LGA lists or polygons) and radius per city; this drives WA-FR-15. | Must |
| AD-FR-56 | **Form options.** Manage the specialization, sub-specialty and state lists the caregiver app uses. The hardcoded US `states` list is replaced with the 36 Nigerian states and the FCT. | Must |
| AD-FR-57 | **Cancellation policy and SLA settings** (fee %, time windows, matching wave size and timeouts, payment-link expiry), edited as config with audit. | Should |
| AD-FR-58 | Prescription service-fee settings get the same editor (v1.1). | Should |

### 7.10 Support inbox (WhatsApp handover)

| ID | Requirement | Priority |
|---|---|---|
| AD-FR-59 | **Inbox** of `support_conversations` created by the bot (WA-FR-40/41), by patients choosing "Talk to a person", or by staff. Each has a priority (**urgent** for an emergency-screen match, high, normal), status (open, pending on the patient, resolved), assignee, and SLA timer. | Must |
| AD-FR-60 | **Thread view.** Shows the handover transcript (retained 90 days), the contact card, their bookings, and quick actions: open booking, cancel & refund request, resend payment link, resend visit code. | Must |
| AD-FR-61 | **Reply from the console.** Free text is sent inside the 24-hour window. Outside it, the agent can only choose an approved template, and the console enforces this using `last_inbound_at`. Attachments are limited to images and PDFs, and scanned. | Must |
| AD-FR-62 | **Hand back to bot.** Resolving a conversation turns automation back on for that contact. | Must |
| AD-FR-63 | **Urgent alerts.** A new urgent conversation raises an in-console alert and sends a notification to the on-call number or channel **[DECISION]**. | Must |
| AD-FR-64 | Canned replies and templates library; CSAT prompt after resolution. | Should (v1.1) |

### 7.11 Ratings and quality

| ID | Requirement | Priority |
|---|---|---|
| AD-FR-65 | **Ratings list.** Stars, comment, caregiver and booking, from `booking_ratings` (new, WhatsApp PRD §10). Low ratings (2 stars or fewer) automatically create a quality review task. | Must |
| AD-FR-66 | **Caregiver quality card.** Average rating, completion rate, acceptance rate and time to accept (from `booking_notifications`), no-show and lateness counts, complaints. | Should |
| AD-FR-67 | **Complaints and disputes.** A ticket type linked to a booking, with outcomes (warning, adjustment, suspension, refund) that link to the matching actions. | Should |

### 7.12 FallGuard content

| ID | Requirement | Priority |
|---|---|---|
| AD-FR-68 | **Question editor** (Clinical Reviewer). Covers question text, options with scores, order, and active flag. This replaces the admin calls currently in the patient app, which will be removed. | Must |
| AD-FR-69 | **Versioning.** Editing a question already used in assessments creates a new version and never hard-deletes (today `DELETE` hard-deletes while answers still reference the question). Past assessments keep the version they were answered against. | Must |
| AD-FR-70 | **Risk bands.** Configure the score thresholds (≤3 Low, ≤7 Moderate, ≤12 High, >12 Very High), now calculated **on the server**. | Should |

### 7.13 Prescriptions operations (v1.1)

**Current state:** `PrescriptionStatus` (pending, available, paid, delivering, delivered, cancelled). The update, status and upload-note routes are **unauthenticated**. `check_availability` is a mock. The patient app isn't wired to the API.

| ID | Requirement | Priority |
|---|---|---|
| AD-FR-71 | **Prescription queue.** Clinical Reviewer or pharmacy-partner operator can see the request, view the doctor's note (signed URL), set availability and price, and move it through the statuses. Every route requires the admin role and is audited. Prescription requests are not sold or fulfilled through WhatsApp. | Should |
| AD-FR-72 | **Pharmacy partners** (v2): partner accounts that see only the prescriptions assigned to them. | Could |

### 7.14 Clinical data access (break-glass)

| ID | Requirement | Priority |
|---|---|---|
| AD-FR-73 | Clinical fields are **hidden by default** for every role. They include: vitals (encrypted), FallGuard answers and summaries (encrypted), booking `condition` and `special_instructions`, visit notes and AI summaries, chat attachments, medical-record fields, and prescriptions. To reveal them, the user needs the `clinical.read` permission, must **enter a reason** (pick from a list plus free text), and gets access for **15 minutes** for that single patient. Each reveal is audited, and Super Admins get a weekly digest. | Must |
| AD-FR-74 | Any role can see whether a patient has records (counts and dates) without revealing their content. | Must |

### 7.15 WhatsApp operations

| ID | Requirement | Priority |
|---|---|---|
| AD-FR-75 | **Templates.** List templates with their Meta category, language, approval status and quality. Alerts on rejection or on a quality drop. | Must |
| AD-FR-76 | **Number health.** Phone number quality rating, messaging tier, and 24-hour send and failure counts by error code. | Must |
| AD-FR-77 | **Opt-outs and blocks.** List contacts who opted out, with the date. Support can see but not override them (only the contact can opt back in). | Must |
| AD-FR-78 | **Message cost report.** Messages by category per day and estimated cost, reconciled with Meta's monthly invoice. | Should |

### 7.16 Clinicians (v2, together with the WhatsApp Phase 3)

| ID | Requirement | Priority |
|---|---|---|
| AD-FR-79 | Doctor applications go through the same verification pipeline as caregivers, plus **MDCN registration number, annual practising licence and expiry**, and specialty. Approval enables teleconsultation. | Should (v2) |
| AD-FR-80 | Teleconsult pricing per session, doctor payout share, and consultation-log oversight (metadata only unless break-glass). | Should (v2) |

### 7.17 Admin users, roles and audit

| ID | Requirement | Priority |
|---|---|---|
| AD-FR-81 | **Admin identity is separate from the patient and caregiver login.** Admins sign in at the console with email, password and **TOTP MFA (required)**. An admin JWT carries `aud=admin` and the admin's permissions. Access tokens last 15 minutes; refresh tokens rotate and are revocable. An **idle timeout of 30 minutes** applies. | Must |
| AD-FR-82 | **One admin check.** Remove the `user_type=="admin"` check. Every admin route uses a single dependency, `require_permission("<perm>")`, backed by `admin_roles` and `admin_role_permissions`. `Admin.permissions` free text is dropped. | Must |
| AD-FR-83 | **Admin management** (Super Admin). Invite by email (single-use link, 48-hour expiry), assign roles, force an MFA reset, deactivate. The last Super Admin can't be removed. | Must |
| AD-FR-84 | **Bootstrap.** The first Super Admin is created with a one-time CLI command that reads its credentials from the environment and forces MFA enrolment on first login. `create_test_admin.py` and the default superuser credentials in `core/config.py` are deleted. | Must |
| AD-FR-85 | **Audit log.** An append-only `admin_audit_log` records: admin ID, action, entity type and ID, a before/after diff (sensitive fields redacted), reason, IP, user agent, timestamp. The console has a searchable audit viewer. Clinical reads and document views are logged too. | Must |
| AD-FR-86 | **Four-eyes approvals** for sensitive actions **[DECISION]**: publishing pricing, adjustments, refunds above the limit, forced assignment, manual status correction. The first admin requests, a second admin approves. | Should |
| AD-FR-87 | Optional **IP allowlist** for the console. | Could |

### 7.18 Reports and exports

| ID | Requirement | Priority |
|---|---|---|
| AD-FR-88 | CSV exports: bookings, payments, refunds, payouts, caregivers, ratings, audit. Exports carry the same masking as the screen. Each export is audited, and exports of PII need `exports.pii`. | Must |
| AD-FR-89 | Scheduled reports: daily ops summary and weekly finance, emailed to a configured list. | Should (v1.1) |

---

## 8. Technical design

### 8.1 Architecture

- **Frontend:** a separate web app, `oakly-admin`.
  - Framework: Next.js (App Router) + TypeScript, with a component library such as shadcn/ui on Radix.
  - The TypeScript API client is **generated from the backend's OpenAPI** (`/api/v1/openapi.json`), filtered to the `admin` tag.
  - Hosted on its own domain (`admin.oaklycare.com`). It is not bundled into any consumer app.
- **Backend:** new routers under `/api/v1/admin/*` in the existing FastAPI app. Each router uses `require_permission`, and all business logic goes through the domain services the WhatsApp channel also uses (pricing, booking, dispatch, refunds, payouts, notifications). The only extra logic is admin-specific.
- **Jobs:** the same Redis job queue and scheduler as the WhatsApp PRD. It runs payout batches, reconciliation, SLA checks (unmatched bookings, no-shows, expiring licences) and report emails.
- **Storage:** private buckets for documents and doctors' notes, served only through signed URLs.

### 8.2 Permission catalogue (initial)

| Permission | Super | Ops | Verif. | Finance | Support | Clinical | Analyst |
|---|---|---|---|---|---|---|---|
| `dashboard.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ (aggregates) |
| `caregivers.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | |
| `caregivers.approve` / `suspend` | ✓ | suspend | ✓ | | | | |
| `documents.read` | ✓ | | ✓ | | | ✓ | |
| `bookings.read` | ✓ | ✓ | | ✓ | ✓ | ✓ | |
| `bookings.dispatch` / `assign` / `cancel` | ✓ | ✓ | | | cancel-request | | |
| `bookings.address` | ✓ | ✓ | | | | | |
| `bookings.correct_status` | ✓ | | | | | | |
| `payments.read` / `reconcile` | ✓ | | | ✓ | | | |
| `refunds.request` | ✓ | ✓ | | ✓ | ✓ | | |
| `refunds.approve` (≤ limit) | ✓ | | | ✓ | | | |
| `payouts.execute` | ✓ | | | ✓ | | | |
| `ledger.adjust` | ✓ | | | request | | | |
| `people.read` (masked) / `people.pii` | ✓ / ✓ | ✓ / ✓ | ✓ / | ✓ / | ✓ / | ✓ / | |
| `people.suspend` | ✓ | ✓ | | | | | |
| `clinical.read` (break-glass) | ✓ | | | | | ✓ | |
| `pricing.write` | ✓ | | | request | | | |
| `settings.write` | ✓ | ✓ | | | | | |
| `support.inbox` | ✓ | ✓ | | | ✓ | ✓ | |
| `fallguard.write` | ✓ | | | | | ✓ | |
| `prescriptions.manage` | ✓ | | | | | ✓ | |
| `whatsapp.read` | ✓ | ✓ | | | ✓ | | |
| `admins.manage` | ✓ | | | | | | |
| `audit.read` | ✓ | | | ✓ | | | |
| `exports.pii` | ✓ | | | ✓ | | | |

### 8.3 Data model changes (in addition to the WhatsApp PRD §10)

| Table | Purpose |
|---|---|
| `admin_users` | Replaces `admins`. Columns: email, name, password hash, MFA secret (encrypted), status, last_login_at, created_by. Admin accounts are **not** `users` rows. |
| `admin_roles`, `admin_role_permissions`, `admin_user_roles` | Role-based access control (§8.2) |
| `admin_audit_log` | AD-FR-85. Append-only. The admin app's DB role has no UPDATE or DELETE grant on it. |
| `admin_approvals` | Four-eyes requests (AD-FR-86) |
| `caregiver_skills` | Structured service skills and sub-specialties (AD-FR-13) |
| `caregivers` (alter) | Adds `approved_by`, `approved_at`, `suspended_at`, `suspension_reason`. `background_check_status` gains `cleared`/`failed`. `profile_status` gains `suspended`. |
| `caregiver_documents` (alter) | Adds `storage_key` (private), `expires_on`, `reviewed_by`, `rejection_reason` |
| `refunds` | Refund model v2 (AD-FR-32). Replaces `patient_refund_requests` for new flows; the old data is migrated. |
| `payout_batches`, `payouts` | AD-FR-40, with Paystack recipient and transfer codes |
| `caregiver_transactions` (alter) | Adds `pending_clearance`, `available_at`, `booking_id` FK, `adjustment` type, `reason` |
| `price_books` | Versioned pricing (AD-FR-51). Replaces "latest row wins". |
| `service_areas`, `form_options` | AD-FR-55/56 |
| `support_conversations`, `support_messages` | AD-FR-59–62 |
| `tickets` | Complaints and disputes (AD-FR-67) |
| `data_subject_requests` | AD-FR-49 |
| `webhook_events` | Stores every Paystack and Meta webhook (signature result, handled flag, idempotency key) for AD-FR-29 |

All tables are created through Alembic, after the migration graph is fixed. Money columns are **BIGINT kobo**.

### 8.4 Admin API surface (v1)

| Area | Endpoints (all under `/api/v1/admin`) |
|---|---|
| Auth | `POST /auth/login`, `POST /auth/mfa/verify`, `POST /auth/refresh`, `POST /auth/logout`, `POST /auth/invite/accept` |
| Dashboard | `GET /dashboard/kpis`, `GET /dashboard/attention`, `GET /dashboard/supply` |
| Caregivers | `GET /caregivers`, `GET /caregivers/{id}`, `POST /caregivers/{id}/approve`, `/reject`, `/request-changes`, `/suspend`, `/reinstate`, `PUT /caregivers/{id}/skills`, `POST /caregivers/{id}/identity-decision`, `/background-decision` |
| Documents | `GET /documents/{id}/url` (signed URL), `POST /documents/{id}/decision` |
| Bookings | `GET /bookings`, `GET /bookings/{id}`, `GET /bookings/unmatched`, `POST /bookings/{id}/redispatch`, `/assign`, `/reassign`, `/cancel`, `/correct-status` |
| Payments | `GET /payments`, `GET /payments/exceptions`, `POST /payments/exceptions/{id}/resolve`, `GET /reconciliation/{date}` |
| Refunds | `GET /refunds`, `POST /refunds` (request), `POST /refunds/{id}/approve`, `/reject` |
| Payouts | `GET /earnings`, `GET /caregivers/{id}/ledger`, `POST /payout-batches`, `POST /payout-batches/{id}/execute`, `GET /payout-batches/{id}`, `POST /ledger/adjustments` |
| People | `GET /people`, `GET /people/{id}`, `POST /people/{id}/suspend`, `/reactivate`, `GET /people/{id}/consents`, `POST /people/{id}/consents/guardian`, `POST /clinical-access` (break-glass grant) |
| Pricing and settings | `GET/POST /price-books`, `POST /price-books/{id}/preview`, `/publish`, `GET/PUT /service-areas`, `GET/PUT /form-options`, `GET/PUT /policies` |
| Support | `GET /support/conversations`, `GET /support/conversations/{id}`, `POST /support/conversations/{id}/messages`, `/assign`, `/resolve` |
| Quality | `GET /ratings`, `GET /tickets`, `POST /tickets`, `PATCH /tickets/{id}` |
| FallGuard | `GET/POST /fallguard/questions`, `POST /fallguard/questions/{id}/versions` |
| WhatsApp | `GET /whatsapp/templates`, `GET /whatsapp/health`, `GET /whatsapp/opt-outs`, `GET /whatsapp/costs` |
| Admins and audit | `GET/POST /admins`, `PATCH /admins/{id}`, `POST /admins/{id}/reset-mfa`, `GET /audit` |
| Exports | `POST /exports` (async job), `GET /exports/{id}` |

**Endpoints being retired or moved:**
- `/booking-settings` and `/prescription/settings` write routes move under `/admin/price-books`.
- The `/patient/admin/refund/*` routes are replaced.
- The unauthenticated `/prescription/{id}` PATCH, status and upload routes are locked down.
- The `/fallguard/admin/*` routes move under `/admin/fallguard`.
- The public `/booking/ws/stats` route is removed.
- `/auth/test-cloudinary` is removed.

---

## 9. UX notes

- **Layout.** A left navigation with grouped modules: Overview · Queues (Applications, Unmatched, Refunds, Support, Exceptions) · Bookings · People · Caregivers · Money (Payments, Payouts, Reconciliation) · Content (Pricing, FallGuard, Service areas, Form options) · WhatsApp · Admin (Users, Audit). Navigation items the user's role can't reach are hidden.
- **The work is queue-driven.** Every queue shows a count badge, is sorted by urgency or SLA, and supports keyboard shortcuts for approve, reject and next.
- **Display conventions.** All times are shown in **WAT**. Money is shown as `₦44,000` and stored in kobo. Booking codes are selectable.
- **Masking.** Masked fields render as `•••` with a padlock. Revealing one opens the reason dialog (AD-FR-73).
- **Destructive actions** (cancel, suspend, refund, adjust) need confirmation and a reason, and show their side effects before confirming: "This will notify the patient and refund ₦44,000 to their card."
- **Target device:** desktop first, with a minimum width of 1280. Queues and booking detail must also work on a tablet for field ops leads.

## 10. Non-functional requirements

| ID | Requirement |
|---|---|
| AD-NFR-1 **Security** | TOTP MFA is required. Passwords are hashed with argon2 or bcrypt through pwdlib. Access tokens last 15 minutes, with a 30-minute idle timeout. Login and MFA are rate-limited, with lockout after 10 failures. The console is served with CSP, HSTS and `frame-ancestors 'none'`. |
| AD-NFR-2 **Authorization** | Deny by default. Every admin endpoint declares a permission. Automated tests check each endpoint against each role. |
| AD-NFR-3 **Audit** | 100% of writes, clinical reads, document views and exports are logged. The log is append-only at the database-grant level. It is kept for 6 years **[counsel]**. |
| AD-NFR-4 **Privacy** | Masking is enforced **in the API**, not just in the UI. The response leaves out or masks fields the caller's permissions don't cover. |
| AD-NFR-5 **Performance** | List endpoints are paginated (cursor-based) with p95 under 500 ms on 100k bookings. The dashboard loads in under 2 s. |
| AD-NFR-6 **Correctness of money** | All amounts are integer kobo. Refunds and payouts are idempotent, using idempotency keys sent to Paystack. Every balance can be recomputed from the ledger. |
| AD-NFR-7 **Availability** | 99.5% during business hours. Admin downtime must never block patient booking. |
| AD-NFR-8 **Observability** | Admin actions show up in metrics (approvals per day, refund time, payout failures). Alerts fire on webhook exception spikes and on items breaching their SLA. |
| AD-NFR-9 **Testing** | Contract tests for every admin endpoint, including permission tests. E2E tests for approval → match → complete → earning → payout, and for cancel → refund. Webhook tests use captured Paystack payloads. |
| AD-NFR-10 **Accessibility** | WCAG 2.1 AA and fully keyboard-operable queues. |

## 11. Dependencies

- **Backend security work** (master checklist §1–2). It is a precondition, because the console widens access to sensitive data.
- **Migration graph fixed**, and the **kobo migration**.
- **Redis, a job queue and a scheduler** (shared with the WhatsApp channel).
- **Private object storage** and migration of the existing documents.
- **Paystack:** Transfers enabled on the account, with the business verification and balance funding that Transfers need ⚠️ (confirm the account tier). Refund and transfer webhooks configured.
- **Background check process or provider.** Mandatory before approval (A5), so a v1.0 blocker. Until a vendor is integrated, Verification records the result manually, with uploaded evidence (AD-FR-10).
- **WhatsApp channel tables** (contacts, consents, support conversations, ratings) for the modules that depend on them.
- **Caregiver app:** it must show approval results, requested changes and suspension, report its location and availability, and receive push notifications.

## 12. Success metrics

| Metric | Target |
|---|---|
| Application decision time (median) | < 48 h |
| Paid bookings unmatched beyond 2 h with no owner | 0 |
| Refund approval → Paystack processed (median) | < 24 h |
| Completed visit → earning credited | < 1 h (automatic) |
| Payout failure rate | < 2% |
| Daily reconciliation unexplained differences | 0 |
| Admin actions with an audit entry | 100% |
| Break-glass reveals reviewed weekly | 100% |

## 13. Release plan

| Release | Contents | Gate |
|---|---|---|
| **v1.0** (before the WhatsApp closed pilot) | Admin auth, roles and audit; caregiver verification and private documents; bookings and unmatched queue; refunds v2; earnings and manual payout batches; pricing editor; service areas and form options; support inbox; ratings list; FallGuard editor; WhatsApp read-only ops; CSV exports; basic Overview | Every Must item in §7.1–7.12, 7.14, 7.15 and 7.17 has passed its E2E tests; permission test suite green; security review done |
| **v1.1** | Partial refunds, scheduled payouts, city pricing, prescription ops, canned replies, quality card, charts, scheduled reports, live map | Pilot learnings |
| **v2** | Clinicians (MDCN), teleconsult ops, pharmacy partners, cohort analytics, packages | WhatsApp Phase 3 |

## 14. Open decisions

| # | Decision | Proposed default |
|---|---|---|
| A1 | **Caregiver payout share** of the service fee | 80% of `service_fee` (the platform fee stays with Oakly) |
| A2 | **Earning hold period** (dispute window) | 24 h after completion |
| A3 | **Payout cadence and minimum** | Weekly (Friday); minimum ₦5,000 |
| A4 | **Refund approval limit** for Finance | ₦100,000; above that needs a Super Admin |
| A5 | **Background check provider and method.** ✅ *Decided 2026-09-30: a background check is mandatory before any caregiver is approved.* Still open: which provider or process (for example, a police clearance certificate plus reference checks, or a vendor). | Open |
| A6 | **Four-eyes** on pricing and adjustments | Yes, for pricing publish and adjustments |
| A7 | **Unmatched SLA** | 30 min to escalate; the patient is told at 30 min |
| A8 | **On-call channel** for urgent handovers | Phone number and email **[TBD]** |
| A9 | **Retention**: audit log, transcripts, exports | 6 yr / 90 d / 30 d. Confirm with counsel. |
| A10 | Hosting for the console | Vercel or the same VPS; behind SSO later |

## 15. Appendix: current-state reference (from the code, 2026-09-30)

| Area | Current behaviour |
|---|---|
| Admin check | `get_current_admin` = an `admins` row exists (`deps.py:88-98`). Refund routes instead check `user_type=="admin"` (`patient.py:320,362`), and no user can have that type. |
| Admin creation | `create_test_admin.py`: hardcoded credentials, `user_type="patient"`, `permissions='{"all": true}'` (never read). |
| Admin endpoints | Booking and prescription settings writes; FallGuard question CRUD, where DELETE is a hard delete; refund list and approve (unreachable). |
| Booking statuses | pending, accepted, in_progress, completed, cancelled, declined (the last is never set). Payment: pending, paid, failed, refunded (refunded is never set). |
| Caregiver actions | accept doesn't check that the caregiver was notified or that the booking is paid; cancel has no ownership check (`services/booking.py:337-394`). |
| Matching | Filters on approved, active and available location, `specialization ILIKE service_type`, within 20 km. Scores and notifies the top 8, with notifications stored in the database only. No timeout or re-dispatch. The caregiver app never sends its location. |
| Refunds | Wallet transactions only. Approval both credits the wallet and calls a Paystack refund. |
| Caregiver money | Integer kobo. No earnings are ever credited. Withdrawals accept negative amounts and never settle. Bank accounts are stored in plaintext. |
| Documents | Local disk under the public `/uploads`. The only type the app sends is `license`. |
| Encryption | Only vitals value and notes, and FallGuard answers and AI text (Fernet). Allergies, medications, conditions, booking condition, notes, chat and bank details are plaintext. |
| Notifications | Email for verification and password reset only. `booking_notifications` rows are stored but never pushed. The apps use no push. No patient notifications at all. |
| Missing models | Ratings, tickets and disputes, support conversations, audit log, roles. |
