# SOP: Account deletion as anonymisation

| | |
|---|---|
| **Date** | 2026-10-07 |
| **Branch** | `fix/account-deletion-anonymise` (base `main` at `1daa634`) |
| **Migration** | `0009_account_erasure` (down_revision `0007_whatsapp_notifications`). **Re-parent onto `0008_dropout_fee_tiers` when that branch merges (superseded: the drop-out branch is now `0010_dropout_fee_tiers` on top of this one)** (change `down_revision` in `app/alembic/versions/0009_account_erasure.py`) |
| **Status** | Verified locally: full suite green, CI steps reproduced, FE guide re-captured on the Docker stack |

## What shipped
`DELETE /api/v1/auth/profile` is now an erasure request handled as anonymisation. It used to hard-delete the profile and user rows and returned 500 for anyone with history.

## Why
Almost no foreign key to `users`, `caregivers` or `patients` cascades (bookings, caregiver wallet and transactions, consents, support tickets, WhatsApp contacts, vitals, prescriptions, care relationships), so the delete raised `IntegrityError` for every user with any history. Hard-deleting payments, consents and audit rows would also be wrong: the NDPA allows retaining records needed for legal obligations, and payments and consents need it. The tests that reproduce the bug (16 red before the fix) are in `app/tests/test_account_deletion.py`.

## How
One transaction, in `AccountErasureService.erase` (`app/services/account_erasure.py`):

| Step | What happens |
|---|---|
| Lock | User row `FOR UPDATE`; caregiver wallet row `FOR UPDATE` while checking, so a withdrawal cannot slip in between the balance check and the wipe |
| Refuse (409, `detail` says what to do) | Caregiver: a booking `accepted`/`en_route`/`in_progress` or paid and `pending`; wallet balance above 0; a pending withdrawal. Patient: a paid booking `pending`/`accepted`/`en_route`/`in_progress` (own or booked by them); wallet balance above 0 |
| Wipe user | name `Deleted user`, email `deleted+<uuid>@example.com`, phone null, profile image null, password replaced with an unusable hash, OTP and reset tokens cleared, `is_active=false` |
| Wipe profile | Caregiver: name, email, phone, DOB, gender, state, address, emergency contact (3), ID and face verification results and status, Onfido ids; documents and certifications blanked; locations and bank account deleted; transaction payout details masked (`******1234`, account name null). Patient: the same personal fields plus blood group, allergies, conditions, medications, special requirements; family members anonymised; `is_active=false` |
| Other | Push tokens deleted; the person's own `self` care recipient anonymised |
| WhatsApp | Contact(s) go through `withdrawal.stop_data` (consents withdrawn, drafts closed, opted out, `data_withdrawal` compliance ticket), then are unlinked (`user_id` null, name null, `wa_id` and `phone_e164` replaced) and the conversation is set to `declined` with an empty context |
| Record | Three append-only `consents` rows (`terms`, `health_processing`, `marketing`), `decision=withdrawn`, `channel=account_erasure`, `subject_user_id` and `granted_by_user_id` = the user, `created_at` = when |
| After commit | The Cloudinary profile image is deleted outside the transaction; a failure is logged and never fails the request |

Kept as they are: bookings, wallet, transaction amounts and references, consents, support tickets, vitals, prescriptions, medical records, audit rows.

Decisions and rejected alternatives:
- **Email placeholder is `@example.com`, not `@invalid`.** `EmailStr` response models reject `@invalid` (and every `.invalid` domain), so any endpoint returning an erased user would 500. `example.com` is reserved (RFC 2606), so nothing is delivered, and it validates.
- **No `deleted_at` column, so no migration.** `is_active=false` already makes `get_current_user` answer 403 `Inactive user`, login 400/401 and refresh 400. Another open branch adds `0008`; avoiding a migration avoids a re-parenting step at merge. A column can be added later if reporting needs it.
- **Erasure recorded as consent rows**, not an audit-log row: `admin_audit_log.admin_user_id` is NOT NULL (it records admin actions), and the `consents.type` check would need a migration for a new `erasure` type. Withdrawing all three types under `channel=account_erasure` carries the same facts (who, when).
- **STOP DATA logic is shared, not copied.** The data effects moved from `processor._apply` into `app/channels/whatsapp/withdrawal.py` (`withdraw_consents`, `opt_out`, `stop_data`); the processor calls them, so chat and erasure cannot drift. Chat behaviour is unchanged (the existing WhatsApp tests pass untouched).
- WhatsApp placeholder phone is `+999` plus 11 digits: 999 is an unassigned country code and the `phone_e164` check requires an E.164 shape.
- Patient wallet balance also blocks (not in the original brief): erasing would otherwise forfeit the person's money. Message sends them to support for a refund.

