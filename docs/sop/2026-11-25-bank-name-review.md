# SOP: a payout account in another name is saved and held for review (supersedes the DP2-05 refusal)

| | |
|---|---|
| **What shipped** | A payout account whose holder name does not look like the caregiver's profile name is no longer refused with `422`. It is saved with `name_review_status = "pending"` and no payout is made to it until Finance approves it. The name check also accepts a short form of a name (`Tosin` / `Oluwatosin`). Branch `feature/bank-name-review` (not yet pushed, no PR number); migration `0030_bank_name_review`. |
| **Why** | The DP2-05 rule ([SOP](2026-11-08-gemini-model-bank-name.md)) matched whole words only, so a caregiver registered as `Adebayo Tosin` was blocked from saving an account held as `Victor Oluwatosin Adebayo`, with "Contact support" as the only way out. Nickname versus formal name (Tosin / Oluwatosin, Tunde / Babatunde) is normal here, not an edge case, and a hard stop with no explanation is too much friction for the fraud risk it covers. |
| **How** | (A) **Review instead of refuse.** `bank_accounts.resolve()` no longer raises; it returns `needs_review`. `save()` stores the account (and registers the Paystack recipient, so approval pays at once) with `name_review_status = pending`. (B) **Looser match.** The profile's first and last words must each be a word of the account name or the start or end of one (at least 4 letters), and at least one of them a whole word. A one-word profile still needs the exact word. Finance decides through `/admin/payouts/bank-reviews`. |
| **Decisions and alternatives** | See the table below. |
| **Verification** | `test_payout_bank_accounts.py` and `test_bank_name_review.py` (new, 15 tests), `test_admin_notify.py`, `test_payouts.py` all green locally; migration upgrade, downgrade, upgrade on a throwaway Postgres. Live captures (real HTTP, local API, throwaway Postgres, fake Paystack) in `docs/fe-captures/caregiver/970-975` and `docs/fe-captures/admin/970-974`, written by `scripts/capture_bank_name_review.py`. The live run found one bug the tests had missed (`submitted_at` moved on a decision); it is fixed and has a test. Mobile: `tsc`, eslint and `bankDetails.test.tsx` (30 tests) green. **Not done:** a run against a live Paystack account, the full CI run (CI is the authority). |

## Decisions

| Decision | Why / alternative rejected |
|---|---|
| A mismatch saves the account and holds it; it does not refuse. | Same idea as the KYC check ("a mismatch is never a rejection"). Rejected: keep the `422` and loosen only the match (still a dead end for a married name). |
| Hold = `name_review_status` (`pending`, `approved`, `rejected`; `NULL` = matched). Held while `pending` or `rejected`. | One column pair, a CHECK constraint, no new table. Who decided and why is in `admin_audit_log`, not duplicated. |
| The lookup returns the name **masked** (`S**** O****`: each word's first letter and a fixed four stars, so even the word lengths are not shown) when `needs_review` is true, and so does the saved account (`PUT` and `GET /bank-account`) while it is `pending` or `rejected`; the real name shows once it is `approved`. | Without this, `POST /bank-account/resolve` would turn any account number into a person's name for any caregiver. The first version masked only the lookup, and saving the account returned the real name (an automated security review caught the bypass; fixed in `_bank_out`, with tests and re-captured 972, 973, 975). The 10 lookups a minute limit stays. |
| Short form: one word may be the start or end of a longer one only if it has 4+ letters, and one of the two words must be exact. | `Ada` is not `Adaeze`; two short forms with no shared whole word (`Tosin Tunde` vs `Oluwatosin Babatunde`) go to review. Rejected: any substring match (too loose). |
| A save restarts the review: a matching account clears it, a flagged one is `pending` again, and saving replaces an approved account. | One account row per caregiver. The 24 hour pause after a change still applies on top. |
| `review_token` is returned by the queue and required in the decision body; a mismatch is `409`. It is an HMAC (keyed with the server secret) over the caregiver id, account number, bank code and holder name, so any change of account (even one that ends in the same four digits) invalidates it. | The caregiver can save a different account while a reviewer has the old one open. A decision must not carry over. (Security review 2026-11-25: the first version compared only the last four digits.) |
| Decisions need `payouts.execute` (Finance, Super Admin); reading needs `payments.read`. Flagged accounts e-mail admins who hold `payouts.execute`. | Deciding where money goes is a payouts action. The e-mail reuses `admin_notify` (best effort, after commit), now routed by permission. |
| `flag_modified(account, "updated_at")` in `review_name`. | Found by the live capture: `onupdate` moved `updated_at`, so the queue's `submitted_at` jumped to the decision time. |

## What's involved

- `app/services/bank_accounts.py`: `account_name_matches_profile` (short forms), `mask_name`, `Resolved.needs_review`, `save()` (pending status, notify), `is_on_name_hold`, `review_name`, `ReviewNotFound`/`ReviewConflict`. `BankNameMismatch` is gone.
- `app/services/payouts.py`: `_eligibility_problem` and the `create_batch` candidate query skip a held account.
- `app/api/v1/endpoints/admin_payouts.py`: `GET /bank-reviews`, `POST /bank-reviews/{caregiver_id}/approve|reject`; `app/schemas/admin_payout.py`.
- `app/api/v1/endpoints/caregiver.py`, `app/schemas/caregiver.py`: `needs_review` on resolve, `name_review_status` on the account.
- `app/services/admin_notify.py`: event `bank_name_review`, recipients by permission (`ROUTES`).
- `app/db/models/caregiver.py`, `app/alembic/versions/0030_bank_name_review.py`.
- Mobile (`caregiver` monorepo): `BankDetails.tsx` (warning and "Save for review", pending and rejected notices), `errors.ts` (the old 422 code and message removed), regenerated `packages/api-client`.
- Docs: caregiver guide 10.3, admin guide 12.11, both verification tables.

## Operate / roll back

- Deploy: run migration `0030` (adds two nullable columns and a CHECK; no existing account is held).
- Roll back: `alembic downgrade 0029_location_nudged_at` drops both columns, after which a held account is paid like any other. **Decide the open reviews first** (`GET /admin/payouts/bank-reviews`). An older mobile build still works: it ignores the new fields, but it will not warn the caregiver that the account is held.
- Finance reads the queue at `GET /admin/payouts/bank-reviews`; there is no console page yet.

## Follow-ups

- **Security review 2026-11-25 (automated), answered:** (1) decision bound to the exact account by `review_token`: fixed. (3) the masked name at the lookup keeps each word's initial; the length is no longer shown; the lookup needs a login and is limited to 10 a minute, and the owner chose to show a hint so a caregiver can spot a mistyped number: accepted, revisit if abuse shows. (2) short-form matching (`Tosin` / `Oluwatosin`) is the owner's explicit choice (option B, 2026-11-25); at least one of the profile's first/last words must still match a whole word, and a flagged account is held anyway: accepted.

- **Admin console page** for the queue (both names side by side, approve and reject with a reason). Until it exists Finance uses the API.
- **Tell the caregiver** when a decision is made (push or e-mail), and show a held payout on the wallet summary.
- Live Paystack check that the real name lookup returns names in the shapes the matcher expects.
- Sonar and Opus review of the money-path diff (`payouts.py`, `bank_accounts.py`) if wanted before merge.
