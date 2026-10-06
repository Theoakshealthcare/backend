# SOP: Auth and access control (WP-C)

| | |
|---|---|
| **Date** | 2026-10-05 |
| **Branch** | `security/access-control` (base `test/harness`) |
| **Commits** | `e43fadb` token type, `3fb9836` prescriptions, `35624e2` booking reads and actions, `33fdc2a` WebSockets, `e2b422f` private documents |
| **Status** | Verified locally (103 tests pass); not pushed |

> **Updated by A0 (2026-10-06):** the admin check described below is gone. Admins are `admin_users` accounts with their own tokens (`aud=admin`), not a `users` row plus an `admins` row. `get_current_admin` and `user_is_admin` were replaced by `require_permission(...)`; the booking, document and prescription routes that also served admins now use `get_user_or_admin(permission)` (`bookings.read`, `documents.read`, `prescriptions.manage`). An app-user token on an admin route is `401` (it was `403`). See [admin auth](2026-10-06-admin-auth.md).

## What shipped
Seven access-control fixes, each with tests for no token, wrong user, right user and a refresh token.

| # | Fix |
|---|---|
| 1 | `get_current_user` accepts only `type=access` tokens and active users. |
| 2 | Both booking WebSockets authenticate with a JWT. The demo user ids are gone. |
| 3 | `/booking/ws/stats` is admin-only. `/auth/test-cloudinary` is deleted. |
| 4 | Prescriptions need auth. A patient sees and creates only their own. Update, status change and note upload are admin-only. |
| 5 | `GET /booking/booking/{id}` and `/live-tracking` are limited to the booking's patient, its assigned caregiver, or an admin. Live tracking also returns 409 unless the booking is accepted, en route or in progress (admins exempt; `en_route` was added by C3), so a caregiver's location isn't visible after the job. |
| 6 | Caregiver booking actions are checked for who and for state. Accept has exactly one winner. |
| 7 | Caregiver documents are stored privately and served only to the owner or an admin. |

## Why
The audit found that:
- a refresh token worked as an access token, and inactive users could still call most routes;
- both WebSockets accepted any non-empty token and ran as a hardcoded demo user;
- any logged-in (or anonymous) caller could read or change any prescription;
- any caregiver could accept, start, complete or cancel any booking, including unpaid ones, and two caregivers could both accept the same booking;
- a user with an unexpected `user_type` could read any booking;
- ID documents were served from the public `/uploads` mount.

## How
| Decision | Choice | Why |
|---|---|---|
| Inactive users | Checked in `get_current_user` itself (403 `Inactive user`) | Every route that depends on it, directly or through `get_current_active_user`, is covered. Before, only some routes used the active-user variant, and it returned 400. |
| Admin check | `user_is_admin(db, user)` in `app/api/deps.py` (an `Admin` row) | Admins aren't a `user_type` yet. Matches `get_current_admin`. |
| WebSocket token | `?token=<jwt>` or `Sec-WebSocket-Protocol: bearer, <jwt>` (the server echoes `bearer`) | Mobile and browser sockets can't set an `Authorization` header. The header form keeps the token out of access logs. |
| WebSocket close codes | 4401 missing, invalid, refresh, inactive or unknown user. 4403 wrong role. | The socket is accepted and then closed with the code. Closing before `accept()` makes uvicorn answer the handshake with HTTP 403, so browsers and Dart would never see 4401/4403 (proved by a real-uvicorn test). Nothing is registered or sent on a rejected socket. |
| Tracking subscribe | Patient owns the booking and status is `accepted`, `en_route` or `in_progress`. "Not found" and "not yours" give the same error. | Ids can't be probed. A WebSocket session lives as long as the socket, so the booking is re-read with `populate_existing`. |
| Accept race | `SELECT … FOR UPDATE` on the booking row (`with_for_update().populate_existing()`) before any check | Concurrent accepts serialise, so exactly one sees `pending`. A conditional UPDATE would also work. The lock keeps the validation readable. |
| Action errors | `BookingActionForbidden` is 403, `BookingStateConflict` is 409, `ValueError` stays 404 | Error shape stays `{"detail": "..."}`. |
| Prescription denial | 403 for an existing record you don't own (403 on all prescription routes) | Matches the plan's contract. Revisit if we want to hide existence (404). |
| Prescription requester | `requested_by_user_id` is overwritten with the caller | The client value was trusted before. |
| Document storage | `PRIVATE_UPLOAD_DIR`; `file_path` is now a key relative to it (`caregivers/<id>/documents/<uuid>.<ext>`) | Moving the volume doesn't break rows. The extension comes from the validated content type. The old code used the client filename extension, which allowed path traversal. |
| Document serving | `FileResponse` with the stored MIME type, `inline`, `nosniff`, `Cache-Control: private, no-store`. The path is resolved and must stay inside the private dir. | A bad DB row can't be used to read other files. |
| Static mount | `/uploads/profile_images` only (was all of `/uploads`) | Nothing else under `uploads/` is public. |

Alternatives rejected:
- A signed URL for documents: more moving parts, and the owner/admin check is enough for now.
- Token as the first WebSocket message: the socket would be accepted before it is authenticated.

