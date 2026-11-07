# SOP: `PUT /auth/profile` now saves name and phone (DP1-33)

| | |
|---|---|
| **What shipped** | `PUT /api/v1/auth/profile` persists `full_name` and `phone_number`, validates them, clears the phone and the photo on an explicit `null`, and refuses an email change, a password and a typed-in photo URL with a stable `code`. |
| **Branch / commits** | `fix/profile-vitals-validation` (cut from `oakly/main`), commit `fix(auth): PUT /auth/profile now saves ...`. Not pushed; the lead opens the PR. |
| **Migration** | None. |
| **FE guide** | [`docs/fe-integration-guide-patient-profile-vitals.md`](../fe-integration-guide-patient-profile-vitals.md) section 3, rendered from captures 100 to 115. |

## Why
The patient-app live capture (wiring B) saw `PUT /auth/profile` answer `200` and keep none of the name, phone, email or photo (only `address`, `gender`, `date_of_birth` and the emergency fields were saved); blank values were silently ignored too. The caregiver device pass (DP1-33) saw the same.

**Root cause:** `auth.py` imports `get_db` from `app.db.session`, but `get_current_user` opens its session from `app.api.deps.get_db`. They are two different dependency callables, so FastAPI gives the endpoint a **second session**. The handler set fields on the `current_user` object (session A) and committed the endpoint's `db` (session B): nothing was written. The address and emergency fields worked only because they are loaded through `db` (session B). The `password` line had the same defect, so it was silently dropped as well.

## How
| Decision | Why / alternative rejected |
|---|---|
| New `app/services/profile.py::ProfileService.update_account_fields`, which **loads the user in the committing session** (`db.get(User, id)`). The endpoint changed by one `try` block, and the address/emergency code is untouched. | The handler change is small and separate so the conflict with PR #42 (reset-by-code, same handler) stays trivial. Rejected: re-pointing the whole app to one `get_db` (wide blast radius for a bug found in one handler; follow-up below). |
| Validation lives in `UserProfileUpdate` (`field_validator`s using `PydanticCustomError`, so `detail[0].msg` is a clean sentence): name trimmed, non-empty, at most 120; phone 7 to 15 ASCII digits with an optional `+` (spaces, `-`, `.`, brackets removed); photo URL well-formed (`http(s)`, at most 2048). | Both apps show the first `msg`. |
| **A photo URL cannot be set by `PUT`** (`400 photo_upload_only`); `null` clears it and re-sending the stored value is a no-op. **Deviation from the brief ("a normal URL")**, found by the Opus review: `DELETE /auth/profile` and `POST /auth/profile/upload-image` delete the stored URL's file at Cloudinary, and the id extraction trusts any URL containing `cloudinary.com/.../upload/`; booking responses show other people's photo URLs, so a typed-in URL let anyone get another person's photo destroyed. | Photos stay upload-only. Rejected alternative: pin to our cloud name (still allows another user's file). |
| `model_fields_set` separates "left out" from "`null`": `null` clears the phone and photo, `""` is a `422`, a name can never be cleared. | The owner asked for an explicit `null`; blank-means-clear would make an emptied text box erase data. |
| **Name and phone are copied to the patient/caregiver row** (not only caregivers). | `/auth/me` shows `patient.full_name` / `patient.phone_number` and the patient app reads them; the caregiver row is DP1-33. |
| **A different email is refused**: `400` `{"code": "email_change_not_supported"}`, nothing in the request is saved. Same email (any case) and `null` are no-ops. | The email is the sign-in identity and there is no verified change flow yet. Rejected: silently accepting (account takeover risk) or dropping (the original bug). |
| **A password in the body is refused**: `400` `use_change_password`. A blank one is ignored as before. | Not in the brief; **necessary**: once the session bug is fixed, the old line would set a password with no current-password check and without bumping `token_version` (a stolen access token could then lock the owner out). `POST /auth/change-password` is the safe route and no client sends a password here. |
| A phone that another account uses **in any spelling** (`+234…`, `234…`, `0…`, via `core.phone.phone_variants`): `409` `phone_number_taken` (checked first; an `IntegrityError` on the phone constraint is also caught, other constraints re-raise). `users.phone_number` is unique and is also a sign-in. WhatsApp matching and erasure treat the spellings as one number, so a second account must not take another spelling. | Review finding. |
| `phone_number: null` on an account **with no email** (a WhatsApp phone-first user) is `400 phone_number_required`. | It would leave no sign-in identifier. |
| Separators are stripped but the country code is not normalised. | Login matches the stored string exactly (`UserService.get_user_by_email_or_phone`); normalising `0803…` to `+234803…` would change what people can sign in with. Follow-up. |

