# Oakly: Clinician Review Pack

**For:** Oakly's clinical lead. **Prepared:** 7 November 2026, from the app exactly as it is built today.
**How to use this pack:** read each section, then tick one box in the sign-off row under it. If you choose "Change", write the new wording or number in the blank. You can also write straight into any table. Sign at the end (page "Sign-off") and send the document back to the Oakly owner.

Nothing here has been invented. Where I could not find something in the app, the text says so. Where two parts of the app disagree, the text says that too. The last page lists the source files for the engineers; you do not need it.

**Two things to know before you start**

1. Oakly has a caregiver app and a patient app. Neither of them tells anyone a reading is "Normal" or "Critical" today. An older, retired version of the patient app did. Section 3 covers both.
2. The five care plan templates are marked "pending clinical review" in the system, but nothing in the app stops a nurse from using them before you sign off. Your sign-off is the only gate.

---

## 1. Care plan templates (5)

**What they are.** When a nurse sets up a care plan for a client, they can start from one of five ready-made templates and then edit it. Each plan has goals (what we are aiming for) and tasks (what the visiting caregiver does and ticks off). Every task below repeats every day of the week. The system has three daily time slots used here: morning (08:00), afternoon (13:00) and evening (18:00). Only nurses and Oakly's Clinical Reviewer can create or edit a plan. Other caregivers on a live visit can read it and tick tasks off.

**Why this matters to a patient.** A caregiver in the home follows this list. If a task is wrong, missing, or unsafe, the patient gets the wrong care, and the family assumes a clinician approved it.

**What I found in all five.** The templates contain no medicines, no doses and no numeric limits (no "stop if BP is above X"). Every template's "report at once" line is only text on the plan. I found nothing in the app that alerts anyone because of that line or because a task is left unticked. The tasks "Check and record BP and pulse" and "Weigh and record weight" are ticked off like any task. Blood pressure and pulse can be saved in the Vitals section, but **weight is not one of the six vitals the app can store** (see Section 3).

### 1.1 Stroke Recovery
*"Daily support after a stroke: safe movement, therapy exercises, swallowing and skin care."* Runs 30 days. Reviewed every 14 days. Starting risk level: **High**.

| Goals | How often |
|---|---|
| Safe mobility and transfers | Daily |
| Daily range-of-motion practice | Daily |
| Support speech and swallowing exercises as taught by the therapist | Daily |
| Prevent falls | Ongoing |

| Tasks | When |
|---|---|
| Check and record BP and pulse | Morning and evening |
| Assisted washing and dressing | Morning |
| Prescribed exercises with the therapist's sheet | Afternoon |
| Safe-swallow check at meals | Morning, afternoon, evening |
| Skin and pressure-point check | Evening |

**Warning text on the plan:** "Follow the therapist's exercise sheet." · "Report new weakness, slurred speech or confusion at once."

| Your decision | |
|---|---|
| ☐ Approve as is   ☐ Change to: ______________________________   ☐ Remove | |

### 1.2 Diabetes Management
*"Meals, readings, foot care and gentle activity for a client living with diabetes."* Runs 30 days. Reviewed every 14 days. Starting risk level: **Medium**.

| Goals | How often |
|---|---|
| Regular meals and fluids | Daily |
| Blood glucose checked and recorded as prescribed | Daily |
| Daily foot check | Daily |
| Gentle daily activity | Daily |

| Tasks | When |
|---|---|
| Check and record blood glucose as the prescriber ordered | Morning and evening |
| Meal support, note what was eaten | Morning, afternoon, evening |
| Foot and skin check | Evening |
| 30-minute walk if able | Afternoon |

**Warning text on the plan:** "Record readings, do not interpret them." · "Report dizziness, sweating or unusual drowsiness at once."

*Note:* there is no instruction about what to do for a very low or very high glucose reading, and no rule linking a reading to the "report at once" line.

| Your decision | |
|---|---|
| ☐ Approve as is   ☐ Change to: ______________________________   ☐ Remove | |

### 1.3 Post-Surgery Recovery
*"Wound care, comfort, gentle movement and nutrition while healing after an operation."* Runs 21 days. Reviewed every 7 days. Starting risk level: **Medium**.

| Goals | How often |
|---|---|
| Wound kept clean and dry as instructed | Daily |
| Pain reported and recorded | Daily |
| Gradual return to movement | Ongoing |
| Adequate fluids and nutrition | Daily |

