# Refunds v2 (A3 slice 3): design

| | |
|---|---|
| **Date** | 2026-10-21 (decisions taken with the owner 2026-10-19; playback approved 2026-10-21 "proceed") |
| **Branch** | `feat/refunds-v2` (worktree `wt-refunds-v2`), base `oakly/main` `623a66e` |
| **PRD** | Admin console PRD §7.6, AD-FR-32 to 36 (AD-FR-37 partial refunds pulled forward: the cancel tiers already need them) |
| **Ledger** | `LEDGER-refunds-v2.md` (worktree root) |
| **Status** | Design. Nothing built. |

## 1. Intent

Today a cancelled paid booking is only *flagged* (`bookings.refund_required`, `refund_amount_kobo`); nothing pays the family back, the ₦50,000 Finance limit is stored but never checked, and the only working refund code is the legacy wallet flow (`patient_refund_requests`). Slice 3 makes "every paid booking is either served or refunded" (PRD G2) true: every money-back case becomes a **refund** row in one queue, a **person always approves it**, it pays out **exactly once** through **exactly one destination**, its status comes from Paystack's word, and the caregiver side is handled by explicit rules.

**Goals**
- One refund model and queue for every booking refund, with suggested amounts from the cancel policy.
- Approval limits; one destination per refund; Paystack refund execution with webhooks and reconciliation; failure and needs-attention handling.
- Caregiver take-back after a completed visit, under the payout invariant.
- Close the app-channel caregiver-cancel gap.
- The admin API contract the console's refunds screens will need (screens come later, in an `apps/admin` slice).

**Non-goals (this slice)**
- Families requesting refunds themselves (they go through support). *(my call, approved in playback)*
- Console screens. Patient-app UI changes.
- Webhook-exceptions queue (AD-FR-29), four-eyes approvals (AD-FR-86), CSV exports.
- Refunding wallet deposits (see §9: the legacy routes are retired; deposit refunds are a follow-up if ever needed).

**Anti-goals (a failure even if it "works")**
- Any path that pays a family twice, or more than they paid, for one booking.
- Any automatic step that raises what a caregiver can be paid, or asks a caregiver to send money back.
- A refund that moves money without a named human approver.
- A status shown to the family or Finance that we guessed instead of hearing from Paystack.

## 2. Owner decisions

