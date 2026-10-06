# SOP: Admin bookings operations (A2, backend)

| | |
|---|---|
| **Date** | 2026-10-06 |
| **Branch** | `feat/admin-bookings-ops` (base `main` at `aade294`). Not pushed. |
| **Migration** | `0006_admin_bookings_ops` (down_revision `0005_whatsapp_booking`; re-parent at merge if another migration lands first) |
| **Plan / PRD** | [Admin console plan, A2](../plans/2026-10-06-admin-console.md), PRD AD-FR-19 to 27 |
| **FE guide** | [`docs/fe-integration-guide-admin.md`](../fe-integration-guide-admin.md) section 12, captures `079` to `118` in `docs/fe-captures/admin/` |
| **Status** | Verified locally: full suite, CI lint and smoke, migration round trip, live capture against the docker stack. Console (frontend) half of A2 is not built. |

## What shipped
Everything an operator needs to run bookings from the console, backend only:

| Endpoint | Permission | Purpose |
|---|---|---|
| `GET /admin/bookings` | `bookings.read` | List: status, payment, dispatch state, channel, city, service, caregiver, scheduled and created ranges, address-check, no-show, search (code, patient or caregiver name, phone in any format), sort, pagination |
| `GET /admin/bookings/unmatched` | `bookings.read` | Queue: held for address check, `unmatched`, or past `UNMATCHED_SLA_MINUTES` |
| `GET /admin/bookings/{id}` | `bookings.read` | Detail with price, payment, dispatch waves and offers, outbox events, timeline, `available_actions` |
| `GET .../candidates` | `bookings.assign` | Approved, available, skilled caregivers by match score |
| `POST .../redispatch` | `bookings.dispatch` | New wave; optional wider radius and re-offer to earlier caregivers |
| `POST .../assign` | `bookings.assign` | Offer to one caregiver (they still accept), or `force` (Super Admin) |
| `POST .../reassign` | `bookings.assign` | Move an accepted, unstarted visit to another approved caregiver |
| `POST .../cancel` | `bookings.cancel` | Reason code plus refund choice (full, partial, none) |
| `POST .../confirm-address` | `bookings.address` | Admin supplies lat/lng, clears the hold, starts dispatch |
| `POST .../no-show` | `bookings.dispatch` | Flags the caregiver as a no-show |
| `POST .../correct-status` | `bookings.correct_status` (Super Admin) | Forward-only status correction |

## Why
Until now an operator could not see a booking, find out why nobody accepted it, or fix it. Paid WhatsApp bookings without a map pin sat held with no way to release them (WA-FR-14), unmatched bookings were only a log line, and a refund-worthy cancellation had no path.

## How
- **One service** (`app/services/admin_bookings.py`), thin routes, typed Pydantic responses (`app/schemas/admin_booking.py`). No untyped dicts in responses.
- **Every action**: lock the booking row (`FOR UPDATE`, `populate_existing`), check the state **under the lock**, change it, write the audit row, commit once. The caregiver row is then taken `FOR SHARE`, the order `BookingService.handle_booking_action` uses, so an admin assignment and a caregiver accept serialise on the booking and exactly one wins. Admin actions never touch a conversation or quote, so conversation, quote, booking order holds trivially. Pushes go out after the commit and never affect the result; no HTTP under a lock.
- **Preconditions live in small `_why_not_*` functions** shared by the actions (which raise `409` with the message) and by `available_actions` (which only asks). The console shows exactly the buttons the backend will accept.
- **Assign is offer or force; reassign is its own route.** An earlier draft let `/assign` on an accepted booking silently reassign. The concurrency test caught it: an admin who meant "offer this pending booking" would, if a caregiver accepted first, overwrite that caregiver. Now `/assign` on anything but a pending, paid, unassigned booking is a `409`.
- **Offer** adds a `new_booking` notification for that caregiver and pushes them; the ordinary accept path settles it. **Force** and **reassign** copy what an accept does (caregiver, `accepted`, `dispatch_state=matched`, new visit code, other offers closed).
- **Cancel never moves money.** It sets `refund_required` and `refund_required_reason` (`admin_cancelled_full` or `admin_cancelled_partial:<kobo>`) for the A3 refund queue, and emits the existing `payment_refund_pending` event (a WhatsApp booker is told). Unanswered offers are closed (`booking_cancelled`) because the caregiver app lists unanswered offers as new requests.
- **Confirm address** marks a paid booking `dispatching` in the same transaction (as settlement does), then calls `start_dispatch` after commit; if that fails the tick sends wave 1. Coordinates must be inside a rough box around Nigeria, to catch swapped values.
- **Clinical access.** Condition (plain, or Fernet-decrypted from `condition_encrypted`) and special instructions are returned only with `clinical.read`; each read that reveals something writes a `booking.clinical_view` audit row naming the fields, never the content. Without the permission the response says only that something exists. Visit notes are never included (count only). Name and phone are shortened without `people.pii`; address and coordinates are hidden without `bookings.address`.
- **No-show.** Auto-suspicion (`no_show_suspected`) is computed on read: accepted or en route, not started, `NO_SHOW_GRACE_MINUTES` (15) past the start. The admin flag is a real column pair, the one reason for a migration.
- **Rejected alternatives.** Storing the flag only as an audit row (not filterable). A separate `refund_amount_kobo` column (a migration for A3 to own; the amount rides in the reason code for now). Adding new `booking_events` types (the table has a CHECK constraint and W3 is changing the notification set in parallel). Widening skills on re-dispatch (radius only).