| Tasks | When |
|---|---|
| Wound check and record appearance | Morning and evening |
| Assisted short walk | Morning and afternoon |
| Hygiene and clean clothing | Morning |
| Fluids and meal support | Morning, afternoon, evening |

**Warning text on the plan:** "Wound care exactly as the surgical team instructed." · "Report fever, spreading redness or discharge at once."

*Note:* the goal "Pain reported and recorded" has no matching task. Pain is not one of the six vitals either.

| Your decision | |
|---|---|
| ☐ Approve as is   ☐ Change to: ______________________________   ☐ Remove | |

### 1.4 Cardiac Rehabilitation
*"Light activity, resting readings and low-salt meals while recovering from a heart event."* Runs 45 days. Reviewed every 14 days. Starting risk level: **High**.

| Goals | How often |
|---|---|
| Progressive light activity as cleared by the care team | Daily |
| Resting BP and pulse recorded | Daily |
| Low-salt, regular meals | Daily |
| Report chest symptoms early | Ongoing |

| Tasks | When |
|---|---|
| Check and record BP and pulse | Morning and evening |
| Light activity as instructed | Afternoon |
| Weigh and record weight | Morning |
| Meal support | Morning, afternoon, evening |

**Warning text on the plan:** "Stop activity and call for help for chest pain, breathlessness or fainting." · "Follow the cardiac team's activity limits."

*Note:* the plan does not say who to call or which number. See Section 5 on emergency handling.

| Your decision | |
|---|---|
| ☐ Approve as is   ☐ Change to: ______________________________   ☐ Remove | |

### 1.5 Elderly Daily Living and Dementia Support
*"A calm daily routine with meals, fluids, safety and engagement for an older client or a client with dementia."* Runs 30 days. Reviewed every 14 days. Starting risk level: **Medium**.

| Goals | How often |
|---|---|
| Calm, predictable daily routine | Daily |
| Safe home and fall prevention | Ongoing |
| Regular meals, fluids and toileting | Daily |
| Keep the client engaged | Daily |

| Tasks | When |
|---|---|
| Morning routine: wash, dress, breakfast | Morning |
| Hydration prompts | Morning, afternoon, evening |
| Gentle activity or conversation | Afternoon |
| Evening routine and a calm bedtime | Evening |
| Note mood and confusion | Evening |

**Warning text on the plan:** "Do not leave the client alone if wandering is a risk." · "Note any new confusion or change in behaviour."

*Note:* there is no night-time task, although the system supports a 21:00 "night" slot.

| Your decision | |
|---|---|
| ☐ Approve as is   ☐ Change to: ______________________________   ☐ Remove | |

---

## 2. The medication notice

**Exact wording** (identical in the system and in the caregiver app):

> "This is a record of what the prescriber ordered. Oakly does not check doses or interactions and does not remind anyone to give medicines. Follow the prescriber's instructions."

**Where users see it.** In the caregiver app, in the "Medication schedules" part of a client's care plan. It is always shown, even when no medicine has been recorded yet, to every caregiver who can open that plan. Nurses can add, edit and stop medicines. Other caregivers can only read them. I found no screen in the patient app or the admin console that shows it; in the admin tools the same wording appears only in the technical documentation.

**What a nurse can record per medicine:** name, strength, dose, how it is given, why, who prescribed it, how often (once, twice, three or four times a day, weekly, as needed, other), times of day, start and end dates, and a note. Strength and dose are free text; nothing checks them. A stopped medicine stays on the plan as history and cannot be edited.

**Why this matters to a patient.** Families may read a list of medicines in a care app as an instruction. This notice is the only thing telling them the list is not checked and is not a reminder.

*Question for you:* is "Follow the prescriber's instructions" the right closing line? Should it also say who to call with a medicine question?

| Your decision | |
|---|---|
| ☐ Approve as is   ☐ Change to: ______________________________   ☐ Remove | |

---

## 3. Vital signs

**What the app records.** Six vital signs, typed in by the patient or a caregiver (a caregiver can only add readings during a live visit). The app saves them. It does not decide whether a reading is good or bad.

