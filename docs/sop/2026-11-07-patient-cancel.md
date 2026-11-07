# SOP: patient app cancels a booking and sees the fee first (P4-BE)

| | |
|---|---|
| **What shipped** | `GET /api/v1/booking/{id}/cancel-terms` (what cancelling costs right now) and `POST /api/v1/booking/{id}/cancel` for the patient app, over the same `booking_cancellation` code WhatsApp uses. `BookingResponse` gains `cancel_fee_tier`, `cancel_fee` and a `refund {status, amount}` summary (patient views only). Closes the old checklist item "Cancel booking calls an endpoint that doesn't exist" on the backend side. |
| **Branch / commits** | `feature/patient-cancel`, cut from `oakly/main` `de0a0fa`: `a756087` (feature), `cf6c854` (Opus review fixes), then the docs and capture commit. Not pushed; the lead opens the PR. |
| **Migration** | None. |
| **FE guide** | [`docs/fe-integration-guide-patient-bookings.md`](../fe-integration-guide-patient-bookings.md), every example rendered from [`docs/fe-captures/patient/`](../fe-captures/patient/) (60 live captures, `scripts/capture_patient_flow.py`). |

## Why
The patient app called `POST /booking/patient/cancel/{id}`, which never existed; family cancel with fee tiers existed only inside the WhatsApp chat (`app/channels/whatsapp/my_bookings.py`). Owner decisions (2026-11-07): app and WhatsApp coexist, refunds are never automatic (RF-D8), families get refunds by support plus this cancel flow, and the patient wallet is hidden in v1.

## How
| Decision | Why / alternative rejected |
|---|---|
| New wrapper `app/services/patient_booking_cancel.py` over `booking_cancellation.terms_for` / `cancel_by_booker`; no policy code was copied. | One rule set for both channels. Rejected: a second implementation in the endpoint. |
| **Booker only**: `403 not_your_booking` for another patient, `404 booking_not_found`, same rule as WhatsApp's `_is_booker` (the maker of a WhatsApp booking may cancel it through the app too). | |
| Errors are `detail = {"code", "message"}` (the convention of change-password and KYC); `terms_changed` also carries `detail.terms`. | Stable codes the app can switch on. |
| **Idempotent**: a repeat of the booker's own cancel returns `200` with `already_cancelled: true` and the stored result; cancelled for any other cause is `409 booking_already_cancelled`. | The brief asked for both "409 for already cancelled" and "a retry returns the same result"; replay for the booker's own cancel, 409 otherwise, satisfies both. |
| `accepted_fee_kobo` (optional, send always): if the fee decided under the booking lock is higher, `409 terms_changed` with the new terms and nothing changes. | Closes the window between "the family looked" and "the caregiver accepted / the 12 h line passed". WhatsApp does this already (the button carries the fee). |
| **Unpaid booking** (pending, payment pending or failed): Paystack is asked first (`verify_and_settle`, no lock held across the call); `503 payment_check_unavailable` if it cannot be asked, `409 payment_under_review` on an amount mismatch; still unpaid: cancelled free with no refund; found paid: cancelled as a paid booking. | Same verify-before-cancel as WhatsApp, so a payment in flight is never cancelled away. |
| **Refund**: `cancel_by_booker` queues the refund exactly as RF-D8 says (`pending_approval`, system origin, amount = paid minus tier fee, capped at what is left). The app is only ever told `original_method`; no wallet field exists on these responses. The family's reason is stored on the refund's `note` (there is no booking column for it). | Nothing moves money until a person approves. |
| The cancel **freezes the live policy** onto `bookings.cancel_policy` when the booking carries no snapshot (app bookings), and the fee shown afterwards is recomputed from tier plus that snapshot. | Found in the Opus review: a settled refund overwrites `refund_amount_kobo`, and an admin could edit the live policy, so neither can be the source of the displayed fee. |
| `cancel_by_booker` now also **closes the caregivers' unanswered offers** (`response = "booking_cancelled"`), as admin cancel and drop-out already did. | Review finding: cancelled bookings stayed in offered caregivers' "new requests". Applies to WhatsApp too (a fix, not a behaviour change anyone relies on). |
| Refund status shown to the family is simplified: `under_review`, `processing`, `needs_bank_details`, `refunded`, `declined`, `contact_support` (mapping in the module). | The internal state machine is not theirs. |
| `enrich_booking_with_details` nulls the three new fields; only the patient list and patient detail add them back. | Caregivers and admins must never read what a family was charged. |

