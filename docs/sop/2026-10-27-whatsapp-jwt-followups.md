# SOP: WhatsApp and JWT follow-ups

| | |
|---|---|
| **Date** | 2026-10-27 |
| **Branch** | `fix/whatsapp-jwt-followups`, based on `oakly/main` `f4b2f80` (refunds v2, #19). PR: see the checklist |
| **Migration** | none |
| **Sources** | Live Meta WhatsApp test of 2026-10-19 (findings, redacted payloads in `meta-test/meta-real-payloads-2026-10-19.redacted.json`), the Semgrep flag on `app/api/deps.py` found during refunds v2, stale checklist lines |

## What shipped
Four small fixes, no behaviour change for valid traffic:

| # | Change | Where |
|---|---|---|
| 1 | The fake Meta now sends the business-scoped user ids real payloads carry; a test proves the webhook accepts and ignores them | `app/tests/fake_meta.py`, `test_whatsapp_webhook.py` |
| 2 | Uvicorn's access log no longer prints the webhook's `hub.verify_token` | `app/core/log_filters.py`, `app/main.py` |
| 3 | The 413 on an oversized webhook body uses the non-deprecated Starlette constant | `app/api/v1/endpoints/whatsapp.py:29` |
| 4 | `get_user_or_admin` no longer decodes a token without verifying it | `app/api/deps.py` |

## Why
1. **BSUIDs.** The real payloads have `contacts[].user_id`, `messages[].from_user_id` and `statuses[].recipient_user_id` (format `NG.<digits>`). Our fake lacked them, so no test ever saw a payload shaped like Meta's.
2. **Verify token in logs.** Meta's handshake is `GET /api/v1/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=<secret>&hub.challenge=...`. Uvicorn's access line prints the full path with the query string, so the secret was written to the logs on each (re)subscription. It was seen once during the live test.
3. **413.** `HTTP_413_REQUEST_ENTITY_TOO_LARGE` is deprecated in Starlette 1.x (the value is the same, 413).
4. **JWT peek.** `get_user_or_admin` (the dependency of the few routes shared by app users and the console) called `jwt.decode(..., verify_signature=False)` only to read `aud` and choose a decoder. Nothing was trusted from it, but reading claims before verifying is the pattern Semgrep flags, and it made the safety of the routing depend on a later step.

## How
- **BSUIDs:** `bsuid(wa_id)` builds a stable placeholder (`NG.` plus 20 digits) and the three builders (`_message_value`, `status_payload`) add it in all three places, so every existing WhatsApp test now runs on Meta-shaped payloads. **Decision (owner/coordinator): do not store them for now.** `parse_webhook` reads fields by name, so the extras are ignored; the test asserts nothing derived from them reaches the queue, the contact, the conversation or the message rows.
- **Log filter:** `AccessLogRedactionFilter` is attached to the `uvicorn.access` logger at import of `app.main` (`install_access_log_redaction`, idempotent). A uvicorn access record has `args = (client, method, path, http_version, status)`; for the exact webhook path (`{API_V1_STR}/whatsapp/webhook`) the value of `hub.verify_token` becomes `[redacted]`, matched on the percent-decoded key so `hub%2Everify_token` is caught, repeats included. The rest of the query (`hub.mode`, `hub.challenge`) stays for debugging. A record of any other shape passes through untouched and the filter never drops a record or raises. It works under both launch modes in `boot/docker-run.sh` (uvicorn directly, and gunicorn's `UvicornWorker`) because both log through `uvicorn.access`. **Rejected:** suppressing the line entirely (loses request visibility) and a regex on the formatted message (the filter sees structured args, which is more reliable).
- **JWT:** the admin decoder goes first (`decode_admin_token`: HS256 with the derived admin key, `aud="admin"`, `exp`/`sub`/`aud`/`type` required, `type="access"`). Only a token it refuses is offered to the app-user decoder (`decode_app_token`: `SECRET_KEY`, no audience, so any token carrying `aud` is refused). An admin token then goes through the same live-session and permission checks as before (`_admin_from_claims`, split out of `_admin_from_token` so the token is verified once). Different keys plus the audience rule mean no token can pass both decoders, so there is no new way to cross user and admin tokens. Status codes and details are unchanged for every class of token (401 "Could not validate credentials", 403 missing permission, 403 "Inactive user").

## What's involved
| Path | Change |
|---|---|
| `app/api/deps.py` | `get_user_or_admin` rewritten; `_admin_from_claims` split out |
| `app/core/log_filters.py` | new: filter and installer |
| `app/main.py` | calls `install_access_log_redaction()` |
| `app/api/v1/endpoints/whatsapp.py` | `HTTP_413_CONTENT_TOO_LARGE` |
| `app/tests/fake_meta.py` | `bsuid()` and the three BSUID fields |
| `app/tests/test_get_user_or_admin.py` | new, 21 tests on a probe app |
| `app/tests/test_access_log_redaction.py` | new, 14 tests |
| `app/tests/test_whatsapp_webhook.py` | BSUID test |
| `docs/checklist/MASTER_CHECKLIST.md` | refunds v2 marked merged (#19, `f4b2f80`), snapshot counts recounted (210 done, 158 open), section 8 header made honest (remaining screens unbuilt, device run pending), Real-Meta notes |

## Verification
- **Tests first for the JWT change:** the 19 behaviour tests passed on the old code (they pin current behaviour; the disabled-admin and inactive-user case was added after the review); only `test_no_token_is_ever_decoded_without_verification` (a `jwt.decode` guard that fails on any `verify_signature=False`) was red, then green after the change.
- The new tests cover: valid app and admin tokens; an admin without the permission (403); a disabled admin (401); an inactive user (403); an app token for an admin's id and an admin token for a user's id (both 401); an app-signed token claiming `aud=admin` and an admin-signed token with no `aud` (401); expired, wrong key, `alg: none`, tampered signature, malformed, refresh-type and revoked-session tokens (401).
- **One read-only Opus review** of the auth diff: verdict ship, no findings. It suggested two extra tests (disabled admin, inactive user), added. It noted an admin-signed token with a non-string `sid` would raise (500) in `_admin_from_claims`; unreachable without the admin signing key and the same as before (see follow-ups).
- Full local CI as `.github/workflows/ci.yml` runs it: see the result in the PR description and the checklist.
- **Not verified live:** the redaction was exercised with records shaped like uvicorn's and through `AccessFormatter`, not by running a uvicorn server and calling the webhook; real Meta delivery is still blocked (below).

## Operate / roll back
No config, env or migration. Roll back by reverting the commit; the log filter is the only runtime addition and can be removed by deleting the `install_access_log_redaction()` call. **Owner action:** the current `WHATSAPP_VERIFY_TOKEN` was printed once in an access log before this fix; rotate it at the next Meta test (change `backend/.env`, recreate api and worker, update the token in Meta's webhook configuration).

## Follow-ups
- **BSUIDs are not stored.** Decide whether to keep `user_id` on `whatsapp_contacts` (a stable key if Meta stops sending phone numbers) before relying on them.
- Real Meta delivery is blocked by business verification (started 2026-10-27, takes days); the "Real-Meta check" checklist item stays open until a reply is delivered.
- Optional hardening: treat a non-string `sid` claim in `_admin_from_claims` as 401 (`TypeError`/`AttributeError`), unreachable without the signing key.
- Other places that log query strings (reverse proxy access logs, ngrok inspector) are outside this change.
