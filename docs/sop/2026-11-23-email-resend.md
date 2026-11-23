# SOP: outgoing email moves from SMTP (Zoho) to Resend

| | |
|---|---|
| **What shipped** | The backend sends email (sign-up codes, password-reset codes, caregiver decisions, KYC notices) through the Resend HTTP API instead of SMTP. The four `SMTP_*` settings are gone; one new setting `RESEND_KEY` replaces them. `EMAILS_FROM_EMAIL` / `EMAILS_FROM_NAME` stay. |
| **Branch / PR** | `fix/verified-tag-digest`, backend PR #66 (see the PR). |
| **Not verified** | **A real email delivered to an inbox on staging** (it needs the deploy). Locally, Resend accepted two real messages (a plain test and a verification-style email) to the owner's address; the owner is to confirm they arrived. |

## Why
The first real phone test could not finish sign-up: no confirmation email arrived. Locally, Zoho (`smtp.zoho.com` and `smtp.zoho.eu`, port 465) answered `535 5.7.8 Authentication Failed` to the mailbox login on three attempts, two passwords and both regions. The owner chose to stop fighting Zoho and use Resend (the sending domain `oaklycare.com` is verified there).

## How
`app/services/email.py`: `send_email_smtp` became `send_email`, which POSTs `{from, to, subject, html}` to `https://api.resend.com/emails` with `Authorization: Bearer <RESEND_KEY>` using httpx, the same way the other provider services call out (test hook `_transport`, like `app/services/push.py`). It keeps the old contract: it returns `False` and logs a warning instead of raising, so a mail problem never turns the triggering request into a 500; the log carries Resend's status and message and never the key. Not configured (`RESEND_KEY` or `EMAILS_FROM_EMAIL` empty) returns `False` with a warning. Rejected: Resend's SMTP relay (it would have kept all four `SMTP_*` settings and port 465), and a third-party Resend SDK (one POST does not need a dependency).

## What's involved
| Path | Change |
|---|---|
| `app/core/config.py` | removed `SMTP_PORT/HOST/USER/PASSWORD`; added `RESEND_KEY` |
| `app/services/email.py`, `app/services/kyc_notify.py` | `send_email` (renamed), Resend transport |
| `app/tests/test_password_recovery.py` | the transport fake is now a fake Resend API (`httpx.MockTransport`); new tests for the unset key, a 403 answer (key not logged) and a network error |
| `app/tests/test_kyc_engine.py`, `app/tests/test_staging_env_example.py` | renamed function; the env-example test checks `RESEND_KEY` instead of the SMTP port |
| `.env.example`, `deploy/env.example`, `.env.staging.enc`, `.env.production.enc` | `SMTP_*` removed, `RESEND_KEY` added (placeholder in the examples) |
| `scripts/capture_password_reset_server.py` | records Resend requests as `.eml` files (same format the reader expects) |
| `docs/deploy/staging.md`, scripts' texts | SMTP and port-465 guidance replaced |

## Verification
Run locally: `tests` for password recovery and the KYC engine (62 passed), the env-example and settings tests (29 passed); a real send through `send_email` to the owner's address (accepted by Resend: plain test and a verification-style email); the capture script writes the file format its reader expects. **Not run:** the full suite (CI), delivery to an inbox on staging.

## Operate / roll back
Set `RESEND_KEY` in the environment's encrypted env file (`docs/deployment/ENV_ENCRYPTION.md`) and deploy. Check delivery: sign up in the app; if no code arrives, the API log line `Failed to send email ... Resend answered <status> <message>` says why (401 key, 403/422 the sender domain or address is not verified). Roll back by reverting the PR (the SMTP settings return, but the Zoho login would still be refused).

## Follow-ups
1. Confirm real delivery on staging after the deploy, including spam placement; set up SPF/DKIM checks in the Resend dashboard if mail lands in spam.
2. Use a different `RESEND_KEY` for production than for staging (restricted to sending, per environment), and rotate the key if it was ever pasted anywhere.
3. Resend rate limits and the daily free quota apply: watch them once real sign-ups start.
