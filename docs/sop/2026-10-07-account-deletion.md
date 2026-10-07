# SOP: Account deletion as anonymisation

| | |
|---|---|
| **Date** | 2026-10-07 |
| **Branch** | `fix/account-deletion-anonymise` (base `main` at `1daa634`) |
| **Migration** | None (alembic head stays `0007_whatsapp_notifications`) |
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
No migration and no config. Roll back by reverting the commits. Rows already erased stay erased (that is the point); there is no un-delete. To find them: `SELECT id FROM users WHERE email LIKE 'deleted+%@example.com'`.

## Follow-ups
- Files are not purged from storage: caregiver document files and provider-side ID checks keep their objects; only the database references are blanked. Add a purge job.
- Medical records and care recipients created for other people are kept; set the retention period with counsel.
- The caregiver and patient apps need the delete screen and to show the 409 `detail`.
- No concurrency test for withdraw-vs-erase; the wallet row lock covers it by construction.
