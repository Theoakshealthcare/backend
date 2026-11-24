# SOP: the caregiver's Chats list, `GET /caregiver/chats`

| | |
|---|---|
| **What shipped** | One read-only endpoint, `GET /api/v1/caregiver/chats`: the signed-in caregiver's conversations, one row per booking whose chat they may open, newest activity first, with the patient's name, service, booking status, visit start and the last message cut to 140 characters. Cursor paged. No `unread_count` (see Why). |
| **Branch / PR** | `feat/caregiver-chats-list`, cut from `oakly/develop` `d380f0a`. PR: see the branch on `Theoakshealthcare/backend`. Not merged (merging waits for the owner's "merge #N"). |
| **Migration** | None. |
| **FE guide** | [`docs/fe-integration-guide-caregiver.md`](../fe-integration-guide-caregiver.md), "Chats list" (section 7), rendered from captures `250`-`256`. |

## Why
The caregiver app's Figma has a **Chats** tab (a list of conversations with the last message and time). The backend only had the per-booking routes (`GET /booking/chat/{booking_id}/messages`, `POST /booking/chat/{booking_id}/send`), so there was no way to build the list without calling every booking's chat. This adds the list; the messages themselves stay on the old routes.

## How
| Decision | Why / alternative rejected |
|---|---|
| **Listed = the chat routes' own access rule.** A booking is listed when `bookings.caregiver_id` is the caregiver, in any status. | That is the only check `send_chat_message` / `get_chat_messages` make, so every listed row opens and nothing that would 403 is listed. A status filter (live visits only) was rejected: it would hide a finished visit's thread the route still serves. |
| **Patient name is always the full name.** | The identity rule for caregivers is "the offer view hides the patient, the assigned caregiver sees them" (`redact_offer`, `enrich_booking_with_details` in `booking.py`). Chat access *is* assignment, so a listed booking is always one where the full name is allowed; an offered-only booking is not listed at all (pinned by a test). There is no first-name-and-initial branch in the backend: that form exists only where the app draws an offer, which carries no name. |
| **No `unread_count`.** | `chat_messages.is_read` / `read_at` exist, but no route or service ever sets them (`GET .../messages` does not mark anything read), so a count would be the number of all messages, wrong from day one. Left out rather than half-working; a follow-up needs a "mark read" write first. |
| **Last message in one statement.** | A `DISTINCT ON (booking_id)` subquery (restricted to this caregiver's bookings) joined to the bookings: one query per page however many conversations. `left(message, 141)` is selected, so a huge message is never loaded; 141 tells "exactly 140" from "cut". A test counts the statements. |
| **Preview = 140 characters including the `…`.** | A cut message ends with `…` inside the 140, so `text` is never longer than 140. Only the newest message is read; `attachment_url`, `message_type` and earlier messages never leave the database here. |
| **Order: last message time, else booking creation time.** | A conversation nobody has written in sorts by when the booking was created, not by the visit date (a visit next week would otherwise sit above live conversations). Side effect: a newly assigned booking with no message sits near the top. |
| **Paging like the neighbours.** | `limit` (1-50, default 20) plus the `before` / `before_id` keyset used by `GET /caregiver/care-plans/{id}/observations`, here over (activity time, booking id) so ties on the same time never skip or repeat a row. A row-value comparison in SQL. |
| **Only the columns the row needs.** | The query selects five booking columns and the patient's name, never the booking object: no address, phone, condition or money can reach the response by accident. `Cache-Control: no-store`. |
| **No avatar / photo field.** | The list draws initials; a photo URL is more patient data to expose for a list row. The chat header already shows the photo from the booking details. |

## What's involved
| Path | Change |
|---|---|
| `app/services/caregiver_chats.py` (new) | `list_chats`, `_preview`, `DEFAULT_LIMIT` / `MAX_LIMIT` / `PREVIEW_CHARS` |
| `app/schemas/caregiver_chat.py` (new) | `CaregiverChatList`, `CaregiverChatOut`, `ChatLastMessage` |
| `app/api/v1/endpoints/caregiver_chats.py` (new), `app/api/v1/api.py` | the route, mounted under `/caregiver`; 403 for non-caregivers via `get_current_caregiver` |
| `app/tests/test_caregiver_chats.py` (new, 17 tests) | auth, empty, shape, preview cut, no leaks, only mine, offered not listed, statuses, opens via the chat route, ordering, paging with ties, limit, one statement per page |
| `app/tests/test_capture_caregiver_chats.py` (new), `docs/fe-captures/caregiver/25x-*.json` | live captures (skipped unless `CAPTURE_CAREGIVER_CHATS=1`) |
| `docs/fe-integration-guide-caregiver.md`, `docs/checklist/MASTER_CHECKLIST.md` | docs |

## Verification
- `poetry run pytest app/tests/test_caregiver_chats.py` (17 pass) plus the capture run against the test harness (real app, real Postgres).
- Neighbours run by name: `test_caregiver_home.py`, `test_caregiver_client_profile.py`.
- Fast checks as in CI: `ruff check --select E9,F63,F7,F82 .`, `scripts.ci_smoke` with CI's dummy env, the eight `render_fe_guide --check` pairs. The full suite is CI's.
- **Not verified:** a phone; a very large conversation table (see follow-ups).

## Operate / roll back
Nothing to configure and no migration. Revert the commit: the route disappears and the app's Chats tab shows its error state.

## Follow-ups
- **Unread counts and badges** need read tracking first: a "mark read" write on opening the chat (and `is_read` set for the other party's messages). Then add `unread_count` to the row.
- **Index:** `chat_messages` has no index on `booking_id` (baseline migration). The list's subquery is restricted to the caregiver's bookings, so it is fine at today's size; add `(booking_id, created_at)` before chat volume grows (a migration, deliberately not mixed into this PR).
- **Captures:** `25x` come from the test harness, like the client profile and care plans. Fold them into `scripts/capture_caregiver_flow.py` the next time that script is run (it rewrites the whole captures directory from a compose stack).
- Not built (no data source): the "Oakly Admin" support conversation, a patient photo, online/typing presence.
