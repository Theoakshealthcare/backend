# SOP: `arrived` errors as `{code, message}`, and the booking's cancel terms on the admin detail

| | |
|---|---|
| **What shipped** | **1.** The caregiver `arrived` action's errors use the backend convention `detail = {code, message}` with stable codes. **2.** `GET /admin/bookings/{id}` gains `cancel_terms`: the snapshot stored on the booking, the tiers in force, what a booker cancel would cost now, and what a booker cancel applied. **3.** The real Gemini audio proof (recorded in [the AI note SOP](2026-11-08-ai-note-transcription.md), Verification). |
| **Branch / commits** | `fix/arrived-errors-admin-cancel-terms`, cut from `oakly/main` `451e292` (after #50). Not pushed; the lead pushes and opens the PR. Commit SHAs are in the lead's report. |
| **Migration** | None (Alembic head stays `0025`). |
| **API shape** | Item 1 changes an existing error shape for one action (plain string to object); item 2 is additive. |

## Why
- **Arrived errors.** The slice that added `arrived` copied the generic string errors. The convention elsewhere (auth, KYC, care plans, voice notes, admin refunds) is `detail = {code, message}`, which lets the app branch on a stable code instead of on wording.
- **Cancel terms.** Support and Finance could see a booking's price and refund fields but not the terms a family cancelled under, so a "why was I charged" call meant reading the policy history. The terms the family is quoted (`GET /booking/{id}/cancel-terms`) were not visible to staff.

## How (decisions)
| Decision | Why |
|---|---|
| Only `arrived` moves to coded errors; `start`, `complete`, `cancel`, `accept`, `decline`, `on_my_way` keep plain strings | The caregiver app reads those strings today (it shows `detail` as the message). Changing them all at once is a bigger, riskier app change. The exceptions carry an optional `code`, and a small allow-list (`_CODED_ACTION_ERRORS` in `app/api/v1/endpoints/booking.py`) turns it on per action, so the next action is a one-line change plus its codes |
| Messages are unchanged | The app can keep showing `message` once it reads the object |
| The two shared guards (`This booking is not assigned to you`, `Your account is not approved to start visits`) carry a code only when the action is `arrived` | They are shared by on_my_way / start / complete / cancel, which must keep their strings |
| `cancel_terms.would_apply_now` calls `booking_cancellation.terms_for` and `policy_for`, and `applied` calls `patient_booking_cancel.cancel_fee_kobo` | One function answers the family's quote, the cancel itself and now the admin view, so the three cannot disagree. No new tier arithmetic |
| Money in `cancel_terms` is **kobo** | The admin API's convention (`price`, `payment`) |
| `snapshot` is nullable and `policy_source` says where the tiers come from | A live check showed an **app** booking (`POST /booking/create`) stores no snapshot at creation: it is only frozen when the booker cancels (`patient_booking_cancel._cancel_paid`). A **WhatsApp** booking stores the quote's tiers (`channels/whatsapp/booking_chat.py`). The model comment and the policy docstring say "set when the quote is made", true for WhatsApp only. Before a cancel, an app booking follows the live policy |
| Rejected: also exposing the live policy next to a stored snapshot | The policy screen already shows it; `policy` is the tiers actually in force for this booking |

## What's involved
| Path | Change |
|---|---|
| `app/services/booking.py` | `BookingActionForbidden` and `BookingStateConflict` take an optional `code`; arrived passes `booking_not_assigned`, `caregiver_not_approved`, `booking_not_arrivable` |
| `app/api/v1/endpoints/booking.py` | `_action_detail` and `_CODED_ACTION_ERRORS`; the 404s (`caregiver_profile_not_found`, `booking_not_found`) for arrived |
| `app/schemas/admin_booking.py`, `app/services/admin_bookings.py` | `CancelTermsOut` and friends; `AdminBookingService._cancel_terms`; `BookingDetail.cancel_terms` |
| `app/tests/test_caregiver_arrived_earning.py`, `app/tests/test_admin_booking_cancel_terms.py` | updated and new tests |
| `scripts/capture_arrived_errors_cancel_terms.py` (new), `scripts/capture_arrival_earning.py` | the new live capture script (blocks 800+ of the caregiver and admin capture folders); the old script now clears only its own 700 to 799 block |
| `docs/fe-integration-guide-caregiver.md`, `docs/fe-integration-guide-admin.md` (12.4a) | re-rendered from captures `caregiver/800-804`, `admin/800-808`; the old arrived-error captures `705`, `708`, `714` were removed and their index entries dropped |

## Verification
| Check | Result |
|---|---|
| Unit tests | `test_caregiver_arrived_earning.py` (the three error shapes, a suspended caregiver, an unknown booking, a login with no profile, and that `start` still answers a plain string) and `test_admin_booking_cancel_terms.py` (tiers 48 h / 12 h / 2 h out, the snapshot kept when the live policy changes, an old booking with no snapshot, in-progress / completed / unpaid refusals, the applied fee after a booker cancel). Booking-area suites run with them (`test_visit_lifecycle`, `test_visit_rules`, `test_response_shapes`, `test_access_booking`, `test_caregiver_cancel_window`, `test_admin_bookings`, `test_patient_booking_cancel`): 175 + 119 passed, one at a time under the pytest lock |
| Live captures | Real HTTP on a scratch Postgres + Redis (no worker), local fake Paystack, bookings made through `/booking/create` and paid by a signed webhook: caregiver `800-804`, admin `800-808` |
| Not live | `caregiver_profile_not_found` and `caregiver_not_approved` (unit tests only: suspending a caregiver with an accepted booking also reassigns it); `would_apply_now` for an in-progress booking and `applied.late_cancel_compensable` (unit tests only) |
| Local CI | `poetry check --lock`, ruff `E9,F63,F7,F82`, `ci_smoke`, all eight `render_fe_guide --check` pairs. The full pytest suite was **not** run locally (CI is the backstop) |

## Operate / roll back
Revert the commits; nothing is stored. The admin response gains a field and the caregiver `arrived` errors change shape, so roll back the app change first if it has shipped.

## Follow-ups
1. **Caregiver app** (not in this PR): read `detail` as `string | {code, message}` for the `arrived` action (today it shows a plain `detail` string, so an object would render as `[object Object]` or break). Branch on `booking_not_assigned`, `booking_not_arrivable`, `caregiver_not_approved`, `booking_not_found`. Ship the app change before or with the backend deploy.
2. Move the other caregiver actions to coded errors, then drop the allow-list.
3. **Cancel policy snapshot is not taken at create for app bookings** (WhatsApp bookings do; the model comment, the `booking_policy` docstring and admin guide 12.9 say both do): decide whether an app family should keep the terms they saw at booking time. Until then an admin policy edit changes what an active app booking would pay.
4. Admin console: show `cancel_terms` on the booking detail (monorepo PR `feat/admin-booking-policy-short-visit`).
5. Recommendation, no code (question from the lead): the `arrived` step has no time window. Suggest the same shape as `on_my_way`: allowed from `onmyway_compensation_window_minutes` (120 by default) before the visit start, answered `409` `arrived_too_early` with the minutes in the message. Reason: a caregiver can currently tap "I've arrived" days early and the family's push says they are at the door. Keep it a policy field (reuse the existing window to avoid a new setting) and keep `start` ungated as the owner decided.