## What's involved
- `app/services/account_erasure.py` (new), `app/channels/whatsapp/withdrawal.py` (new), `app/channels/whatsapp/processor.py` (calls the shared helpers).
- `app/api/v1/endpoints/auth.py` (`delete_account`, `response_model=DetailResponse`), `app/schemas/user.py` (`DetailResponse`).
- `app/tests/test_account_deletion.py` (17 tests).
- `scripts/capture_caregiver_flow.py` (new steps at the end, so earlier capture numbers did not move), `docs/fe-captures/caregiver/`, `docs/fe-integration-guide-caregiver.md` (section 3, "Delete account").

## Verification
| Check | Result |
|---|---|
| Baseline before the fix | 16 of the 17 new tests red (500s); rest of the suite green |
| New tests | 17 pass: fresh caregiver; caregiver with completed history (records kept, PII gone, bank account removed, transactions masked); 409 for accepted/en_route/in_progress, positive balance, pending withdrawal; patient 409 with a paid active booking and 200 with history plus an unpaid booking; old access token refused (403) and refresh refused (400), login fails (401), repeat DELETE is 403 not 500; the same email registers again; push tokens gone; Cloudinary called after commit and a failure is harmless; WhatsApp contact anonymised like STOP DATA; self care recipient anonymised |
| Full suite and CI steps | 1749 passed (1732 before plus the 17 new; the WhatsApp tests pass unchanged after the processor refactor). `poetry check --lock`, `ruff --select E9,F63,F7,F82`, `scripts.ci_smoke`, both `render_fe_guide --check` |
| Live | Captured on the Docker stack (compose project `oakly-del`): 409s for caregiver and patient, 200, 403/400/401 afterwards, re-register 200 |

## Operate / roll back
Run `alembic upgrade head` (round-trip tested). Roll back by reverting the commits; `alembic downgrade` backfills erased bookings (address `[erased]`, coordinates 0) and restores NOT NULL on the three booking location columns. Roll back by reverting the commits. Rows already erased stay erased (that is the point); there is no un-delete. To find them: `SELECT id FROM users WHERE email LIKE 'deleted+%@example.com'`.

## Follow-ups
- Files are not purged from storage: caregiver document files and provider-side ID checks keep their objects; only the database references are blanked. Add a purge job.
- Medical records and care recipients created for other people are kept; set the retention period with counsel.
- The caregiver and patient apps need the delete screen and to show the 409 `detail`.
- No concurrency test for withdraw-vs-erase; the wallet row lock covers it by construction.

## Privacy review fixes (same day)
| # | Issue | Fix |
|---|---|---|
| 1 | Unpaid bookings stayed open; a late payment dispatched a caregiver to an erased patient | The patient's open bookings are locked `FOR UPDATE`; paid ones refuse (409), unpaid ones are cancelled with `cancel_reason='account_erased'`. A payment arriving later takes the existing `paid_after_cancellation` refund path, no dispatch |
| 2 | An erased caregiver could re-enter matching (open socket recreating locations; matcher ignored `User.is_active`) | `_wipe_caregiver` sets `profile_status='suspended'`, `suspended_at` (so the matcher and accept both refuse); `find_nearby_caregivers` joins `User.is_active`; the endpoint closes the open socket after commit (`websocket_manager.close_caregiver`, via `anyio.from_thread`); the ping handler ignores and closes for an inactive user (covers other workers) |
| 3 | `PatientCaregiverAccess` rows still let caregivers read and write the erased patient's vitals | `revoked_at` set on all of the patient's open grants |
| 4 | Money in flight into an erased wallet | 409 for a `pending` `PatientTransaction` or a `pending`/`processing` `PatientRefundRequest`, checked under the wallet lock |
| 5 | Cloudinary `False` return ignored; file reference already gone | Checked; a warning logs the user id only (never the URL), and the `public_id` is queued in `pending_media_purges` (new table) for a purge job |
| 6 | Retained personal data | See the retention table |
| 7 | Caregiver booking check unlocked | Bookings `FOR UPDATE`, then the caregiver row `FOR UPDATE`, then the bookings are checked again, in accept's order (booking before caregiver), so an accept in flight finishes first. Threaded test: an accept that holds the locks makes the erase wait and then return 409 |
| 8 | Unlinked WhatsApp contact with the same number | Phones (user, caregiver, patient; normalised, captured before the wipe) also match contacts with `user_id` null by `phone_e164` or `wa_id`; they go through the same STOP DATA path and anonymisation |

