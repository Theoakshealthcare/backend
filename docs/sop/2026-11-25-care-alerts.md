# SOP: care alerts, out-of-range readings and missed tasks reach the family and the admin queue (slice 1c)

| | |
|---|---|
| **What shipped** | An alert record (`care_alerts`, with a dedupe ledger `care_alert_occurrences`) raised when a saved vital reading is High, Low or Critical, or when a care-plan task that was due in a completed visit was never ticked. It is delivered to the **family contact** (e-mail and push, only a consenting, verified contact) and to the **admin queue** (a console page; an e-mail to the on-call admins for Critical). Migration `0035_care_alerts` (down_revision `0029_location_nudged_at`; the lead re-points the chain at merge). Backend branch `feature/care-alerts`, **stacked on #84 (`feature/vitals-flags`): merge that first**; console PR on `feature/care-alerts-console`, stacked on the monorepo PR #75. FE guide: [fe-integration-guide-care-alerts.md](../fe-integration-guide-care-alerts.md). |
| **Why** | The clinical lead's pack (sections 3 and 5) answered "Yes" to alerting someone when a reading is outside a safe range and when a task is missed; the app judged no reading and alerted nobody. The owner decided (2026-11-25) that the alert goes to the family contact and the admin queue; the family view stays out of the v1 apps, so the family only receives a message. |
| **Verification** | `test_care_alerts_vitals.py` (34), `test_care_alerts_missed_tasks.py` (34, the rule and the sweep), `test_admin_care_alerts.py` (11), `test_care_alerts_migration.py` (2), `test_admin_rbac.py` (matrix extended, 4 routes), `test_worker.py` (cron registered), all green locally on a scratch Postgres. Live captures: `test_capture_care_alerts.py` into `docs/fe-captures/care-alerts/`. |

## The rules (each is a constant or one function)

| Rule | Decision | Where |
|---|---|---|
| **Which readings raise an alert** | After a reading is saved (create, or an edit of its type, value or unit) the vitals flags engine rates it (`vital_flags.reading_flag`, never re-implemented). High and Low are `warning`, Critical is `critical`. Normal, weight, sleep, unknown types and unreadable values raise nothing. A reading recorded more than 6 h ago (a back-filled entry) raises nothing (`CARE_ALERT_VITAL_MAX_AGE_MINUTES`). | `care_alerts.alert_for_reading` |
| **What "missed" means** | A task is missed when it was **due in a completed visit** and has **no tick for that day** when the visit ended. Due = `care_plan_tasks.scheduled_on` (active, inside the plan's dates, the task's weekdays) on each local day (Lagos) the visit's scheduled window touches, **and** the task's own `time_slot` overlaps that window by at least 30 minutes; a task added after the visit's scheduled start is never due in it; a tick by any visit that day counts. Only `completed` visits that actually started are judged (a tick is impossible once a visit is completed, so the ticks are final); no-shows, cancelled, declined and never-started visits are not. Only the subject's `active` plan counts (a visit for oneself uses the patient's plan, a visit for a relative the care recipient's plan). | `care_alert_missed_tasks.py` |
| **When it is judged** | A cron (`care_alert_sweep_job`, every 5 min) looks at visits that ended in the last 6 h (`CARE_ALERT_SWEEP_LOOKBACK_HOURS`) and were not judged yet. One alert per visit (`source_key = visit:<booking>`), so re-running never doubles it. Worst-case delay: 5 minutes after the caregiver completes the visit. | `worker.py`, `sweep` |
| **Debounce** | Per patient + type + severity (+ care recipient). While an alert with that key is `open` or `acknowledged` and was last fed inside the window, a new occurrence only bumps `occurrence_count` and `last_occurred_at` and **sends nothing**. Windows: warning reading **240 min**, critical reading **30 min** (an emergency that persists is repeated), missed task **720 min**. Severity is in the key, so a Critical is never swallowed by an earlier Warning; a `resolved` alert absorbs nothing. Every occurrence has a unique `source_key` (`vital:<reading>:<severity>`), so the same reading is never counted twice (a retry, or editing a note). | `care_alerts.raise_alert`, `CARE_ALERT_*` settings |
| **Who the family contact is** | A `care_relationships` row of role `family` or `guardian` with `clinical_access` on the alert's care recipient (the visit's recipient, or the recipient row of role `self` that stands for the patient), **and** the latest `share_with_family` consent for that recipient is `granted`, **and** the account is active. This is exactly the rule `care_plan_access.resolve_family_access` already used. E-mail only when the account is verified and has an address; push only when a device token is registered (the app identity). A WhatsApp-only contact has neither, so nothing is sent (WhatsApp is parked). `patient_family_members` (names typed into a profile) are **not** used: they carry no verification and no consent. | `care_alert_delivery.family_contacts` |
| **What the message says** | The first name and one plain sentence; "Call 112 now" when critical; never a reading, a surname, a task name, a note, a voice note, a medicine or a condition. | `care_alert_delivery.family_sentence` |
| **Admin queue** | Every alert is in the queue. A **Critical** alert also e-mails every active admin holding `support.inbox` and `ADMIN_NOTIFY_EMAIL`, through the existing `admin_notify` machinery; the mail names nobody and no reading. | `admin_notify.critical_care_alert_mails` |
| **Permission** | `support.inbox` for the list, detail, acknowledge and resolve (the urgent-ticket inbox these alerts belong with; Operations, Support, Clinical Reviewer and Super Admin hold it). **No permission was added.** The clinical `detail` and the note need `clinical.read`; the full name and phone need `people.pii` (masked otherwise), as in the bookings console. | `admin_care_alerts.py` |
| **Audit** | `care_alert.acknowledge` and `care_alert.resolve` in `admin_audit_log` in the same transaction (statuses, severity, type, `note_present`: never the note); opening a detail whose clinical content is visible writes `care_alert.read`. | `AuditService` |
| **Never into the caller's request** | `on_vital_recorded` runs after the reading is committed on its own session and swallows every error (logged with ids only); the mail and push go to a worker thread. A mail or push outage leaves the alert in the queue with `family_notified_count` 0. | `care_alerts.on_vital_recorded` |