| Vital sign | Unit(s) | Values the app accepts | Does the current app flag it as Warning or Critical? |
|---|---|---|---|
| Heart rate | beats per minute (bpm) | 20 to 250 | No |
| Blood pressure | mmHg, written like 120/80 | Top number 50 to 260; bottom number 30 to 160; top must be higher than bottom | No |
| Temperature | °C or °F | 30 to 45 °C, or 86 to 113 °F | No |
| Respiratory rate | breaths per minute | 4 to 60 | No |
| Blood glucose | mg/dL or mmol/L | 20 to 600 mg/dL, or 1.1 to 33.3 mmol/L. The person can mark it Random, Fasting or After meal | No |
| Sleep duration | hours | More than 0 and at most 24 hours (the patient app asks for bedtime and wake time and accepts 0.5 to 18 hours) | No |

**These ranges are typing-mistake limits, not clinical limits.** They only stop things like "300/20". A reading of 240 bpm or 50 mmHg is accepted and saved with no warning to anyone.

**Critical or warning thresholds.** The current patient app, the caregiver app and the server have **none**. The caregiver app's own code says this is deliberate "because the thresholds need clinical sign-off first".

**The retired patient app did have them.** An earlier, separate patient app (last changed September 2025, being retired) showed a red "Critical" label with these rules. It is listed so you can see what the owner may have seen, not because it is live:

| Vital sign | Showed "Critical" when | Its own "normal range" text said |
|---|---|---|
| Heart rate | below 60 or above 100 bpm | 60 to 100 bpm |
| Blood pressure | top number above 130, or bottom number above 80 (no low limit at all) | Top 90 to 120, bottom 60 to 80 |
| Temperature | below 36.0 or above 37.5 °C (below 96.8 or above 99.5 °F) | 36.1 to 37.2 °C |
| Respiratory rate | below 12 or above 20 | 12 to 20 |
| Blood glucose | fasting above 100, random above 140, after meal above 180 mg/dL (5.6, 7.8 and 10.0 mmol/L) | Fasting 70 to 100, after meals under 140 mg/dL |
| Sleep | no rule | 7 to 9 hours |

**Inconsistencies in that retired version:** it called a reading of 131/79 "Critical" but 70/40 "Normal"; its blood glucose flag (180 after a meal) did not match its own normal-range text (under 140); and its temperature and blood pressure flags did not match their own normal-range text. It had no "Warning" level, so a slightly high reading looked the same as a dangerous one.

**Why this matters to a patient.** If the app ever shows a colour or word beside a reading, families will act on it. A wrong "Normal" can delay care; a wrong "Critical" causes panic and unnecessary emergency calls.

**Please give us (or confirm "no flags in version 1"):**

| Vital sign | Warning if below / above | Critical if below / above | Who should be told |
|---|---|---|---|
| Heart rate | | | |
| Blood pressure | | | |
| Temperature | | | |
| Respiratory rate | | | |
| Blood glucose (fasting / random / after meal) | | | |
| Sleep duration | | | |

| Your decision on the accepted ranges (table above) | |
|---|---|
| ☐ Approve as is   ☐ Change to: ______________________________   ☐ Remove | |
| Your decision on flags | ☐ No Warning/Critical flags in version 1   ☐ Use the thresholds I wrote above |

---

## 4. FallGuard fall-risk questionnaire

**What it is.** A 24-question check a patient (or family) answers in the patient app. Each answer is worth points. The server adds the points up (the most anyone can score is **40**). The app then shows the total "out of 40", plus a short summary and tips written by an AI system. A clinician has not reviewed those AI words.

**Total-score bands.** The current patient app shows **no bands**: no Low, Moderate or High label, and nothing that tells the family what a score means beyond the AI's text. An older, retired patient app had four bands. They are shown so you can see them, but they do not fit a 40-point score:

| Total score | Label in the retired app | What it told the family |
|---|---|---|
| 0 to 3 | Low | "Minimal fall risk - continue current safety practices" |
| 4 to 7 | Moderate | "Some risk factors present - implement basic safety measures" |
| 8 to 12 | High | "Multiple risk factors - comprehensive intervention needed" |
| 13 and above | Very High | "Significant fall risk - immediate professional consultation required" |

Because the real maximum is 40, those bands would call everything from 13 to 40 "Very High".

**Please set bands for a 0 to 40 score and say what each tells the family:**

| Score from | to | Label | What the family is told |
|---|---|---|---|
| | | | |
| | | | |
| | | | |
| | | | |

### ⚠ Needs a clinician's confirmation: three questions look reversed