## What's involved
| Path | Change |
|---|---|
| `app/services/patient_booking_cancel.py` (new) | terms view, cancel, replay, unpaid path, refund summaries, error type |
| `app/services/booking_cancellation.py` | `cancel_by_booker` closes open offers |
| `app/api/v1/endpoints/booking.py` | the two routes; patient list/detail attach the new fields (one query for all refunds of a list) |
| `app/schemas/booking.py` | `RefundSummary`, `CancelTermsResponse`, `CancelBookingRequest/Response`, three `BookingResponse` fields |
| `app/tests/test_patient_booking_cancel.py` (new) | 60 tests |
| `scripts/capture_patient_flow.py`, `docs/fe-captures/patient/`, the FE guide | live evidence |

## Verification
- **Tests** (`test_patient_booking_cancel.py`): terms per tier; **fee boundaries at exactly 12 h and 2 h** and one second either side; the app and the WhatsApp function store identical results; wrong state, other patient, missing, caregiver token, no token; replay and a retry with different numbers; `terms_changed`; unpaid free cancel creates no refund; unpaid-but-paid-at-Paystack; Paystack unreachable; amount mismatch; **never refund more than is left**; an existing open refund gets no second one; fields visible only to the patient; the visit code rules; **races with real concurrent sessions**: two or three simultaneous cancels (one fee, one refund, one caregiver notification), cancel vs caregiver accept, cancel vs start (never a refunded booking assigned or in progress), plus each order of both races run one at a time; the review fixes (fee stable after a settled refund and a policy edit, offers closed, reason not on a foreign refund).
- **Neighbour modules re-run**: `test_cancellation_tiers`, `test_whatsapp_my_bookings`, `test_refunds_create`, `test_caregiver_earnings`, `test_access_booking`, `test_response_shapes`, `test_whatsapp_booking_privacy`, `test_caregiver_cancel_window`: all pass.
- **Live**: 60 captures against the running API (Postgres, uvicorn from this checkout, fake Paystack): free, standard and late terms; cancel and replay; one refund row; the Finance queue shows it, approval shows `processing` to the family; visit code behaviour; error codes; unpaid cancel; the late-webhook case and the Paystack outage were **simulated** (labelled in the captures).
- **Opus review** (one round, read-only): 1 major (fee derived from a column that refund settlement overwrites) and 4 minors, all fixed in `cf6c854`. No re-review was run; the fixes are covered by the three new tests.
- **Fast checks**: `poetry check --lock`, `ruff check --select E9,F63,F7,F82 .`, `python -m scripts.ci_smoke`, all three `render_fe_guide --check`. The full suite is left to CI.
- **Not verified**: a live Paystack refund (the dispatch job was not run), delivery of the caregiver push (outbox row only), a real phone, a WhatsApp-channel booking cancelled through the app (unit-level only).

## Operate / roll back
Nothing to run or configure. Roll back by reverting the commits: the routes disappear (the app's old call returns 404 again), the three response fields vanish, and `cancel_by_booker` stops closing offers. Bookings already cancelled keep their stored fee tier, refund rows and the frozen `cancel_policy`, which is harmless.

## Follow-ups
- **Wallet destination on the admin side (needs an owner call):** the patient responses never offer the wallet, but a Super Admin can still approve one of these refunds *to the wallet* (RF-D4) or switch a failed one to it; the family would not see it in a hidden-wallet app. Restricting it is a one-line guard in `refunds._check_wallet_owner` plus fixtures in the refund tests; not done because it changes owner decision RF-D4.
- The family's cancel **reason** is only kept when a refund is queued (stored on the refund note); a free cancel of an unpaid booking drops it. Keeping it everywhere needs a `bookings.cancel_note` column (migration).
- `dispatch_state` stays `dispatching` on a booking cancelled while dispatching (documented as a trap in the guide); clearing it belongs with the dispatch code.
- `refunds._mark_succeeded` overwrites `bookings.refund_amount_kobo` with the last succeeded refund (found by the review); the patient views no longer depend on it, but admin and WhatsApp detail views still read it.
- Rescheduling does not exist (the app's `rescheduleBooking` has no route); cancel and re-book.
- Patient notification of refund progress belongs to the parallel P8-BE notifications slice.
