# SOP: Paystack test keys rotated (staging and production files)

| | |
|---|---|
| **What shipped** | `PAYSTACK_SECRET_KEY` and `PAYSTACK_PUBLIC_KEY` replaced in `.env.staging.enc` and `.env.production.enc`. Only those two names changed in each file. |
| **Why** | The owner issued new Paystack keys. Both pairs are still **test-mode** (`sk_test_` / `pk_test_`), so real money cannot move. Production files carry test keys until go-live. |
| **Not verified** | A payment through the new keys on staging (needs the owner's Card and Bank Transfer test runs after the deploy). |

## How
The owner's local plaintext files were older than `develop` (they would have reverted staging workers, `GUNICORN_MAX_REQUESTS`, `KYC_ALERT_EMAIL` and voice notes). So each file was decrypted in memory from `develop`, only the two Paystack lines replaced with the owner's values, re-encrypted with the shared key (same parameters), round trip checked, and a names-only diff against `develop` showed only those two names differ. No values were printed.

## Webhook
Paystack dashboard (Test mode) > Settings > API Keys & Webhooks > Test Webhook URL:
`https://staging-api.oaklycare.com/api/v1/payment/paystack/webhook`
Route: `app/api/v1/endpoints/payment.py` (`POST /payment/paystack/webhook`). Requests are verified with the `x-paystack-signature` header over the raw body, using `PAYSTACK_SECRET_KEY`, so the webhook only works once the deployed key matches the key on the dashboard. At go-live set the Live Webhook URL to the production API host.

## Operate / roll back
Staging redeploys on merge to `develop`. Revert the PR to restore the previous keys.

## Follow-ups
Re-sync local plaintext (`./scripts/env.sh decrypt staging` and `production`) so the next edit starts from the committed values.
