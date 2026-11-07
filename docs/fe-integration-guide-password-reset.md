# FE integration guide: password reset by emailed code

| | |
|---|---|
| **Audience** | `apps/patient` (new) and `apps/caregiver` (Reset and Recover screens) |
| **Base URL** | every path starts with `/api/v1` |
| **Source of truth** | Every body, status code and error string below was **copied from a real response** captured by `scripts/capture_password_reset.py` against the running API and a real Postgres. Files: [`docs/fe-captures/password-reset/`](fe-captures/password-reset/). Examples are rendered from them: `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-password-reset.md docs/fe-captures/password-reset` (`--check` fails if stale). Never edit an example by hand. |
| **The one fake** | The mail transport. The capture server writes outgoing emails to a folder instead of sending them, and the script reads the 5-digit code out of the email as a person would. Nothing else is faked. A real inbox was **not** exercised. |
| **Placeholders** | `example.com` emails, `<CODE>` for the real 5 digits, `<NEW_PASSWORD>` etc. for passwords. Real values appear in no file. |

## 1. What it is, in the app's terms

The person taps "Forgot password", types their email, and receives an email with a **5-digit code** (valid **15 minutes**, works **once**). They go to the next screen, type the email (pre-filled), the code and a new password, and are done. They then sign in normally with the new password. **No tokens come back from the reset**: the app sends the user to Login.

Flow: `POST /auth/password-recovery` -> email -> `POST /auth/reset-password` -> `POST /auth/login/mobile`.

## 2. Endpoints

### 2.1 Ask for a code: `POST /auth/password-recovery`
No auth. Body `{"email": "..."}`.

**Always `200` with the same body**, whatever the email is (registered, not registered, inactive, over its hourly limit, mail server down). The app cannot tell, and must not try: show "If that email has an account, we've sent a code" and move to the code screen.

Known address (an email was sent):

<!-- capture: 01-recovery-known-email.json response.body -->
```json
{
  "message": "Password recovery email sent"
}
```

Address with no account (nothing was sent, **identical reply**):

<!-- capture: 02-recovery-unknown-email.json response.body -->
```json
{
  "message": "Password recovery email sent"
}
```

What the mailbox held afterwards (verified live):

<!-- capture: 03-mailbox-after-two-requests.json observed -->
```json
{
  "observed": {
    "emails_for_known_address": 1,
    "emails_for_unknown_address": 0
  }
}
```

Rules the app should state or respect:

- **At most 3 emails per address per hour.** The 4th request still answers the same `200` but sends nothing (live capture below). So a "Resend code" button should be disabled for a while after use (suggest 60 seconds) and the screen should say "Check your spam folder" instead of inviting endless resends.
- **Asking for a new code makes the previous one useless.** Tell the user to use the newest email.
- The email may take a minute (it is sent just after the reply).

The 4th request in an hour (same reply, no 4th email):

<!-- capture: 33-throttle-request-4.json response.body -->
```json
{
  "message": "Password recovery email sent"
}
```

<!-- capture: 34-throttle-mailbox.json observed -->
```json
{
  "observed": {
    "emails_for_address_after_3_requests": 3,
    "emails_for_address_after_4th_request": 3
  }
}
```

### 2.2 Reset: `POST /auth/reset-password`
No auth. Body `{"email", "code", "new_password"}`.

Success, `200` (note the `Cache-Control: no-store` header). Request, then response:

<!-- capture: 11-reset-success.json request.body -->
```json
{
  "email": "pr-a-770225@example.com",
  "code": "<CODE>",
  "new_password": "<NEW_PASSWORD>"
}
```

<!-- capture: 11-reset-success.json response -->
```json
{
  "status_code": 200,
  "headers": {
    "content-type": "application/json",
    "cache-control": "no-store"
  },
  "body": {
    "message": "Password updated successfully"
  }
}
```

After success: the code is gone, **every other session of that account is signed out** (access and refresh tokens issued before the reset stop working), and the old password stops working. The app should clear its stored tokens and show Login.

An old access token after the reset:

<!-- capture: 13-me-with-old-access-token.json response.body -->
```json
{
  "detail": "Could not validate credentials"
}
```

An old refresh token after the reset:

<!-- capture: 14-refresh-with-old-refresh-token.json response.body -->
```json
{
  "detail": "Invalid refresh token"
}
```

