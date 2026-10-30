# SOP: Hosted Dojah widget page (KD2)

| | |
|---|---|
| **What shipped** | A page the backend hosts, `GET /api/v1/kyc/widget/{token}`, that loads Dojah's web widget and sends the caregiver back to the app by deep link. `POST /api/v1/caregiver/identity-verification/sessions` gains one field, `widget_url`. The app opens that URL in the in-app browser (`expo-web-browser`) instead of embedding Dojah's native React Native SDK (owner decision 2026-10-30). |
| **Branch / commit** | `feat/kyc-widget-page`, cut from `oakly/main` `2d4f04d` (KD1 merged as backend#35). Not pushed; the lead opens the PR. |
| **Migration** | **None.** Alembic head stays `0019`. |
| **Verified against real Dojah?** **No, not the page.** The sandbox pass of KD1 used a harness page with no CSP and no `Permissions-Policy`. Here the page logic was run in a browser against a **stub** widget only (auto-open, success callback, redirect to the return link), and everything else by tests. The CSP therefore allows all of `*.dojah.io` rather than the narrower list in the brief. |

## Why
The owner will not use Dojah's native SDK in the app. The live sandbox pass showed that Dojah's web widget works on a real phone when loaded from a page we control, so the backend now serves that page. The page needs a credential that works without an auth header (an in-app browser cannot carry the app's bearer token), and it must expose nothing secret.

## How
| Area | Decision |
|---|---|
| Link | `widget_url = {KYC_WIDGET_PUBLIC_BASE_URL}/api/v1/kyc/widget/{token}`; an absolute origin the phone can reach, checked at boot (https only, and required, in production). A repeat `POST .../sessions` for a running attempt returns the same `reference_id` with a fresh token. |
| Token | `<expiry unix>.<attempt id>.<hex HMAC-SHA256>`. The handle is the attempt's own id (`identity_verifications.id`, which the app already has as `verification_id`), **not** the Dojah `OKL-` reference (review finding 1): a leaked or old link never reveals the reference the widget can be run under. No table is needed: the page looks the attempt up by primary key and prints the reference only inside a page it chose to serve. The brief said the payload would be the provider reference; this deviates on purpose. Key = `HMAC(SECRET_KEY, "oakly.kyc.widget.link.key.v1")`, so no new secret and JWT signing with `SECRET_KEY` can never produce a link (or the reverse); the MAC message carries its own purpose label. Constant-time compare; strict regex parse (ASCII digits only, no leading zero in the expiry so a token has one spelling, exact lengths); 30 minutes, and a token that claims more than 30 minutes of life is refused. |
| Who gets what | Malformed / forged / signed with another key / unknown attempt: the **same plain `404 "Not found"`** (text/plain, no detail). A correctly signed token whose life is over, or an attempt that is no longer open (`passed`, `failed`, `needs_review`, `expired`, `error`, and also `awaiting_result`): a `410` page reading "This verification link has finished. Return to the app." that never opens the widget. Only a valid MAC earns the 410, so a forger learns nothing from the difference. Opening the page writes nothing. |
| `awaiting_result` is "finished" | The app's `completed` hint moves an attempt there; a second widget run under one reference would be ambiguous and possibly billed twice (the KD1 follow-up). The page refuses it. |
| Page | One self-contained HTML document. Public values (app id, public key, widget id, the `OKL-` reference, the return link) sit in a `<script type="application/json">` block written with ASCII-only JSON that also escapes `<`, `>` and `&`, read by one nonce'd script with `JSON.parse`. Nothing else is interpolated; the return link is also an HTML-escaped `<a href>` fallback. No secret, no name, no token, no caregiver id in the page. It loads `https://widget.dojah.io/widget.js`, creates `Connect({type:'custom', config:{widget_id}, reference_id, ...})`, opens it automatically, keeps a "Start verification" button as the fallback, shows a status line, and on `onSuccess` / `onClose` / `onError` shows "Done. You can return to the app" and navigates to the return link after 600 ms. The callbacks are **not trusted for the outcome** and the page sends nothing anywhere (no fetch, XHR, beacon or socket). |
| Headers (every response of the route, including 404/410/503) | `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Permissions-Policy: camera=(self "https://identity.dojah.io"), microphone=(...), geolocation=(...)` (the `self` is required or Dojah's frame cannot use the camera: Weysure SOP 2026-10-09), and a CSP: `default-src 'none'`, `script-src 'nonce-<per response>' https://*.dojah.io`, `connect-src`/`frame-src`/`img-src`/`font-src`/`media-src`/`style-src`/`worker-src` limited to `*.dojah.io` (+ `data:`/`blob:` where media needs it; `style-src` has `'unsafe-inline'`), `base-uri`/`form-action`/`object-src` `'none'`, `frame-ancestors 'none'`. No analytics. |
| Return link | `KYC_RETURN_URL`, default `oaklycarecaregivermobile://kyc/return`. Read from config only (nothing in a request can change it: query, headers and Host are ignored), validated at boot: `<scheme>://<rest>`, scheme in an allow-list (`oaklycarecaregivermobile`, `oaklycaregiver`; plus `exp`/`exps` outside production), no whitespace, control characters, quotes, angle brackets or userinfo, at most 200 characters. |
| Logging | The route logs nothing. uvicorn's access log prints the path, so `app/core/log_filters.py` now blanks the token segment of `/api/v1/kyc/widget/...` (it already blanked the WhatsApp `hub.verify_token`). |

Rejected alternatives:
- **A DB table of link nonces:** more state for no gain; the token is short-lived, reference-bound and the page is idempotent and write-free.
- **JWT for the link:** the app's JWT code is built around users and token types; a separate three-field format with its own derived key is smaller and cannot be confused with an access token.
- **`'strict-dynamic'` or a narrower host list in the CSP:** not testable without a live widget run (follow-up).
- **Reporting the widget's result to the server from the page:** the callbacks are not evidence; the webhook, the pull and the cron already settle every attempt.

## What is involved
| Path | Change |
|---|---|
| `app/services/kyc_widget.py` (new) | Token issue / verify, derived key, security headers, page rendering |
| `app/api/v1/endpoints/kyc_widget.py` (new), `app/api/v1/api.py` | The route, mounted at `/kyc/widget`, `include_in_schema=False` |
| `app/services/kyc_caregiver.py`, `app/schemas/caregiver.py` | `widget_url` in the start response; an empty base URL is `503 PROVIDER_UNAVAILABLE` like an unconfigured widget |
| `app/core/config.py`, `.env.example` | `KYC_WIDGET_PUBLIC_BASE_URL` (added to the production boot guard), `KYC_RETURN_URL`, validators |
| `app/core/log_filters.py` | Token redaction in uvicorn's access line |
| `app/tests/test_kyc_widget.py` (new), `test_kyc_boot.py`, `test_kyc_caregiver_api.py`, `test_access_log_redaction.py`, `test_response_shapes.py` + golden, `fake_dojah.py` | Tests and fixtures |
| `docs/fe-integration-guide-caregiver.md`, `docs/fe-captures/caregiver/` | `widget_url` captured (203, 204); the page is described and marked not covered by captures; both `render_fe_guide --check` pass |

## Verification
- `poetry check --lock`, `ruff check --select E9,F63,F7,F82 .`, `python -m scripts.ci_smoke` (198 operations: the route is hidden), both `render_fe_guide --check`.
- Tests run: `test_kyc_*`, `test_dojah_client`, `test_response_shapes` (golden: only `identity_session.widget_url`), `test_money_kobo_and_config`, `test_trusted_proxies_warning`, `test_access_log_redaction`, `test_account_deletion`, `test_worker`: 877 passed after one fixed assertion (a stale key-set check). **The full suite was not run** (no shared code touched; CI is the first full run).
- `test_kyc_widget.py` (about 105 cases): token round trip, 30-minute edge, tampered reference / expiry / MAC, another key, derived key is not `SECRET_KEY`, an HMAC under `SECRET_KEY` itself does not verify, 15 garbage shapes, empty `SECRET_KEY`; start response format and fresh token on resume; page carries only the public values and none of the secrets (secret key, webhook secret, fingerprint key, `SECRET_KEY`, derived key, attempt ids), no person, no token; per-response nonce on every executable script and no inline handlers; hostile config values cannot break out; headers and CSP; headers also on the 404/410; closed-attempt and expired-token pages; unknown attempt is the same 404; nothing is written; the return link cannot be changed from a request; scheme allow-list; production boot guard; nothing logged.
- The page JS passes `node --check`, and in the browser pane against a stub widget it opened the widget on load and, on success, navigated to the return link. **Not run against Dojah's real widget.**

## Operate / roll back
- **Deploy:** set `KYC_WIDGET_PUBLIC_BASE_URL` to the public https origin of the API (production refuses to boot without it) and, if the app's scheme differs, `KYC_RETURN_URL`. Names only in chat and logs. In development use an https tunnel (a phone cannot reach `localhost`). The reverse proxy must forward `GET /api/v1/kyc/widget/*` without caching or rewriting headers (the CSP and `Permissions-Policy` are set by the app).
- **Mobile contract:** the app opens `widget_url` with `openAuthSessionAsync(widget_url, <return deep link>)`; the return link must equal `KYC_RETURN_URL`. Always call `POST .../sessions` right before opening (30-minute token). Send `client-event` hints as before and poll `GET` for the outcome.
- **Roll back:** revert the branch. The new field is additive and no data changed; an app that ignores `widget_url` is unaffected.

## Decision log
- **2026-10-30, build:** the default return scheme is `oaklycarecaregivermobile` (the caregiver app's real scheme, from `caregiver/apps/caregiver/app.json`), not the brief's `oaklycaregiver`; both are allowed. The app session must pass the same value to `openAuthSessionAsync`.

## Review (Opus, read-only: token scheme, output encoding, CSP, return link)
No critical or high finding. Applied: (1) **medium** the token carried the Dojah reference in clear, so any leaked or old link let someone run Dojah's widget under that reference for the attempt's whole 24 h (burning a try or making the pull ambiguous); the token now carries the attempt id instead (it is also not Oakly's opaque `reference`, which a test keeps out of every response); (2) **low** the expiry accepted leading zeros, giving one token several spellings; now rejected. Confirmed by the review: the derived link key cannot be confused with the app JWT (raw `SECRET_KEY`), the admin JWT or the contact-ref HMACs; the MAC is compared in constant time and the database is read only after it verifies; no request value reaches the page; the JSON block and the anchor are safely encoded; the return link has no request input and a fixed scheme allow-list; forged, malformed and unknown tokens give the same 404; the route writes nothing. Not applied, noted below: no SRI on `widget.js` (Dojah's file is mutable, and auth here is bearer-only so a script in this origin holds no cookie), no HSTS from the app (the edge sets it), `exp`/`exps` allowed in any non-production environment, two app schemes allowed. No second review round (no hole remained).

## Follow-ups
- **Edge proxy logs:** only uvicorn's access log is redacted. If nginx or another proxy logs request paths, redact `/api/v1/kyc/widget/` there too (the token is a 30-minute key to the page, and no longer names the Dojah reference). Confirm HSTS is set at the edge.
- Consider a staging-specific allow-list for the return scheme (today `exp`/`exps` are allowed whenever `ENVIRONMENT` is not `production`).
- **Run the page live against Dojah's real widget** with the CSP and `Permissions-Policy` on (the sandbox pass used a page with neither), then narrow the CSP from `*.dojah.io` to the hosts actually used and drop `'unsafe-inline'` for styles if the widget allows it.
- If K8 shows the widget needs a user gesture before the camera opens on iOS, the fallback button covers it; check that the automatic open is not blocked.
- `awaiting_result` makes a link finished: if the app sends `completed` when the caregiver merely dismisses the browser, a caregiver who wants to retry cannot until the attempt expires (24 h). Decide in K7b/K8 whether the app should send `completed` at all from the browser session, or whether the backend should mint a new attempt.
- Rate-limit the route per IP if the access log ever shows probing (the token is unguessable, 256 bits of MAC, so it is not urgent).