| ID | Decision |
|---|---|
| **RF-D1** | A family cancellation refunds **total paid − tier fee**. Oakly absorbs Paystack's processing fee in every case; the fee Paystack charged is recorded on the booking so Finance can see what refunds cost. |
| **RF-D2** | No-fault cancellations (caregiver drop-out, no caregiver found, Oakly/admin cancel) refund **every naira**. |
| **RF-D3** | Finance approves refunds up to `finance_refund_limit_naira` (₦50,000, the existing setting; the PRD's ₦100,000 is superseded). Above it a **Super Admin approves alone**. |
| **RF-D4** | Destination is the **original payment method** (Paystack refund) by default. A **Super Admin** may choose **Oakly wallet credit** instead, app users only. Exactly one destination per refund. |
| **RF-D5** | Paystack `failed` → the refund returns to the queue as `failed` with Paystack's reason; Finance retries, or a Super Admin switches it to wallet credit. `needs-attention` → Finance enters the family's bank account in the console. The family is told when the refund starts, completes, and when bank details are needed. |
| **RF-D6** | For a refund of a **completed** visit, the approver chooses per refund whether to **take back the caregiver's share** (with a reason). Taken from the wallet first; whatever is already paid out is recorded as owed and recovered from future earnings; the caregiver is never asked to repay. |
| **RF-D7** | The caregiver **always keeps** `late_cancel_compensation`, even when the late fee is later refunded. (Closes checklist item "void/adjust late_cancel_compensation" as won't-do.) |
| **RF-D8** | **No refund is automatic.** Every refund, no-fault ones included, waits in the queue for Finance (or a Super Admin above the limit). A caregiver cancel of a paid **app** booking now queues a full refund. |
| **RF-D9** *(my call, approved in playback)* | The approver may change the amount before approving, never above what is still refundable. |
| **RF-D10** *(my call, approved in playback)* | The legacy refund routes are retired (no data to migrate: greenfield). |
| **RF-D11** (owner, 2026-10-21) | A **staff-raised** refund must be approved by a **different** person than the one who raised it (another Finance person, or a Super Admin). `403` "You raised this refund, so someone else must approve it." System-created refunds (from a cancel) have no requester and any eligible approver may approve. |
| **RF-D12** *(my call, from the builder check)* | The ₦50,000 limit is **cumulative per booking** (this refund plus the booking's earlier approved / in-flight / succeeded refunds), so a large refund cannot be split under the limit. |

## 3. Constraints

- Money is integer kobo; naira only at the API edge (`core/money`). Rounding half-up `(x*pct+50)//100`, as everywhere.
- Lock order is the existing one: **booking → refund → ledger rows → wallet**. Paystack is never called while a lock is held (as in `payouts.py` and the legacy two-phase refund).
- The payout invariant (`services/payouts.py` docstring) holds: automatic code may only lower what a caregiver can be paid; a balance never goes below 0 (`ck_caregiver_wallets_balance_not_negative`); what cannot be taken is **recorded**, not forced.
- Paystack refunds have **no idempotency key**. A sent refund must never be resent on silence or timeout; an unknown outcome is reconciled from Paystack's records.
- Paystack's refund webhook carries `transaction_reference` and may have `refund_reference: null` (sample payload in the Paystack docs), so matching cannot rely on a refund id alone.
- Tests run against `FakePaystack` (`tests/fake_meta.py`), extended with refund endpoints. Real Paystack is not exercised (labelled in the SOP and FE guide).
- Migration number: `0014_refunds_v2`, `down_revision` = the finance-audit migration `0013` (PR #18, not merged on 2026-10-21). The plan says how the branch carries it until #18 merges.
- **How bookings are paid (checked in code, 2026-10-21):** a booking is `paid` only through `settle_booking_payment` on a Paystack charge of its own reference `bookings.paystack_reference` (`BKG_<code>_<patient>`, `services/booking.py:208`). A late payment after expiry or cancellation uses the **same** reference, and a second charge on a paid booking is refused (`ALREADY_PAID`). So every refundable booking has exactly one Paystack transaction, and `original_method` always means a Paystack refund of that reference. The patient `POST /wallet/payment` debits the patient wallet with a `booking_id` but never marks the booking paid; it cannot produce a refundable booking (an oddity recorded as a follow-up, not handled here).

## 4. The model

### 4.1 `refunds` (new)

| Column | Notes |
|---|---|
| `id` | UUID |
| `booking_id` | FK, not null |
| `patient_id` / booker reference | who gets the money (for wallet credit and notifications) |
| `reason_code` | `cancelled_by_booker`, `caregiver_cancelled`, `no_caregiver_found`, `admin_cancelled`, `paid_after_expiry`, `paid_after_cancellation`, `complaint`, `other` |
| `origin` | `system` (a cancel path created it) or `staff` (raised by hand) |
| `requested_by_admin_id` | null for `system` |
| `suggested_amount_kobo` | what the policy says (§5), frozen at creation |
| `amount_kobo` | what will be / was refunded; set at approval (defaults to suggested) |
| `destination` | `original_method` or `wallet`, null until approved |
| `status` | §4.3 |
| `approved_by_admin_id`, `approved_at`, `rejected_reason` | |
| `take_back_from_caregiver` | bool, decided at approval (only offered for a completed visit) |
| `take_back_kobo` | computed at approval (§7), 0 when not taken back |
| `take_back_reason` | required when `take_back_from_caregiver` |
| `failure_reason` | Paystack's text on the current attempt |
| `processed_at` | money confirmed out (Paystack `processed`, or wallet credit) |
| `note`, `created_at`, `updated_at` | |

Constraints: `amount_kobo > 0`; `take_back_kobo >= 0`; status CHECK; **partial unique index: one open refund per booking** (`status` in `pending_approval`, `approved`, `processing`, `needs_attention`, `needs_review`). A booking can have several refunds over time (e.g. a fee refunded after the main refund), but never two open at once. This is also what makes webhook matching by `transaction_reference` unambiguous.

### 4.2 `refund_attempts` (new; mirrors `payout_attempts`)

One row per call to Paystack: `refund_id`, `attempt` (1, 2, …), `paystack_refund_id` (from the create response or the webhook), `status` (`sending`, `pending`, `processing`, `needs_attention`, `processed`, `failed`, `unknown`), `failure_reason`, `bank_details_submitted_at`, `created_at`. A retry never overwrites history. Only the **current** (latest) attempt can settle the refund.

### 4.3 Refund states

```
pending_approval ──approve──▶ approved ──send job──▶ processing ──refund.processed──▶ succeeded
       │                        │ (destination wallet: credited at approval ─────────▶ succeeded)
       ├──reject──▶ rejected    │                     ├──refund.needs-attention──▶ needs_attention ──bank details──▶ processing
       │                        │                     └──refund.failed──▶ failed ──retry──▶ approved
       │                        │                                          └──switch to wallet (SA)──▶ succeeded
       │                        │                                          └──close (reason)──▶ closed
       └──(booking fully refunded elsewhere) n/a: prevented by the open-refund index and the refundable check
```

- `succeeded`, `rejected`, `closed` are terminal. `closed` is a failed refund an admin gives up on (the money stays with Oakly; it can be re-requested as a new refund).
- **Contradictions** (Paystack disagrees with what we recorded) put the refund in **`needs_review`** and log an error; nothing else moves automatically: a `processed` for a non-current attempt, a `processed` after we recorded `failed` and switched to wallet, a `processed` amount different from ours, or reconcile finding two Paystack refunds for one attempt. `needs_review` is resolved only by a Super Admin (`resolve` with a note; §8), after checking the Paystack dashboard:
  - `resolve succeeded`: the refund becomes `succeeded` with every success effect (booking `payment_status`, take-back, family notified "complete"), exactly once (guarded by the from-state).
  - `resolve failed`: the refund becomes `failed`, so retry, switch to wallet or close follow as usual (each re-checks Paystack first, §6.5).
  - The reconcile "no record" case (§6.4) lands in `needs_review` and uses the same resolve. This is the refunds analogue of the payout freeze: a possible double payment to the family is surfaced, never "fixed" by guessing.

### 4.4 Booking fields

- `bookings.paystack_fee_kobo` (nullable): Paystack's `fees` from `charge.success` / `verify`, recorded at settlement (RF-D1 reporting). Null for bookings settled before this slice.
- `payment_status` gains **`partially_refunded`** (enum value). Set when a refund succeeds and less than the total has been refunded in sum; `refunded` when the sum reaches the total. `refund_required` is cleared when a refund for it is created (the flag becomes the queue row); the old flag columns stay for the WhatsApp copy that reads them.
- **Refundable amount** = `total_amount_kobo − Σ amount_kobo of refunds in (approved, processing, needs_attention, succeeded, needs_review)`. Every create and approve checks `amount ≤ refundable` under the booking lock.

### 4.5 Caregiver side

- New ledger type `refund_take_back` on `caregiver_transactions` (debit, `successful`), with `booking_id`, the refund id in `status_reason`, and the amount actually debited from the wallet.
- New `caregiver_wallets.owed_kobo` (`>= 0`): what a take-back could not debit. `earnings.release_due` takes from `owed_kobo` first when it releases an earning (the caregiver's balance only grows by what is left); each recovery writes a `refund_take_back` row for the recovered part. *(This is the "recover from future earnings" of RF-D6.)*

## 5. Suggested amounts (when a refund is created)

| Case | Suggested amount |
|---|---|
| Family cancel, free tier | total paid |
| Family cancel, standard (12–2 h) | total − 20% of the service fee |
| Family cancel, late (<2 h / en route) | total − 50% of the service fee |
| Caregiver drop-out, no caregiver found, admin cancel "full", paid after expiry/cancellation | total paid |
| Admin cancel "partial" | the amount the admin entered (existing `refund_amount_kobo`) |
| WhatsApp drop-out started-visit case (today `refund_amount_kobo` NULL = "a person decides") | null suggestion: the approver must enter the amount |
| Staff-raised (complaint, other) | the amount the requester entered |

The Paystack fee is never deducted (RF-D1/D2). The tier fee is the booking's frozen `cancel_policy` result already computed by `terms()` (`booking_cancellation.py`), never recomputed from today's policy.

## 6. Flows

### 6.1 Creation
- `flag_refund` (`booking_cancellation.py:198`) keeps its signature and now also **creates the refund row** (`origin=system`, `pending_approval`) in the same transaction, unless an open refund exists (then it does nothing new, idempotent) or the booking is not paid.
- **Gap fix (RF-D8):** `_after_caregiver_cancel` (`services/booking.py:446`) flags a full refund for a **paid app booking** cancelled by its caregiver (today it returns early for non-WhatsApp). WhatsApp behaviour (re-dispatch first) is unchanged.
- Staff: `POST /admin/refunds` (`refunds.request`) for complaints and other cases.

### 6.2 Approval (`POST /admin/refunds/{id}/approve`, `refunds.approve`)
Under the booking lock, then the refund lock:
1. Status must be `pending_approval`; amount (default suggested; required if suggested is null) `> 0` and `≤ refundable`.
2. **Limit (per booking, cumulative)** *(my call)*: if `amount + Σ refunds of this booking already approved/succeeded/in flight > finance_refund_limit_naira`, the caller needs the new permission **`refunds.approve_above_limit`** (Super Admin only). Else `403` with a message naming the limit.
3. **Destination:** `wallet` needs `refunds.approve_above_limit` too (Super Admin, RF-D4) and an app-user booker with a patient wallet; else `422`.
4. **Take-back:** accepted only for a `completed` booking; reason required; computed per §7.
4a. **Requester ≠ approver** for a staff-raised refund: see §2 RF-D11.
5. `original_method` → status `approved` (the send job sends it). `wallet` → credit the patient wallet under its lock, write a `PatientTransaction` of type `refund` (linked to the refund), status `succeeded`, `processed_at` now, take-back applied, family notified.
6. Audit row (`refund.approve`) with amount, destination, take-back, and the limit in force.

### 6.3 Sending (ARQ job `refund_dispatch_job`, every minute, like `payout_dispatch_job`)
Claim with `SKIP LOCKED` an `approved` refund, create attempt N (`sending`), **commit**, then call `POST /refund {transaction: booking.paystack_reference, amount}` with no lock held.
- 2xx: store `paystack_refund_id`, attempt and refund `processing`; notify the family "being processed".
- A definite 4xx refusal: attempt `failed`, refund `failed` with Paystack's message.
- Timeout / 5xx / unreadable: attempt `unknown`, refund stays `processing`. **Never resent.** Reconcile decides.

### 6.4 Settling (webhook `refund.*` and reconcile)
- Webhook `refund.pending|processing|needs-attention|processed|failed` (signature-checked by the existing endpoint). Match: by `paystack_refund_id` if present, else by `transaction_reference` → the booking's **one open refund** → its current attempt; the amount must match or it is a contradiction (§4.3). Transitions are from-state guarded, so repeats are no-ops.
  - `processed` → `succeeded`, `processed_at`, booking `payment_status`, take-back applied, family notified "complete".
  - `needs-attention` → `needs_attention`, family notified "waiting for your bank details".
  - `failed` → `failed` with reason (Paystack has returned the money to our balance).
- Reconcile job (`refund_reconcile_job`, every 15 min): for refunds `processing` or attempts `unknown` older than 10 minutes, ask Paystack (fetch by id, or list refunds for the transaction) and apply the same transitions. Paystack having **no record** 30 minutes after an `unknown` send → the attempt becomes `failed` only on a definite answer; "no record" alone sets the refund to `needs_review` (we cannot prove it was not sent).

### 6.5 Failure handling
- `POST /{id}/retry` (`refunds.approve`): only from `failed`; first **fetches the current attempt from Paystack** and proceeds only if Paystack says `failed` (a `processed` answer settles it instead and refuses the retry); then status `approved` (the job sends attempt N+1).
- `POST /{id}/bank-details` (`refunds.approve`): only from `needs_attention`; `{bank_code, account_number}`; resolves the account name via Paystack `/bank/resolve` (existing client), calls `POST /refund/retry_with_customer_details/{paystack_refund_id}`, sets `processing`. The account number is **not stored**; only the bank code, last 4 digits and resolved name go in the attempt and the audit row.
- `POST /{id}/switch-to-wallet` (`refunds.approve_above_limit`): only from `failed` after the same Paystack check as retry; credits the wallet as in §6.2 step 5.
- `POST /{id}/close` (`refunds.approve`, reason): only from `failed`.
- **Paystack unreachable during these checks** *(my call)*: the action is refused with `503` and a plain message ("We couldn't reach Paystack to confirm the last attempt failed. Try again shortly."); the refund stays `failed`. Never proceed on silence.

## 7. Caregiver take-back (RF-D6, RF-D7)

- Offered only when the booking is `completed` and its `earning` row (type `earning`, the visit share) exists. `earning.amount` is the visit share only; `late_cancel_compensation` is a separate row and is never touched (RF-D7).
- Amount *(my call)*: proportional, the caregiver's share of what is refunded, including for a goodwill partial refund (the approver's yes/no is the place to decide whether the caregiver is at fault), `take_back_kobo = round_half_up(earning.amount × refund.amount_kobo / booking.total_amount_kobo)`, capped at `earning.amount` minus earlier take-backs for that booking. The approver sees the number before confirming; it is not editable (one rule, no free-form debits).
- **Applied when the money has actually left Oakly** (`succeeded`), not at approval, so a refund that fails and is closed takes nothing. Under the wallet lock: debit `min(balance, take_back)` as a `refund_take_back` row; add the rest to `owed_kobo`. If the earning is still `pending_clearance` / `held`, the same rule applies: it will be released into `owed_kobo` first, so the effect is the same and no earning row is edited.
- Never credits. Never freezes payouts (this is not a Paystack contradiction). A payout already reserved or sent is not touched: its money is the caregiver's; the shortfall goes to `owed_kobo`.
- The caregiver's wallet API (`GET /caregiver/wallet`) gains nothing in this slice *(my call)*; the take-back row appears in their transaction history with a plain label. Showing `owed_kobo` in the app is a follow-up.

## 8. Admin API (contract for the console)

All under `/api/v1/admin/refunds`, admin bearer auth, every write audited (`refund.*`), naira at the edge.

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/admin/refunds` | `refunds.request` or `refunds.approve` or `payments.read` | Queue: filter `status` (default open), `reason_code`, `booking_id`, `above_limit`; paged like `/admin/payouts` |
| GET | `/admin/refunds/{id}` | same | Detail: booking context (status, channel, cancel tier and fee, caregiver assigned/arrived/completed, total, Paystack fee, already refunded, refundable), suggested amount and why, attempts, take-back preview, `needs_super_admin`, `available_actions` for the caller |
| POST | `/admin/refunds` | `refunds.request` | Staff-raised refund `{booking_id, amount_naira, reason_code, note}` |
| POST | `/admin/refunds/{id}/approve` | `refunds.approve` (+ `refunds.approve_above_limit` above the limit or for wallet) | `{amount_naira?, destination?, take_back_from_caregiver?, take_back_reason?, note?}` |
| POST | `/admin/refunds/{id}/reject` | `refunds.approve` | `{reason}` |
| POST | `/admin/refunds/{id}/retry` | `refunds.approve` | from `failed` |
| POST | `/admin/refunds/{id}/bank-details` | `refunds.approve` | `{bank_code, account_number}` from `needs_attention` |
| POST | `/admin/refunds/{id}/switch-to-wallet` | `refunds.approve_above_limit` | from `failed` |
| POST | `/admin/refunds/{id}/close` | `refunds.approve` | `{reason}` from `failed` |
| POST | `/admin/refunds/{id}/resolve` | `refunds.approve_above_limit` | `{outcome: succeeded|failed, note}` from `needs_review` |

`available_actions` mirrors the A2 bookings pattern so the console never re-derives permissions. Errors use the project's existing `{detail: ...}` shape with plain messages; the FE guide lists them from live captures.

RBAC: new permission `refunds.approve_above_limit` granted to Super Admin only (`core/admin_permissions.py`); Finance keeps `refunds.request` + `refunds.approve`; Ops/Support keep `refunds.request`.

## 9. Legacy

- `GET /patient/admin/refund/requests`, `PUT /patient/admin/refund/requests/{id}` and the patient `POST /wallet/refund/request` answer **`410`** with a plain message (as the payouts slice did for withdraw). The legacy tables stay (unused) until a cleanup migration.
- The legacy `refund.processed|failed` webhook handling (`patient_wallet.process_refund_webhook`) is replaced by the new handler; a refund event matching no v2 refund is logged and ignored (200).

## 10. Notifications (RF-D5)

Through the existing `NotificationService` / `booking_events` pipeline and the approved WhatsApp template `refund_update` ("Your refund of {{1}} for booking {{2}} is {{3}}"), with `{{3}}` = "being processed" (sent to Paystack, or approved to wallet), "complete", "waiting for your bank details. Our team will contact you". Keyed per refund and status so repeats send once. **App-channel bookers**: the pipeline has no app delivery for these events today; the refund status is visible in the booking (`payment_status`); an app push/email is a recorded follow-up. *(my call)*

## 11. Verification required

- TDD throughout; tests against `FakePaystack` extended with `POST /refund`, `GET /refund/{id}`, `GET /refund?transaction=`, `POST /refund/retry_with_customer_details/{id}` and a `settle_refund(...)` webhook helper.
- Money invariants asserted after every refund test: **Σ refunded (succeeded + in flight) ≤ total paid** per booking; caregiver: paid + available + reserved + owed-adjusted never exceeds earned; balance ≥ 0.
- A seeded random-sequence test (like the payouts one) over create/approve/send/webhook (any status, any attempt, duplicates, out of order)/reconcile/retry/switch/close/take-back.
- Races: double approve, approve vs webhook, two send workers, webhook vs reconcile, take-back vs payout batch vs release.
- Opus review of every money path (approval, send, settle, take-back, wallet credit), 2+ rounds expected.
- Live captures for the admin FE guide on the shared compose stack with the fake Paystack (labelled), `render_fe_guide --check` passing.

## 12. Left to the builder

Exact column types and index names; job schedules within the stated cadence; error message wording (plain, user-facing); how the fake models Paystack's refund payloads (from the documented sample); test file layout.