Old password refused, new password accepted (login is unchanged by this feature; `login-new-password` returns the usual token pair):

<!-- capture: 15-login-old-password.json response.body -->
```json
{
  "detail": "Incorrect email/phone or password"
}
```

## 3. Errors the app must handle

For the code form the error is `detail = {"code", "message"}` (an **object**, unlike most of this API, where `detail` is a string). Switch on `detail.code`, show `detail.message`.

| Status | `detail.code` | When | What the app does |
|---|---|---|---|
| 400 | `invalid_or_expired_code` | Wrong, expired, already used, replaced by a newer code, malformed (not 5 digits), **or no such account**. One answer for all, on purpose. | "That code isn't right or has expired." Offer **Resend code**. |
| 400 | `weak_password` | New password fails the policy (8+ characters, at most 72 bytes, at least 5 different characters). **The code is not used up and the try is not counted**: let the user fix the password and resubmit. | Show `detail.message` under the password field. |
| 429 | `too_many_attempts` | 5 wrong codes for that address. The code is dead, **even the right one**, until a new one is requested. | "Too many tries. Request a new code." Show **Resend code**. |
| 429 | `too_many_attempts` (message "Please try again later.") with a `Retry-After` header (seconds) | 20 wrong codes in 24 hours across all codes of that address. Asking for new codes does not help, and none are sent, until the window ends. | "Too many tries. Try again later." Do not offer Resend. **Not captured live** (it needs four rounds of five wrong codes an hour apart); shape taken from the test `test_asking_for_fresh_codes_does_not_reset_the_daily_guess_budget`. |
| 422 | (list, FastAPI validation) | Missing field, or both forms sent. | A bug in the app, not a user error. |

A wrong code:

<!-- capture: 04-reset-wrong-code.json response.body -->
```json
{
  "detail": {
    "code": "invalid_or_expired_code",
    "message": "This code is incorrect or has expired. Request a new code."
  }
}
```

An email with no account gets **exactly the same** answer (do not try to tell them apart):

<!-- capture: 05-reset-unknown-email.json response.body -->
```json
{
  "detail": {
    "code": "invalid_or_expired_code",
    "message": "This code is incorrect or has expired. Request a new code."
  }
}
```

A malformed code (`"12ab"`) is the same 400 and counts as a wrong try, so validate "5 digits" in the app before sending:

<!-- capture: 06-reset-malformed-code.json response.body -->
```json
{
  "detail": {
    "code": "invalid_or_expired_code",
    "message": "This code is incorrect or has expired. Request a new code."
  }
}
```

A weak password with the right code (code still valid afterwards):

<!-- capture: 07-reset-weak-password.json response.body -->
```json
{
  "detail": {
    "code": "weak_password",
    "message": "Password must be at least 8 characters."
  }
}
```

The same code used a second time:

<!-- capture: 12-reset-replay.json response.body -->
```json
{
  "detail": {
    "code": "invalid_or_expired_code",
    "message": "This code is incorrect or has expired. Request a new code."
  }
}
```

The fifth wrong code (`lockout-wrong-5`) is still a `400`; the next call, **even with the right code**, is:

<!-- capture: 22-lockout-wrong-5.json response.body -->
```json
{
  "detail": {
    "code": "invalid_or_expired_code",
    "message": "This code is incorrect or has expired. Request a new code."
  }
}
```

<!-- capture: 23-lockout-right-code-is-refused.json response.body -->
```json
{
  "detail": {
    "code": "too_many_attempts",
    "message": "Too many incorrect attempts. Request a new code."
  }
}
```

Requesting a new code starts over: the reset with the new code returned `200` (`25-lockout-reset-with-new-code.json`, same body as the success above).

Validation errors (`422`). The server never echoes the code or the password back in `input`:

<!-- capture: 08-reset-missing-code.json response.body -->
```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": [
        "body"
      ],
      "msg": "Value error, email and code are required"
    }
  ]
}
```

<!-- capture: 10-reset-missing-password.json response.body -->
```json
{
  "detail": [
    {
      "type": "missing",
      "loc": [
        "body",
        "new_password"
      ],
      "msg": "Field required"
    }
  ]
}
```

## 4. Field traps

