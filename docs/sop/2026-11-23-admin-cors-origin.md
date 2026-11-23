# SOP: https://admin.oaklycare.com added to the API's CORS origins

| | |
|---|---|
| **What shipped** | `BACKEND_CORS_ORIGINS` in `.env.staging.enc` (5 entries) and `.env.production.enc` (4 entries) now includes `https://admin.oaklycare.com`, where the admin console will run as its own Vercel project. |
| **Why** | The admin console moves to its own Vercel project at `admin.oaklycare.com` (decided with the owner, 2026-11-23). The console talks to the API through its own server-side proxy, so the browser may not need CORS at all; the origin is added as a precaution and costs nothing. |
| **Not verified** | A real browser call from `admin.oaklycare.com` (the project and domain do not exist yet). Only decryption, the diff and `Settings` loading were checked. |

## How
The owner edited and re-encrypted both files locally. Their local plaintext `.env.staging` was older than `develop`: re-encrypting it would also have reverted `GUNICORN_WORKERS` (3), `GUNICORN_MAX_REQUESTS` (5000) and `VOICE_NOTES_ENABLED` (true) that merged in backend #68 and #69. The staging file committed here is `develop`'s decrypted file with ONLY the `BACKEND_CORS_ORIGINS` line taken from the owner's file (the owner's original is kept outside the repo, still encrypted). The production file was committed as the owner made it: only that line differs.

## Verification
Both files decrypt with the shared key; a names-only diff against `develop` shows only `BACKEND_CORS_ORIGINS` changed in each; the staging file loads in `Settings` (with the compose-built URLs added); no values were printed.

## Operate / roll back
Staging redeploys on merge to `develop`; production only on a release. Revert the PR to remove the origin.

## Follow-ups
- **Re-sync your local plaintext after the merge:** `./scripts/env.sh decrypt staging` (it will ask before overwriting) so the next edit starts from `develop`'s values, not a stale file.
- Production CORS takes effect only at the next release (`git push oakly oakly/develop:main` after sign-off).
