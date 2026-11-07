# SOP: Forgot-password email no longer 500s (`POST /api/v1/auth/password-recovery`)

| | |
|---|---|
| **What shipped** | `POST /auth/password-recovery` returns `200 {"message": "Password recovery email sent"}` instead of a 500, sends the reset email through the real SMTP sender, answers identically for unknown emails (no account enumeration), and never turns a mail problem into a 500. A regression suite covers the whole forgot -> email -> reset -> sign-in flow. Fixes device-pass-1 blocker **DP1-01**. |
| **Superseded in part** | The emailed token / `oaklycare://` link described below was replaced by a 5-digit code on 2026-11-07 ([SOP](2026-11-07-password-reset-code.md)); the SMTP, enumeration and 500 fixes here stand. |
| **Branch / commit** | `fix/forgot-password-email`, cut from `oakly/main` `83e81cc`. One commit; not pushed, the lead opens the PR. |
| **Migration / API shape** | None. The only visible change: an unknown email used to get `404 {"detail": "User not found"}`; it now gets the same `200` as a known one. Neither FE guide documents this route, so `render_fe_guide --check` is unaffected (both pass). |

## Why
`send_password_reset_email` (`app/services/email.py`) called `send_email_smtp(email_to=, subject_template=, html_template=)`, but the function takes `(email_to, subject, html_content)`. Python raised `TypeError: send_email_smtp() got an unexpected keyword argument 'subject_template'` on every call, so the endpoint was a 500 in every environment, whatever the SMTP settings. It went unnoticed because the test fixture `sent_emails` replaces `send_password_reset_email` as a whole, so no test ever ran the real call.

The old handler also answered `404 User not found` for an unknown email, which lets anyone probe which emails are registered.

## How
| Area | Decision |
|---|---|
| The call | Pass the rendered HTML to `send_email_smtp(email_to, subject, html_content)`, the same positional call the verification and caregiver-decision senders already use. Those two and the KYC notifier were checked: no other caller had wrong arguments. |
| SMTP unset | `send_email_smtp` now returns `False` with a WARNING (`SMTP is not configured; email '<subject>' was not sent`) when `SMTP_HOST` / `SMTP_PORT` are empty, instead of dialling an empty host. The recipient address is not logged. |
| Send failures | The existing catch-all stays, but logs through `logging` (WARNING) instead of `print`. A wrong SMTP login, a refused connection and so on still return `False`; the endpoint still answers 200. |
| Enumeration | The handler only looks the user up to decide whether to create a token and send the mail; the reply is the same either way. Rejected: sending a "no account" email to unknown addresses (more mail, and still a signal if a mailbox is watched). |
| Tests | Mock only the transport (`smtplib.SMTP_SSL`), so the real `send_password_reset_email` and `send_email_smtp` run and a signature mismatch fails. The reset token is read back out of the captured MIME message, as a user would. |

## What is involved
| Path | Change |
|---|---|
| `app/services/email.py` | Fixed call, `logger`, SMTP-unset guard, `print` -> `logger.warning` |
| `app/api/v1/endpoints/auth.py` (`recover_password`) | Same 200 reply for known and unknown emails |
| `app/tests/test_password_recovery.py` (new) | 9 tests |
| `docs/checklist/MASTER_CHECKLIST.md` | Line "Password reset: fix the email arguments that cause a 500" checked; follow-ups added |

## Verification
- **Red first:** run against the unfixed code, 8 of 9 failed with the real `TypeError: ... 'subject_template'` (500 from the endpoint).
- **Tests (`test_password_recovery.py`, 9 pass):** the email reaches the transport addressed to the user with a reset link in its body; the full flow (recover -> token from the email -> `/reset-password` -> the new password signs in, the old one is refused); a reset link works once; asking again invalidates the earlier link; a garbage token is `400 Invalid token`; unknown email gets the identical 200 and nothing is sent; SMTP unset gives 200, a log line and no connection attempt; a failing mail server (SMTP auth error) gives 200 and a log line; verification, reset and caregiver-decision senders all go through the transport with the right signature.
- **Neighbours re-run:** `test_kyc_engine.py`, `test_change_password.py`, `test_access_auth.py` with the new file: 101 passed.
- **Fast checks:** `poetry check --lock`, `ruff check --select E9,F63,F7,F82 .`, `python -m scripts.ci_smoke` (199 operations), both `render_fe_guide --check`. The full suite is left to CI.
- **Not verified:** a real SMTP server (the transport is faked), the email arriving in a real inbox, the deep link `oaklycare://reset-password?token=...` opening the app, the caregiver app's Recover/Reset screens (the device pass could not reach them; this was only checked at the API level).

## Operate / roll back
- **Deploy:** nothing to run. For emails to actually go out, the environment needs `SMTP_HOST`, `SMTP_PORT` (465, SSL), `SMTP_USER`, `SMTP_PASSWORD`, `EMAILS_FROM_EMAIL`, `EMAILS_FROM_NAME`. If they are missing, recovery still answers 200 and the log shows `SMTP is not configured`: **grep for that line if users say no reset email arrives.**
- **Roll back:** revert the commit; the endpoint goes back to a 500.

## Follow-ups
- ~~**The "code" is a ~200-character JWT.**~~ **Done 2026-11-07:** the email now carries a 5-digit code (hashed, 15 minutes, attempt-limited); the long token remains only for the shipped caregiver app, deprecated. See [2026-11-07-password-reset-code.md](2026-11-07-password-reset-code.md). The app screens still need the swap.
- ~~**Password reset should also bump `token_version`**, and `reset-password` has no password policy.~~ **Done 2026-11-07** (same SOP): a reset applies the change-password policy and ends every other session, on both the code and the token form.
- `send_verification_email` and `/resend-verification` ignore the sender's `False`; a user with broken SMTP is told "Verification code sent". Same shape as the fix here (log, answer normally); a delivery-failure signal for the FE is a separate decision.
- Verification and resend-verification still answer `404 User not found` / `400 Email already verified`, which also reveal whether an email is registered.