| Q | What looks wrong |
|---|---|
| **21** (medicines that make you drowsy or dizzy) | "Daily" scores **0**, while "Occasionally" and "Rarely or never" both score **1**. Daily drowsiness is the highest risk, so this looks upside down. |
| **23** (balanced meals at least twice a day) | "Yes" scores **1** and "No" scores **0**. Eating well is adding to the fall-risk score. |
| **24** (alcohol) | "Never" scores **1**, and every drinking option scores **0**, including "Daily or almost daily". |

These values are exactly as seeded. **They have not been changed.** Please confirm the correct values; the engineers will change them only on your answer.

**Other places where a middle and the worst answer score the same** (not necessarily wrong, please confirm): Q10, Q16, Q17, Q20, Q22 (marked ◆ below).

### All 24 questions, answer choices and points

| # | Question | Answer choices and points | Your mark (✓ / change) |
|---|---|---|---|
| 1 | What is your current age? | Less than 65 = 0 · 65–74 = 1 · 75–84 = 2 · 85+ = 3 | |
| 2 | Do you live alone or with others? | With family = 0 · With a caregiver = 1 · Alone = 2 | |
| 3 | Have you fallen in the last 12 months? | No = 0 · Yes, 1–2 times = 1 · Yes, 3+ times = 2 | |
| 4 | Do you use mobility aids regularly? | No = 0 · Occasionally = 1 · Always = 2 | |
| 5 | Do you feel unsteady when walking or standing? | Never = 0 · Occasionally = 1 · Frequently = 2 | |
| 6 | Can you stand up from a chair without needing to push yourself up with your hands? | Yes, easily = 0 · With difficulty = 1 · Not at all = 2 | |
| 7 | Do you experience dizziness or lightheadedness? | Never = 0 · Occasionally = 1 · Frequently = 2 | |
| 8 | Do you have pain or stiffness in your knees, hips, or back? | No = 0 · Mild = 1 · Severe = 2 | |
| 9 | Do you have foot pain, swelling, or wear ill-fitting shoes? | No = 0 · Mild discomfort = 1 · Severe pain = 2 | |
| 10 ◆ | Do you have chronic conditions (e.g., Diabetes, Arthritis, Hypertension, etc.)? | None = 0 · One condition = 1 · Two or more conditions = 2 · Three or more conditions = 2 | |
| 11 | Have you experienced confusion, forgetfulness, or trouble concentrating in the last 6 months? | Not at all = 0 · Occasionally = 1 · Frequently = 2 | |
| 12 | Do you experience confusion or difficulty recognizing your surroundings at night? | Not at all = 0 · Occasionally = 1 · Frequently = 2 | |
| 13 | Do you have trouble seeing clearly, even with glasses? | No difficulty = 0 · Some difficulty = 1 · Significant difficulty = 2 | |
| 14 | Do you struggle to hear conversations, even in quiet settings (e.g., at home, religious gatherings, or family meetings)? | No = 0 · Mild difficulty = 1 · Severe difficulty or use of a hearing aid = 2 | |
| 15 | Do you have loose rugs or uneven floors in your home? | No = 0 · Yes = 1 | |
| 16 ◆ | Is your home well-lit, especially hallways and staircases? | Well-lit = 0 · Partially lit = 1 · Poorly lit = 1 | |
| 17 ◆ | Do you frequently experience total darkness due to power outages and lack of backup power? | Never = 0 · Occasionally = 1 · Frequently = 1 | |
| 18 | Do you have grab bars in your bathroom and railings on stairs? | Fully accessible = 0 · Partially accessible = 1 · No aids present = 2 | |
| 19 | Is it safe for you to walk outside your home without risk of falling due to poor road conditions, open drains, or uneven pathways? | Yes = 0 · No = 1 | |
| 20 ◆ | How often do you engage in physical activity like walking or stretching? | Daily = 0 · Occasionally = 1 · Rarely or never = 1 | |
| 21 ⚠ | Do you take medications that make you feel drowsy or dizzy? | Daily = 0 · Occasionally = 1 · Rarely or never = 1 | |
| 22 ◆ | Do you have someone who regularly checks on your well-being? | Yes, daily = 0 · Occasionally = 1 · Rarely or never = 1 | |
| 23 ⚠ | Do you eat meals that include a mix of protein, vegetables, and carbohydrates at least twice daily? | Yes = 1 · No = 0 | |
| 24 ⚠ | How often do you drink alcohol? | Never = 1 · Occasionally (1–2 times per month) = 0 · Regularly (1–2 times per week) = 0 · Daily or almost daily = 0 | |

