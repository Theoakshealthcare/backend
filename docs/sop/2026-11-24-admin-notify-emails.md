# SOP: admin e-mails for caregiver onboarding events

| | |
|---|---|
| **What shipped** | Admins who review caregivers now get an e-mail when (1) a caregiver submits or resubmits their profile for review, (2) an identity check ends as `needs_review`, (3) a caregiver uploads a document while the application is open. PR: see the branch `feat/admin-notify-emails` on `Theoakshealthcare/backend`. |
| **Why** | During the owner's first end-to-end caregiver test on staging (2026-11-24) nothing reached an admin: the only mails were caregiver-facing ones and the Dojah ops alert (`KYC_ALERT_EMAIL`). A submitted profile or a name-mismatch review sat in the queue until someone opened the console. |
| **Not verified** | A real Resend delivery (tests fake the transport). Staging redeploys on merge to `develop`; with no env change it already emails every active admin who holds `caregivers.approve`. |

## Who gets what
Recipients are every `admin_users` row with `status = 'active'` whose roles carry `caregivers.approve` (today the Super Admin and Verification roles), plus `ADMIN_NOTIFY_EMAIL` when it is set (one extra address, lower-cased, never duplicated). Operations, Finance, Support, Clinical Reviewer, Read-only, `invited` and `disabled` admins get nothing.

| Event | Fires when | Silent when |
|---|---|---|
| `submitted` / `resubmitted` | `PUT /caregiver/consent` moves `profile_status` into `under_review` (from `incomplete`/none, or from `changes_requested`) | the status was already `under_review`, consent is incomplete, or the caregiver is approved, rejected or suspended |
| `identity_review` | the KYC engine decides an attempt as `needs_review` (the notice reaches `kyc_notify.deliver`, after the commit) | a pass, a "try again" failure, a replay of the same webhook, or a check an admin already decided |
| `document_added` | a document upload commits while `profile_status` is `under_review` or `changes_requested` | any other status, or the caregiver already uploaded a document in the 30 minutes before this one |

Content: the caregiver's full name, one sentence, and a link `ADMIN_CONSOLE_URL/caregivers/applications/<caregiver id>` (the console route `apps/admin/src/app/(console)/caregivers/applications/[id]`). Subject: `<PROJECT_NAME> - <heading>`, no name. Never a document or its name, ID number, photo, address, phone number, e-mail, date of birth or the reason a check failed. Template: `app/templates/emails/admin_caregiver_notice.html` (same look as `caregiver_decision.html`, every value escaped).

## How
- One module, `app/services/admin_notify.py`. Public entry points: `notify(event, ...)`, `on_profile_status_change(caregiver_id, before, after)`, `on_document_uploaded(caregiver_id, document_id)`. Each call site is one line, after the commit, and none can raise.
- **Never blocks or fails the request.** The lookups (caregiver, recipients, debounce) run in a short session of their own, so a failed query cannot poison the caller's session. The HTTP calls to Resend run on a small worker pool (`_executor`, 2 threads) that touches no database, so a slow provider never holds the caregiver's request. Any exception is logged (`admin notify failed event=...`) and swallowed; `email.send_email` already returns False on a provider error.
- **Debounce without a migration.** "Another document by this caregiver uploaded in the 30 minutes before this one" is read from `caregiver_documents.uploaded_at`. The first upload of a burst mails, uploads within 30 minutes of the previous one do not, so at most one mail per 30 minutes and the same on every worker. Of two simultaneous uploads only the earlier counts as "previous", so exactly one sends. Rejected: a new `caregivers` column (a migration and a possible Alembic head clash for a convenience), Redis (optional in this app, so not a reliable counter), process memory (gunicorn runs several workers). Trade-off: the window slides, so a caregiver who keeps uploading every 20 minutes mails once, at the start.
- The `needs_review` hook sits in `kyc_notify.deliver`, the production notifier the ARQ jobs already call after the commit for both the webhook and the reconcile paths. The engine's own guard (a decided check is never decided again) is what makes it fire once per transition.

## What's involved
| Path | Change |
|---|---|
| `app/services/admin_notify.py` | new: recipients, copy, debounce, send |
| `app/templates/emails/admin_caregiver_notice.html` | new template |
| `app/services/caregiver.py` | `update_consent` (records the status before, calls `on_profile_status_change`), `upload_document` (calls `on_document_uploaded`) |
| `app/services/kyc_notify.py` | `deliver`: one line for `UNDER_REVIEW` |
| `app/core/config.py`, `.env.example`, `deploy/env.example` | `ADMIN_NOTIFY_EMAIL` (default empty = off), `ADMIN_CONSOLE_URL` (default `https://admin.oaklycare.com`) |
| `app/tests/test_admin_notify.py` | new, 33 tests |

No migration, no new endpoint, no OpenAPI change, no auth or permission code changed (the permission name `caregivers.approve` is only read).

## Verification
Run locally (touched files only): `test_admin_notify.py` and `test_staging_env_example.py` (49 passed); the notification, consent and upload tests in `test_kyc_engine.py`, `test_caregiver_approval.py`, `test_caregiver_approval_review.py` (13 passed); `ruff check --select E9,F63,F7,F82 .`; `python -m scripts.ci_smoke` with CI's dummy env (217 operations, one head). Covered: exact recipients (active + permission, extra address, case and duplicates), no mail for other roles or inactive admins, name and link present and private data absent, each trigger once and not on a repeat, the 30-minute debounce (burst, quiet half hour, per caregiver, upload endpoint), a failing Resend transport and a crash inside the notifier leaving the request at 200 with the change committed, the send going to a worker in production mode. CI runs the rest.

## Operate / roll back
- Extra recipient: set `ADMIN_NOTIFY_EMAIL` in the encrypted env file and redeploy. Different console origin (for example staging): set `ADMIN_CONSOLE_URL`.
- To silence everything: remove the `caregivers.approve` permission from the roles (an admin decision), or revert the PR; there is no switch because the owner asked for the mails to be on.
- Needs `RESEND_KEY` and `EMAILS_FROM_EMAIL`, as every other mail.

## Follow-ups
- A real send on staging (submit a profile, trigger a sandbox name mismatch, upload a file while `changes_requested`) to confirm delivery and the link.
- Per-admin opt-out and a digest are not built. Identity checks that need an admin after attempts run out reach admins through the same `needs_review` mail.
- Other events an admin may want later (a document expiring, a caregiver stuck at identity) are not covered.