## What's involved
| Path | Change |
|---|---|
| `app/services/profile.py` (new) | `ProfileService`, `ProfileError` and its three subclasses |
| `app/schemas/user.py` | validators on `UserProfileUpdate` |
| `app/api/v1/endpoints/auth.py` | `update_profile` calls the service (about 8 lines changed) |
| `app/tests/test_profile_update.py` (new) | 36 tests |
| `app/tests/test_capture_profile_vitals.py`, `docs/fe-captures/patient-profile-vitals/` | live captures |

## Verification
- **Red first:** the new tests failed on the unchanged code (full_name and phone not saved, blank/implausible values accepted with `200`).
- `test_profile_update.py`: patient save and `/auth/me` agreement (user and patient copy), caregiver sync on both rows, fields left out untouched, separators removed, blank/`null` name, implausible and blank phone, phone taken (and nothing saved), own phone not a conflict, clearing phone/photo on both rows, bad photo URLs, email change refused with nothing else saved, same email and `null` email no-ops, address and emergency side effects for both roles with blanks still ignored, password refused (unchanged hash), blank password ignored, no token.
- Neighbours re-run: `test_change_password`, `test_account_deletion`, `test_access_auth`, `test_response_shapes`, `test_caregiver_client_profile`, `test_capture_client_profile`, `test_whatsapp_phone_only_patient`.
- **Live:** captures 100 to 115 against the real app and Postgres (test harness), rendered into the FE guide.

- **Opus review** (one round, read-only; its 6 findings): fixed the Cloudinary blocker (photo upload-only), the phone spelling collision and non-ASCII digits, the phone-only lockout, and the constraint check on `IntegrityError`; not fixed here, listed below: phone-change OTP, single commit, suspension, `recorded_by_user_id`. The vitals access control (acting for someone else, leaks, races, validation breakage) came back clean.

## Operate / roll back
Nothing to configure. Revert the commit: the endpoint returns to dropping the account fields. The phone numbers saved meanwhile stay (harmless).

## Follow-ups
- **Verified email change flow** (send a code to the new address, change on confirmation, bump `token_version`); until then the email is read-only.
- **Two `get_db` functions** (`app.api.deps` and `app.db.session`): any other handler that mutates `current_user` and commits its own `db` has the same bug. A grep of `current_user.<field> =` found only this handler, but the duplicate should be removed.
- Normalise phone numbers to one form (and migrate stored ones) so `0803…` and `+234803…` are the same account; today the stored string is the login.
- Clearing the photo with `null` leaves the old image on Cloudinary (the upload route deletes the old one; this does not).
- **A phone change needs a one-time code** (review, major): today anyone can claim a number nobody has registered, which can block its real owner from registering or from the WhatsApp link (WA-FR-3 would make it an account-takeover path). Needs an SMS/WhatsApp OTP flow; also store one spelling (migration).
- `update_profile` still commits twice (account fields, then the patient/caregiver fields), so a failure in the second leaves the first saved; one commit would touch the address/emergency blocks PR #42 also edits.
- `cloudinary_service.get_public_id_from_url` trusts any URL containing `cloudinary.com`; harden it to our own cloud name as defence in depth.
- Clearing `address`, `gender` and the emergency fields is still impossible (`""` and `null` are ignored).
- PR #42 (`feature/password-reset-code`) edits this handler (it clears a pending reset when email or password is sent); expect a small conflict in `update_profile`. With email and password now refused, that clearing is dead code and can go.
