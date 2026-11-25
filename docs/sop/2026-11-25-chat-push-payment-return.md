# SOP: chat push, patient photo in the caregiver chats list, payment return page

| | |
|---|---|
| **What shipped** | (1) A push to the other person when a chat message is saved. (2) `patient_photo_url` on each item of `GET /caregiver/chats`. (3) `GET /api/v1/payment/callback`, the page Paystack redirects to after paying, and `PAYMENT_CALLBACK_URL` pointed at it. |
| **Branch / PR** | `fix/chat-push-payment-return`, cut from `oakly/develop` `9186b62`. PR on `Theoakshealthcare/backend`; not merged (waits for the owner's "merge #N"). |
| **Migration** | None. |
| **FE guide** | [`docs/fe-integration-guide-chat-push-payment-return.md`](../fe-integration-guide-chat-push-payment-return.md), rendered from `docs/fe-captures/chat-push-payment-return/`; caregiver guide "Chats list" updated. |

## Why
The owner's first real-phone test of the Android apps against staging found three gaps: a chat message produced no push; the caregiver Chats list showed a letter avatar although the patient uploaded a photo; and after paying, Paystack's redirect landed on `https://www.oaklycare.com/payment/callback`, which the website does not have (404). Booking create sent the browser to yet another missing page, `{FRONTEND_URL}/bookings/{id}/payment-complete`.

## How
| Decision | Why / alternative rejected |
|---|---|
| **Push from the REST send endpoint, via `BackgroundTasks`, on its own `SessionLocal`.** | The message is already committed, so a slow or failing Expo must never delay or fail the sender. Inline (the dispatch precedent) could hold the request for `EXPO_PUSH_TIMEOUT_SECONDS`. `chat_push.push_new_message` takes plain ids and never raises. |
| **Recipient from the booking, not the request.** | Patient sends: `bookings.caregiver_id` to the caregiver's user. Caregiver sends: `bookings.patient_id` to the patient's user. Skipped when there is no caregiver, the recipient is the sender, or the account is inactive. Token handling, dead-token pruning and "no token, no push" stay in `PushService`. There is no opt-out setting in the codebase to respect. |
| **Title / body / data.** | Caregiver sender: first name. Patient sender: first name plus last initial, the pre-accept privacy rule, because a lock screen is public. Body: text on one line, 100 characters including the `…`. `data`: only `{type, booking_id}`. |
| **`patient_photo_url` = `users.profile_image_url` of the booking's patient** (joined into the existing list statement; `""` becomes `null`). | The same value the assigned caregiver already gets as `patient_details.profile_image_url`. Always present, nullable. |
| **Patient side: no new field.** | Booking detail and list already carry `caregiver_details.profile_image_url`; there is no patient chat-list endpoint. Verified by capture. |
| **Callback page is static and reads nothing from the request.** | It cannot mark anything paid (the webhook does) and cannot reflect `reference`/`trxref`. CSP `default-src 'none'`, the one inline style allowed by its sha256 (computed from the same string, so they cannot drift), no `unsafe-inline`, no script, font or image. `Cache-Control: no-store`, `Referrer-Policy: no-referrer`. The deep link `oaklypatient://payment-complete` is a constant; no auto-redirect (the button only). |
| **`PAYMENT_CALLBACK_URL` default empty, derived from `KYC_WIDGET_PUBLIC_BASE_URL`** (`<base>/api/v1/payment/callback`), else the old website URL (local dev only). An explicit value wins. | `KYC_WIDGET_PUBLIC_BASE_URL` is the existing "API public origin" setting. Booking create and the stored payment link (`booking_payment_link._callback_url`, app channel) now use it; the WhatsApp return URL is unchanged. |

## What's involved
| Path | Change |
|---|---|
| `app/services/chat_push.py` (new) | `push_new_message`, name and preview helpers |
| `app/api/v1/endpoints/booking.py` | `send_chat_message` queues the push |
| `app/services/caregiver_chats.py`, `app/schemas/caregiver_chat.py`, `app/api/v1/endpoints/caregiver_chats.py` | `patient_photo_url` |
| `app/services/payment_return_page.py` (new), `app/api/v1/endpoints/payment.py` | the page and `GET /payment/callback` |
| `app/core/config.py`, `app/services/booking.py`, `app/services/booking_payment_link.py` | `PAYMENT_CALLBACK_URL` derivation and use |
| `app/tests/test_chat_push.py`, `test_payment_callback.py` (new); `test_caregiver_chats.py`, `test_booking_expiry.py`, `test_booking_payment_link.py`, `test_booking_payment_channels.py` | tests |
| `app/tests/test_capture_chat_push_payment.py`, `docs/fe-captures/chat-push-payment-return/`, `.github/workflows/ci.yml` | captures and the guide's `--check` |

## Verification
- Ran locally, by name: the files above plus `test_kyc_boot.py`, `test_push.py` (181 tests); the CI lint step (`ruff check --select E9,F63,F7,F82 .`); the guides' `render_fe_guide --check`.
- Live captures on the test harness (real app, real Postgres, fake Expo). **Not exercised:** a real push to a phone, a real Paystack redirect, the deep link opening the app, the WebSocket chat path (it neither saves nor pushes).

## Operate / roll back
- **Done in the env change (2026-11-25):** `PAYMENT_CALLBACK_URL` is now empty in `.env.staging.enc`, `.env.production.enc` and `deploy/env.example`, so it derives from `KYC_WIDGET_PUBLIC_BASE_URL` on each host. Only that name changed in each file.
- **Was required after merge:** set `PAYMENT_CALLBACK_URL` in the staging and production env to `<API origin>/api/v1/payment/callback` (or empty to derive it). The `.env.*.enc` files and `deploy/env.example` are not changed here; until they are, the old website value keeps being sent to Paystack.
- Roll back by reverting the PR; no data or schema to undo.

## Follow-ups
- The WebSocket `chat_message` path does not persist or push.
- No unread count or mute setting exists for chat pushes.
- A pushed message shows the text on the lock screen (as requested); a "hide previews" setting would need a preference store.