## What's involved
- `app/api/deps.py`: token type and active check, `user_is_admin`, `authenticate_websocket`.
- `app/api/v1/endpoints/booking.py`: `ensure_booking_read_access`, the WebSocket endpoints, `/ws/stats`, action error mapping.
- `app/services/booking.py`: `handle_booking_action` rewritten (lock, ownership, state rules); `BookingActionForbidden`, `BookingStateConflict`.
- `app/services/websocket_manager.py`: subprotocol on accept; subscribe checks owner and status; no internal error text sent to clients.
- `app/api/v1/endpoints/prescription.py`: auth on every route, `_require_patient_access`, admin-only mutations.
- `app/api/v1/endpoints/auth.py`: `/test-cloudinary` removed.
- `app/api/v1/endpoints/caregiver.py`: `GET /caregiver/documents/{id}/file`.
- `app/services/caregiver.py`: private storage and `resolve_document_path`.
- `app/main.py`: static mount narrowed.
- `app/core/config.py`: `PRIVATE_UPLOAD_DIR` (default `private_uploads`).
- `Dockerfile`, `docker-compose.yml`: `/data/private_uploads` volume and `PRIVATE_UPLOAD_DIR` env. `.gitignore`: `private_uploads/`.
- `scripts/migrate_private_documents.py`: one-off migration.
- Tests: `app/tests/test_access_auth.py`, `_websocket.py`, `_prescriptions.py`, `_booking.py`, `_documents.py`.

Two pre-existing 500s on these paths were fixed because the happy path couldn't be tested otherwise:
- `enrich_booking_with_details` put the ORM `_sa_instance_state` into the response dict, so every caregiver action failed to serialise;
- live tracking passed `None` for `is_moving` when the caregiver had no speed.

## Verification
- Every new test failed first, then passed. `test_concurrent_accepts_have_exactly_one_winner` (6 threads, 6 notified caregivers) fails when the row lock is removed and passes with it.
- CI steps, all green locally: `poetry check --lock`, ruff bug-class rules, `scripts.ci_smoke` (89 operations), `pytest` (103 passed).
- Not exercised: a real mobile client over the WebSocket subprotocol header; the migration script against production data.

## Operate / roll back

> **Update 2026-10-05 (E1):** There is no production data, so the legacy-document migration was never needed. `scripts/migrate_private_documents.py` and its tests have been removed. Skip the migration steps below. See [the migration baseline SOP](2026-10-05-migration-baseline.md).
- **Deploy order:** deploy the code, then run the migration once on the host:
  ```
  docker compose exec api python -m scripts.migrate_private_documents --dry-run
  docker compose exec api python -m scripts.migrate_private_documents
  ```
  Until it runs, old documents return 404 from the new endpoint (and are no longer served from `/uploads` either). Take a copy of `uploads/caregivers` first.
- Needs the `private_uploads` volume from `docker-compose.yml`. If the container is run without the compose volume, set `PRIVATE_UPLOAD_DIR` to a persistent path. Local disk is wiped on redeploy (see the checklist item on moving documents to Cloudinary or S3).
- Roll back: revert the commits. Migrated files would then need to be moved back to `uploads/caregivers/...` and `file_path` prefixed with `uploads/` (the script has no reverse mode).

## Review fixes (same branch)
- WebSocket rejections are accept-then-close, tested with a real uvicorn server and the `websockets` client.
- REST live tracking is limited to accepted/in-progress bookings (409 otherwise).
- `POST /caregiver/identity-verification/result` stores the payload as evidence only; the status can go to `in_progress` at most and never overwrites `verified`/`failed`. Those become admin decisions in WP-E5. Until then, nothing sets `verified` through the API.

- `PrescriptionUpdate` no longer has `payment_status`, `total_cost` or `paystack_reference`; the schema forbids extra fields, so sending them is a 422 (not a silent ignore). Payment state changes only via the webhook/settlement.

## Client impact (tell mobile)
- Identity verification results no longer change status to verified/failed.
- Live tracking REST returns 409 outside accepted/in-progress.
- WebSocket clients must send a JWT access token and handle close codes 4401 and 4403.
- `DocumentUploadResponse.file_path` is now a storage key, not a URL. Fetch documents with `GET /caregiver/documents/{id}/file` and a bearer token.
- Caregiver action errors are now 403 and 409 (they were silent no-ops returning 200).
- Prescription settings and availability routes now need a token.
- Inactive users get 403 `Inactive user` (was 400 on some routes, none on others).

## Follow-ups
- Admin routes in `endpoints/patient.py:320,362` compare `user_type` to `"admin"`, which is never true. Use `get_current_admin`.
- Chat and notes routes don't allow admins; decide whether support should read them.
- WebSocket state is per process; it needs Redis if we run more than one worker.
- Move document storage off local disk (Cloudinary or S3).
- The unused `endpoints/websocket.py` imports a function that doesn't exist; it isn't mounted. Delete it when someone owns that cleanup.

## Addendum, 2026-10-05: document download hardening
The download route `GET /caregiver/documents/{id}/file` no longer trusts the stored `mime_type`:
- **Allowlisted types render inline.** Only `application/pdf`, `image/jpeg` and `image/png` are served `inline` with their own content type.
- **Everything else downloads.** Any other type, including `NULL` and legacy rows moved by `migrate_private_documents.py`, is served as `application/octet-stream` with `attachment`.
- **Response headers:** every response also carries `Content-Security-Policy: sandbox; default-src 'none'` (so an inline PDF can't run script in the API origin), `X-Content-Type-Options: nosniff` and `Cache-Control: private, no-store`.
- **Uploads:** the upload route already rejects non-allowlisted types.

The automated security scan flagged this as a stored-XSS risk. Tests: `test_non_allowlisted_stored_type_is_forced_to_download` (text/html, SVG, XHTML, none) and `test_allowlisted_document_is_served_sandboxed`.