**Why this matters to a patient.** The score and the AI tips are what a family reads to decide whether to book a home visit or change the home. A reversed question makes a safer person look riskier and a riskier person look safer. The AI is told only the answers and the total; it is not given any bands, and its words are not reviewed.

| Your decision | |
|---|---|
| ☐ Approve the questions and points as is   ☐ Change to (use the "Your mark" column, and write Q21, Q23, Q24 corrections here): ______________________________   ☐ Remove the questionnaire | |
| Q21, Q23, Q24 | ☐ Confirmed reversed; correct values: ____________   ☐ Correct as written |
| Bands | ☐ Use the bands I wrote above   ☐ Show no bands in version 1 |

---

## 5. What the app does, and does not do, clinically

**Why this matters to a patient.** Families tend to assume a health app is watching over them. This section is the plain list of what it is not doing, so that you can decide what must be said to users, and what must be built before launch.

| The app does | The app does NOT |
|---|---|
| Keep a care plan with goals and tasks for each client, and let caregivers tick tasks off. | Remind anyone about a task, or alert anyone if a task is missed (I found no code for either). |
| Record the medicines a prescriber ordered, with the notice in Section 2. | Check doses or interactions, remind anyone to give a medicine, or record that a dose was given. |
| Save six kinds of vital sign. | Judge any reading, flag it, or tell any person when a reading is dangerous. |
| Add up the FallGuard answers on the server. | Give a risk band (see Section 4). |
| Show an AI-written summary and tips after FallGuard. | Have a clinician review those words. If the AI fails, the patient sees only "We could not prepare personal advice just now. Your score has been saved with your account." |
| Let the family chat with Oakly on WhatsApp for bookings, with an emergency screen (below). | Give medical advice over WhatsApp. It refuses and points to booking a visit. |

**WhatsApp emergency handling, as built.**

- Every WhatsApp text is checked against a list of emergency phrases, in English and Nigerian Pidgin: for example chest pain, trouble breathing, unconscious, seizure or fit, heavy bleeding, stroke signs, heart attack, overdose, poisoning, choking, snakebite, thoughts of suicide, and Pidgin forms such as "e no dey breathe" and "blood dey commot". It is deliberately set to over-trigger: "no chest pain" still triggers it. The check also catches a few spelling mistakes (for example "unconcious").
- It runs in every state, even for a person who has opted out or been paused.
- **The reply, exactly** (the same reply in English is sent whatever language the person wrote in; Pidgin replies are not built yet): *"This may be an emergency. Please call 112 now, or go to the nearest hospital. Don't wait for a reply here. We have alerted our team and a person will contact you as soon as possible."*
- **It names only one number: 112.** The settings allow more numbers, but the note beside that setting says state-specific lines should be added "only after they are verified", and none have been. There is no ambulance service, no hospital, and no state line named.
- It opens an **urgent** support ticket and pauses the automatic replies. If the emergency reply cannot be delivered, the ticket is flagged so that a person phones the client. I found no code that phones, texts or pages a member of Oakly staff automatically; staff would see the ticket in the support inbox.
- The same applies inside the booking form: if emergency words appear, an urgent ticket opens and a one-line "call 112" notice is shown.
- **The medicine-advice refusal, exactly:** *"I can't give medical advice, diagnose, or suggest medicines. To see a doctor or nurse, choose Book a visit from the menu. If this is an emergency, call 112 now."*

**Outside WhatsApp.** I found no emergency or SOS button in the patient app or the caregiver app. The caregiver app lets a caregiver tap to call the client's saved emergency contact. The "report at once" lines on care plans (Section 1) are text only.

**Please tell us:**

| Question | Your answer |
|---|---|
| Is "call 112" enough, or should Lagos, Abuja or other state lines be added once verified? | |
| What should a caregiver do, step by step, when a care plan says "report at once"? Who do they call? | |
| Should a missed task, or a reading outside a safe range, alert someone in version 1? | |
| Does the AI-written FallGuard text need to be removed, or reviewed, before launch? | |
| Is there anything in the "does NOT" column above that users must be told before launch? | |