## How, and what was rejected

| Decision | Why / alternative rejected |
|---|---|
| Two tables: the alert and an occurrence ledger with a unique `source_key`. | Idempotency and the debounce count come for free and a retry is harmless. Rejected: a counter only (a re-run of the sweep would double count); a per-alert `last_reading_id` (no way to tell an edit from a repeat). |
| The missed-task judgement runs in a cron, not in the "complete visit" endpoint. | The completion path is the caregiver's money path; this keeps the change out of it. The cost is up to 5 minutes of delay. Rejected: a hook after completion (also needs an overrun case anyway). |
| Slot overlap, not "any untick that day". | A morning task must not alarm the family after an evening visit. The task only stores the word "morning", so the hour bands are one constant (`SLOT_BANDS`: morning 05-12, afternoon 12-17, evening 17-21, night 21-05, Lagos). |
| Detail (reason, reading, task titles) is Fernet-encrypted JSON; the plaintext columns carry ids, type, severity and a generic one-line `summary`. | The vitals and plan columns are encrypted at rest; the queue must not become a plaintext copy. |
| The family is selected through `CareRelationship` + `share_with_family`, not `patient_family_members`. | The latter has no verification or consent, which the owner's rule requires. The consequence is below in the questions. |

## What's involved

- Models and migration: `app/db/models/care_alert.py`, `app/alembic/versions/0035_care_alerts.py`.
- Services: `app/services/care_alerts.py` (raise, debounce, hook, acknowledge, resolve), `care_alert_missed_tasks.py`, `care_alert_delivery.py`; `admin_notify.py` (`critical_care_alert_mails`, `send_all`, permission-parametrised `_recipients`).
- API: `app/api/v1/endpoints/admin_care_alerts.py`, `app/schemas/admin_care_alert.py`, router registered in `api/v1/api.py`; hook calls in `endpoints/vitals.py` (create and edit).
- Worker: `care_alert_sweep_job` (cron, every 5 minutes at second 10) in `app/worker.py`.
- Templates: `templates/emails/care_alert.html`, `admin_care_alert.html`.
- Settings (`app/core/config.py`): `CARE_ALERT_VITAL_DEBOUNCE_MINUTES` 240, `CARE_ALERT_CRITICAL_DEBOUNCE_MINUTES` 30, `CARE_ALERT_TASK_DEBOUNCE_MINUTES` 720, `CARE_ALERT_VITAL_MAX_AGE_MINUTES` 360, `CARE_ALERT_SWEEP_LOOKBACK_HOURS` 6, `CARE_ALERT_MIN_SLOT_OVERLAP_MINUTES` 30.
- Tests: `test_care_alerts_vitals.py`, `test_care_alerts_missed_tasks.py`, `test_admin_care_alerts.py`, `test_care_alerts_migration.py`, helpers in `care_alert_helpers.py`; `test_capture_care_alerts.py` (capture only, `CAPTURE_CARE_ALERTS=1`).

## Operate / roll back

Deploy the migration, then the worker with the new cron. Nothing to configure: mail uses the existing Resend settings, push the existing Expo settings, and `ADMIN_NOTIFY_EMAIL` (optional) also receives the critical e-mail. To tune a window, change the setting; to change the slot hours, edit `SLOT_BANDS` and the matching cases in `test_care_alerts_missed_tasks.py`. Roll back by reverting the PR and running `alembic downgrade 0029_location_nudged_at` (drops both tables: every alert is lost). The vitals API and the caregiver's request behave as before either way.

## Questions for the owner and the clinical lead

1. **App-profile family members get no alerts.** The only family contacts the system can verify and has consent for are WhatsApp-channel `care_relationships` with a granted `share_with_family`. The app's own "family members" list (`patient_family_members`) is unverified and consent-less, so it is not used. Is that the intent for v1, or should the app collect consent and verify an e-mail for those contacts (a small slice)? Until then an app-only patient's alerts reach the admin queue only.
2. **Who watches the queue and the critical e-mail?** Everyone holding `support.inbox` (Operations, Support, Clinical Reviewer, Super Admin). Say if only one role should.
3. **The slot hours** (morning 05-12, afternoon 12-17, evening 17-21, night 21-05) are ours; the clinical lead has not given them.
4. **Missed means "not ticked by the end of the visit".** Is a task done late (ticked by the next visit) still an alert? Today yes: the alert is raised at the end of the visit.
5. "Report at once" lines on care plans stay text only; this slice does not call anyone.

## Follow-ups

- A visit left `in_progress` long past its end is judged only when it is finally completed (no overrun rule yet).
- The sweep takes at most 200 visits per run, oldest first; at launch volume that is ample.
- A family screen listing alerts (the data and the consent rule are in place) and WhatsApp delivery are parked by the owner.
- Real-inbox and real-phone delivery, and the cron firing on a real worker, are not exercised.