## What's involved
| Path | Change |
|---|---|
| `app/api/v1/endpoints/admin_bookings.py`, `app/api/v1/api.py` | routes, mounted at `/admin/bookings` |
| `app/services/admin_bookings.py` | reads, masking, timeline, all actions |
| `app/schemas/admin_booking.py` | request and response models |
| `app/services/dispatch.py` | `find_candidates` and `_send_wave` take `radius_km` and `renotify`; public aliases `lock_booking`, `send_wave`, `push_offers` |
| `app/services/booking.py` | `smart_caregiver_matching(radius_km=...)` |
| `app/db/models/booking.py`, `app/alembic/versions/0006_admin_bookings_ops.py` | `no_show_flagged_at`, `no_show_flagged_by` (FK `admin_users`) |
| `app/core/config.py` | `UNMATCHED_SLA_MINUTES=30`, `NO_SHOW_GRACE_MINUTES=15` |
| `app/tests/test_admin_bookings.py`, `app/tests/test_admin_rbac.py` | 53 new tests; 11 routes added to the role matrix |
| `scripts/capture_admin_flow.py` | new `bookings` section and `--section bookings` |

## Verification
- Full suite green (1554 tests after the review fixes), including the route by role matrix (every new route against all seven roles), each action's effects and audit row, the clinical masking and audit tests, and two race tests with real threads: admin force-assign vs caregiver accept (3 rounds, exactly one winner, final state matches the winner) and two admins cancelling at once (one succeeds, one audit row, one event).
- CI reproduced locally: `poetry check --lock`, `ruff check --select E9,F63,F7,F82`, `scripts.ci_smoke` (126 operations), both `render_fe_guide --check`.
- `alembic upgrade head`, `downgrade -1`, `upgrade head` round trip on a fresh Postgres.
- Live: `docker compose -p oakly-a2` stack, 40 real requests captured (`079` to `118`), all with the expected status, then `down -v`.

## Review (Opus, 2026-10-06)
Locks, masking and money paths came back clean. Two fixes, both with tests: (1) the informational notices to a caregiver (reassigned, cancelled) had no response, so the caregiver app listed the booking under "new requests"; they are now written with `response="info"`; (2) search by name or phone is limited to `people.pii` holders, so a masked role cannot confirm identities by searching.

## Operate / roll back
- Deploy: the migration runs with the API container (`alembic upgrade head`). New settings have defaults; set `UNMATCHED_SLA_MINUTES` and `NO_SHOW_GRACE_MINUTES` only to change them (A4 will make them audited config).
- Roll back: `alembic downgrade -1` drops the two no-show columns; the routes are additive. A booking cancelled by an admin stays cancelled; its `refund_required` flag is for finance to clear.

## Follow-ups
- **Console half of A2** (screens for the above).
- **Patient is not told about an admin cancel with no refund**, and nobody is told when a visit is reassigned: `booking_events` has no event for them. Add them when W3's notification set lands (WhatsApp templates are needed).
- **Refund amount for a partial refund** is encoded in `refund_required_reason`; A3 should add a real column and backfill from that string.
- **Earnings credit and the feedback request** should hook onto `correct_status` to `completed` when A3 builds them.
- **Widening skills** on re-dispatch, and the widened radius lasts one wave only (the tick uses the default after).
- **Contact patient** (AD-FR-21) needs the support thread (A5 / support inbox). Support's `bookings.cancel_request` has no route yet.
- **Live map** (AD-FR-27) is v1.1.
- `payment.attempts` reads wallet transactions linked to the booking; booking payments made through Paystack directly have none, so it is usually `[]`.