| Your decision on how the app handles emergencies and clinical limits, as described in this section | |
|---|---|
| ☐ Approve as is   ☐ Change to: ______________________________   ☐ Remove | |

---

## Sign-off

By signing, I confirm that I have reviewed the decisions marked above.

| | |
|---|---|
| **Name** | |
| **Role** | |
| **Date** | |
| **Signature** | |

Please send this document back to the Oakly owner with your marks. Anything left blank is treated as not yet reviewed.

---

## Appendix (for the engineers; the clinician can ignore this)

Facts were read from the code on 7 November 2026: backend `oakly/main` at `f3b0a7c` (worktree `wt-clinician-pack`); the app code from the local monorepo `caregiver/` checkout at `0964956` (that checkout is 56 commits ahead of its `origin`, so app-side facts may differ on another branch); the retired patient app from the separate `patient/` checkout at `4f6cb2b`.

| Item | Source |
|---|---|
| Care plan templates, goals, tasks, warning text, days, durations, risk levels, slot times (morning 08:00, afternoon 13:00, evening 18:00, night 21:00) | `app/alembic/versions/0016_care_plans_core.py` (lines 37 to 95) |
| Templates marked pending, not enforced anywhere | `clinical_review_status` in `app/db/models/care_plan.py`; reviewed through `app/services/care_plan_admin.py:195`; no check blocks use of a pending template |
| Nurse-only template list | `app/api/v1/endpoints/caregiver_care_plans.py:118` |
| Medication notice constant | `app/schemas/care_plan.py:24` (also copied by hand in the caregiver app at `apps/caregiver/src/careplan/carePlanModel.ts:541`, so a wording change needs both) |
| Where the notice is shown | `apps/caregiver/src/components/careplan/MedicationsSection.tsx` (always shown); API docs only: `caregiver_care_plans.py:439`, `admin_care_plans.py:292` |
| Medication fields and behaviour | `docs/sop/2026-10-28-care-plans-medications.md` |
| Vital ranges the server enforces | `app/services/vital_rules.py`, `app/schemas/patient_vital.py` |
| Patient app entry limits | `apps/patient/src/domain/vitals.ts` (`ENTRY_FIELDS`, `SLEEP_MIN_HOURS`, `SLEEP_MAX_HOURS`) |
| "No judging of readings" (deliberate) | `apps/caregiver/src/vitals/vitalsModel.ts` header comment |
| Retired patient app flags | `patient/src/screens/vital/Vitals.tsx` (lines 700 to 835), `patient/src/utils/vital-utils.ts` (normal-range text) |
| Unused AI vitals insights endpoint | `app/api/v1/endpoints/vitals.py:129`; no screen in either current app calls it |
| FallGuard questions, options, points (Q1 to Q24) | `app/scripts/seed_fallguard_questions.py`. Run by hand, not by a migration, and editable by admins with `fallguard.write`, so the live database can differ from this file |
| Server-side scoring (sum of stored option points; client score ignored) | `app/services/fallguard.py` (`score_answers`, `submit_assessment`) |
| FallGuard result screen (total "out of" max, AI summary, tips; no bands) | `apps/patient/src/screens/fall/FallResult.tsx`, `apps/patient/src/domain/fallguard.ts` (its comment that the backend "does not look them up" is out of date) |
| FallGuard AI prompt | `app/services/gemini.py:172` |
| Retired app bands (0-3, 4-7, 8-12, 13+) | `patient/src/services/fallguard.service.ts` (`getRiskLevel`), `patient/FALLGUARD_API_INTEGRATION_GUIDE.md` |
| Emergency phrase list | `app/channels/whatsapp/lexicon.py` |
| Emergency and refusal reply text | `app/channels/whatsapp/copy.py:56` and `:61`, `:80` (booking form) |
| Emergency flow, ticket, failed-reply flag | `app/channels/whatsapp/engine.py:321`, `app/channels/whatsapp/processor.py` (lines 95 to 141, 176, 211) |
| Emergency numbers setting (default `112`) | `app/core/config.py:205-208` |
| Emergency contact tap-to-call | `apps/caregiver/src/screens/booking/ClientProfile.tsx:226` |

Inconsistency for the checklist: `docs/checklist/MASTER_CHECKLIST.md` mentions "Critical/Normal chips, insights" pending for the caregiver Vitals tab; the caregiver code contains neither by design.