### Retention table
| Data | Action | Reason |
|---|---|---|
| Finished or cancelled bookings of an erased patient: address, coordinates, special instructions, `condition`, `condition_encrypted` | **Wiped** (null; the three location columns became nullable in `0009`, and the typed booking responses accept null) | Where the person lives and what they said about their health serve no obligation once the visit is over |
| Same bookings: amounts, references, status, city and state, timestamps | Kept | Payment and tax records |
| Chat messages the erased user sent | `message` = `[deleted]`, `attachment_url` null | Free text from the person; the row stays so the other party's thread still reads in order |
| Chat messages the other party sent | Kept | Their data |
| `BookingNote` (caregiver clinical notes) | Kept | Health record of the care given, owned by the caregiver's professional duty |
| Vitals, `FallGuardAssessment`, prescriptions, medical records | Kept | Health records; access by caregivers is revoked (item 3) |
| Caregiver `background_check_evidence`, `CaregiverDocument.onfido_document_id` | Kept | Proof the background check happened (compliance obligation); the extracted ID data is wiped |
| `suspension_reason`, `rejection_reason`, `changes_requested`, approval fields | Kept (an erased caregiver gets `suspension_reason='account_erased'` only if none existed) | Admin records about the account |
| Consents, support tickets, transactions, wallet | Kept | Legal and audit retention (see above) |

### Verification (fixes)
18 new tests (35 in `test_account_deletion.py`), including the late-webhook refund, matcher, socket close, ping from an inactive user, access revoked, pending money, purge queue, retention wipes and the threaded accept race. The migration round-trips (`upgrade head`, `downgrade -1`, `upgrade head`) on a throwaway Postgres. Full suite: 1767 passed. CI steps (lock check, bug-class ruff, ci_smoke, both guide checks) clean. The caregiver FE guide did not change (no caregiver response or message changed), so no recapture.

### Follow-up found (not fixed here)
`Booking.condition` is stored in **plaintext** for app-channel bookings (`BookingCreate.condition` goes straight into `Booking(condition=...)` in `app/services/booking.py:203`, and is returned in booking responses), while WhatsApp bookings store `condition_encrypted` (Fernet, `booking_chat.py:443`). Health text at rest should be encrypted for both channels; this needs a data migration and a response change, so it is a separate piece of work.

## Re-review fixes
| # | Issue | Fix |
|---|---|---|
| 1 | An abandoned pending deposit (no webhook ever) blocked deletion forever | `settle_pending_deposits` runs before `erase` takes any lock (no HTTP under a lock) and calls Paystack `verify_transaction` for each pending deposit: paid is credited (the balance check then refuses); abandoned, failed or reversed is marked failed; a deposit older than 24 h that is not paid is marked failed; one genuinely in progress (`ongoing`, `pending`, `processing`, `queued`, under 24 h) or one Paystack cannot answer for (unreachable, error) stays pending and the erasure is refused with 409. An unreachable Paystack is never treated as abandoned, even for an old deposit, because it could hide a paid one |
| 2 | An admin could reinstate an erased caregiver and assign them | `reinstate` returns 409 "the account was deleted by its owner" when the user is inactive or `suspension_reason='account_erased'`; `_lock_caregiver` (assign, reassign, force) returns 409 "Caregiver account is closed" for an inactive user |
| 3 | Admin live tracking 500 on an erased patient's finished booking | 404 `No tracking data available` when the coordinates are null |
| 4 | `initiate_deposit` and `request_refund` took no wallet lock | Both lock the wallet row first (`_lock_wallet_for_write`, same row erasure locks) and refuse with a ValueError if the account was closed meanwhile. Erasure creates an empty wallet if there is none, so there is always one row to serialise on. Threaded test |
| 5 | `0009` downgrade would fail on NULLs | Backfills before restoring NOT NULL; migration test round-trips with an erased booking |

Full suite: 1777 passed. CI steps clean.

Known gap: a Paystack charge that succeeds after its deposit was marked failed here would still be credited by the existing webhook (`process_successful_payment` does not require `pending`). That is the pre-existing webhook behaviour and now lands in an erased patient's wallet; closing it needs a webhook change (ignore deposits for inactive patients and flag them for refund).