- `detail` is an **object** here for the code form and a **string** for the deprecated token form (section 6). The two never mix: send `{email, code, new_password}` and read objects.
- Send the **same email string** to both endpoints, exactly as typed (trimmed). Counters and the account lookup are per exact address: do not lowercase it or change it between screens. Pre-fill it on the reset screen from the recovery screen.
- The reset response has **no tokens** and no user. Do not look for them.
- A throttled or unknown-address recovery looks identical to success. There is no "email not found" state and no "email sent" confirmation to show beyond the generic wording.

## 5. UX consequences (instructions, not trivia)

1. **Every other device is signed out** by a successful reset. Say so on the success screen ("You've been signed out on other devices").
2. **Never tell the user whether the email exists.** Use the same wording after any `200` from recovery.
3. **Resend**: enable it after about 60 seconds; after 3 sends in an hour further sends do nothing silently, so show "Check your spam folder; if nothing arrives in a few minutes try again later".
4. **Counting tries**: after a `400 invalid_or_expired_code` the user has fewer tries left (5 per code). On `429 too_many_attempts` stop accepting input and show Resend (or "try later" when `Retry-After` is present).
5. Code field: numeric keypad, max length 5, `autoComplete="one-time-code"` / `textContentType="oneTimeCode"`, trim spaces. Password field: apply the same 8+ rule client-side, but rely on the server's `weak_password`.
6. The code is not in any link. Do not build a deep link that carries it.

## 6. Deprecated: the long-token form (shipped caregiver app only)

The email still carries, in small print, the old ~200-character token, and `POST /auth/reset-password {"token", "new_password"}` still works so the **already-shipped caregiver app does not break**. **New code must use the code form.** Switch-off: set `PASSWORD_RESET_LEGACY_TOKEN_ENABLED=false` once that app version is retired (the email then shows no token and the token form answers `Invalid token`). Differences, all verified live:

- Errors are plain strings: `400 {"detail": "Invalid token"}`, `400 {"detail": "Invalid or expired token"}` (used twice, or replaced by a newer request), and the weak-password sentence. The token form has no attempt counter (the token is unguessable).
- It now **also** applies the password policy and **also** signs out every other session.
- The old email's `oaklycare://reset-password?token=...` button is gone. The caregiver app's URL scheme is `oaklycarecaregivermobile`, so that link never opened it.

<!-- capture: 36-legacy-garbage-token.json response.body -->
```json
{
  "detail": "Invalid token"
}
```

<!-- capture: 37-legacy-weak-password.json response.body -->
```json
{
  "detail": "Password must be at least 8 characters."
}
```

<!-- capture: 38-legacy-success.json response.body -->
```json
{
  "message": "Password updated successfully"
}
```

<!-- capture: 39-legacy-replay.json response.body -->
```json
{
  "detail": "Invalid or expired token"
}
```

## 7. Verification table

| Behaviour | Status |
|---|---|
| Recovery: same `200` and body for known and unknown address | **verified live** (01, 02, 03) |
| Reset happy path; old password refused, new works | **verified live** (11, 15, 16) |
| Old access and refresh tokens die after a reset | **verified live** (13, 14) |
| Wrong code, unknown address, malformed code: same 400 body | **verified live** (04, 05, 06) |
| Weak password: 400 `weak_password`, code not consumed | **verified live** (07 then 11 succeeded with the same code) |
| Replay of a used code | **verified live** (12) |
| Five wrong codes, then even the right code refused (429); new code starts over | **verified live** (18 to 25) |
| A new request replaces the previous code | **verified live** (26 to 29) |
| 4th request in an hour: same reply, no 4th email | **verified live** (30 to 34) |
| 422 shapes | **verified live** (08, 09, 10) |
| Deprecated token form (errors, success, replay, weak password) | **verified live** (36 to 40) |
| 15-minute expiry | **tests only** (clock pinned: `test_a_code_expires_after_fifteen_minutes`, `..._just_before_it_expires`) |
| 24-hour guess budget, `Retry-After`, no email while locked | **tests only** (shape not captured live; see section 3) |
| Hourly window ending, counters idle for an hour | **tests only** |
| Concurrent double submit uses the code once; 10 concurrent wrong guesses give exactly 5 x 400 and the rest 429 | **tests only** (real threads against Postgres) |
| Code, password and token never in logs or responses | **tests only** (`test_no_code_password_or_token_ever_reaches_the_logs_or_a_response`) |
| The email arriving in a real inbox, its look on a phone, SMTP delivery | **not verified** (mail transport faked) |
| The apps' screens | **not verified** (not built against this yet) |
