# Clinician review, Phase 2 sign-off and FallGuard v2.0: build map

Prepared 2026-11-25 from three documents the owner added at the repository root: the clinical lead's answers to the Clinician Review Pack (7 Nov), the Phase 2 sign-off pack (co-founder, 21 Nov, marked "review ongoing") and the FallGuard v2.0 Developer Handover Package (10 Nov). Nothing here has been built yet. Exact wording is in the PDFs; this file maps each ask to code that exists today and to the work that remains.

## What the documents decide

| Source | Decision | Already built? | Work |
|---|---|---|---|
| Clinician pack s1 | Five care templates approved with annotations: add "contact the family and inform the family's doctor" for diabetes, cardiac and glucose warnings, a pain record or task for post-surgery, cardiac activity wording, Elderly template usable by a nurse OR a caregiver, a free space for the caregiver's own documentation | Templates exist (`0016_care_plans_core.py`), nurse-only creation | S: new template text (migration or seed), template visibility, documentation space |
| Clinician pack s2 | Medication notice stays; closing line may name the family or designated doctor; caregivers may add and edit medications (liaising with the Clinical/Nursing Supervisor); admin console shows the notice and medications; an AI check flags drug-drug reactions and dose inconsistencies in the background | Medications are nurse-only, notice exists in API and caregiver app, no admin screen, no AI check | M-L: permission change with a supervisor step, admin screen, AI interaction check (clinical wording and legal review needed) |
| Clinician pack s3 | Vital thresholds supplied (below); flags are wanted; weight and sleep not needed; out-of-range readings and missed tasks should alert someone | Six vitals stored, typing-mistake ranges only (`app/services/vital_rules.py`), no flags, no alerts | M: flag engine on the server, badges in both apps, alert routing (who is told: owner decision) |
| Clinician pack s4 | The 24-question score is replaced by FallGuard v2.0; AI text must be reviewed before launch | v1 live in the patient app, questions not seeded on staging | L: see FallGuard v2.0 below |
| Clinician pack s5 | "Call 112" is enough, add state lines once verified; "report at once" means contact the family and the family doctor; alert on a missed task or an unsafe reading | Emergency reply names 112 only; no alerts | S-M with the alerts work |
| Phase 2 | Reschedule: build now (free more than 12 h before, 2 free per booking, new time within 28 days or set manually, auto re-match else admin matches by hand, price dynamic at the new time, not offered under 12 h) | Not built | M-L: backend rules and admin settings, both apps |
| Phase 2 | No provider ratings (brand rating only, unclear), no provider choice, prescriptions deferred (Famasi Africa or One Health Pharmacy), video deferred | n/a | none |
| Phase 2 | Drug reminders, wallet, family details marked "Build now" | Not built | CONFLICT: see below |
| Phase 2 | Services: only Nurse confirmed; nutritionist, physiotherapist, caregiver, doctor rows blank; typed addresses need a Google Maps key | Catalogue and minutes merged on the backend (checklist section 17); patient app books nurse only | wait for the owner's answers |

## Vital thresholds supplied by the clinical lead (for the flag engine)

| Vital | Normal | Flag | Critical | Notes |
|---|---|---|---|---|
| Heart rate | 60-100 bpm | high above 100, low below 60 (escalate to the family doctor) | below 50: contact family, arrange an ambulance | patients on beta blockers: flag below 60 |
| Blood pressure | 120/80 to 140/90 | high above 140/90, low below 100/60 | systolic below 90 | diabetes or kidney disease: flag above 130/80 |
| Temperature | 36.5-37.5 C | low below 36.5, high above 37.5 | not given | |
| Respiratory rate | 12-20 per minute | high above 20, low below 12 | not given | |
| Blood glucose | not clearly given | fasting 126 mg/dL or more, random 200 mg/dL or more; reading marked Random, Fasting or After meal | not given | text partly garbled in the PDF: confirm the normal band |
| Sleep, weight | not needed at this stage | | | |

Open: the condition-based rules need the patient's conditions and medicines as data; check what the patient profile holds.

## FallGuard v2.0 (replaces the 24-question screener)

A full module: consent, eligibility (age 60 or more), parts A, B and C one item per screen, red-flag items with an offline Urgent screen and an always-visible "I need help now" button, server-side scoring (the server alone decides the tier; tiers LOW 0-5, INTERMEDIATE 6-11, HIGH 12-36, gate tier floor, provisional when more than 3 items unknown), result screen with reasons and recommendations, PDF summary, history, immutable versioned results, caregiver module (start for a client, proxy and assisted modes, clinician tests, tier override), monthly check-ins and notifications with no health text, audit log, admin console (version registry, feature flags, go-live lock needing two named approvers, kill switch), five languages with English fallback, analytics and shadow mode (P1). Out of scope: payments, wearables, model-generated tiers. The handover assumes six sprints for a team of about six; its delivery plan is sprint 1 pipeline and scoring library, 2 config-driven screens, 3 submit API and result and Urgent flow, 4 caregiver module and check-ins, 5 admin console and go-live lock, 6 hardening.
BLOCKER: the document says it ships with `fallguard_scoring.py`, `test_fallguard_scoring.py` and `questionnaire_v2_0.json`; the owner has not yet supplied them. The clinical lead's sign-off table in Appendix E is blank (gate item 1 of the specification), and the state emergency-number list is "to be supplied by operations".

## Conflicts to settle before building
1. **Wallet.** The owner refuses: Oakly is not a licensed money system, so a stored-value wallet or wallet-to-wallet or wallet-to-bank movement is a compliance risk. The co-founder marked it "Build now". Recommendation: do not build a patient wallet; keep the caregiver earnings view (Finance-approved payouts to a bank account) but name it Earnings; if a wallet is truly required, use a licensed partner and take legal advice first. Refund-to-wallet stays off.
2. **Drug reminders.** Marked "Build now" but the approved medication notice says Oakly does not remind anyone to give medicines. Needs clinical and legal wording and a new notice before any build.
3. **Family details.** Marked "Build now" without a spec: need what the family view should show and who consents.
4. **Who is alerted** for an out-of-range reading or a missed task (the family contact, the caregiver, an admin queue, or all).

## Proposed order (at most three builder sessions at a time)
1. Slice 1a: care template text, template visibility, caregiver documentation space (small).
2. Slice 1b: vitals flag engine (server) and badges (both apps) using the table above.
3. Slice 1c: alerts, after the owner answers who is told.
4. Reschedule (backend rules and admin settings, then both apps).
5. Medications: caregiver edit with supervisor step, admin screen; AI check last.
6. FallGuard v2.0: backend scoring and configuration first from the supplied reference code, then patient screens, caregiver module, admin console and go-live lock, hardening.
Mobile changes reach a phone only with the next APK (Expo plan resets 2026-12-01).
