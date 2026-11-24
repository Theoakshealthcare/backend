# SOP: KYC alert email address set (staging and production)

| | |
|---|---|
| **What shipped** | `KYC_ALERT_EMAIL` is now set in `.env.staging.enc` and `.env.production.enc` (it was empty). Only that name changed in each file. |
| **Why** | When Dojah's wallet is out of funds, or Dojah refuses our key, or the key is not configured, every caregiver identity check fails at once. `app/services/kyc_notify.py::alert_ops` always logs an ERROR line and, only when `KYC_ALERT_EMAIL` is set, also emails the team ("KYC provider needs attention": a KIND such as wallet_empty, never a person). With the setting empty nobody would have been told. |
| **Not verified** | A real alert email arriving (it needs a real Dojah failure; the code path is covered by tests with a fake sender). Staging redeploys on merge; production only at the next release. |

## How
Each file was decrypted in memory from `develop`, the single `KYC_ALERT_EMAIL` line replaced, re-encrypted with the shared key (same parameters), round trip checked, and a names-only diff against `develop` showed only that name differed in each. No other value moved, so workers, recycle limit and voice notes are untouched. The staging and production files now carry the same alert address (an owner decision).

## Verification
Both files decrypt with the shared key and load in `Settings` (with the compose-built URLs added); no values printed.

## Operate / roll back
Change or clear the value with `./scripts/env.sh` (decrypt first), or revert the PR. Staging redeploys on merge to `develop`.

## Follow-ups
Send a real alert once (for example by temporarily using a wrong Dojah key on staging) to prove the email arrives; add a separate production address if the team changes.
