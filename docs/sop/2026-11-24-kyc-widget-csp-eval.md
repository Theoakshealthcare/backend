# SOP: the hosted identity-check page hung on "Loading the verification widget..."

| | |
|---|---|
| **What shipped** | The hosted KYC page's content-security policy now allows `'unsafe-eval'` in `script-src` (for Dojah's script), and the page can no longer reload the script in an endless loop: at most two loads, then a visible error and a working "Start verification" button. |
| **Reported by** | The owner, on the caregiver app's identity step (2026-11-24): the system browser showed "Verify your identity / Loading the verification widget..." with a grey button, forever. |
| **Not verified** | A complete run with REAL Dojah credentials on a phone (needs the Dojah dashboard; the browser test below used placeholder credentials, so Dojah answered 400). Staging redeploys on merge. |

## Why
`app/services/kyc_widget.py` sends `script-src 'nonce-X' https://*.dojah.io`. Dojah's `https://widget.dojah.io/widget.js` (163 KB, obfuscated, bundles CryptoJS) evaluates strings, so the browser blocked it: *"Evaluating a string as JavaScript violates the following Content Security Policy directive because 'unsafe-eval' is not an allowed source of script"*. The script threw before it defined `window.Connect`. The page's loader then saw no `Connect`, called `load()` again, and repeated forever: a real-browser reproduction injected the script 600,000+ times. The page showed its first, static text ("Loading..." with the button disabled) and never an error. The code had flagged the policy as `[UNVERIFIED]` against a live widget since the sandbox pass used a page with no policy at all.

## How
| Decision | Reason |
|---|---|
| Add `'unsafe-eval'` to `script-src` only | The only way Dojah's script runs. `connect-src`, `frame-src`, `img-src` and the rest are unchanged; `'unsafe-inline'` is still absent (the page's own script is nonce-only). The page has no user content and loads one third-party script. |
| Bound the loader to two loads (`tries`), then show "Could not start the check. Check your connection and tap Start verification to try again." | A missing `Connect` must be an error message, never a loop. Tapping the button resets the counter. |
| Keep the allow-list at `https://*.dojah.io` | The browser run showed no further violation, but only with placeholder credentials; do not narrow it until a full real run. |
| Rejected: a CSP-less page, or `'unsafe-eval'` on every directive | Wider than needed. |

How it was found: the exact page and headers were served locally (`render_page` + `security_headers`) and opened in a real browser; the console showed the eval error. With the one change below the widget initialised and called Dojah (HTTP 400 for the placeholder credentials).

## What's involved
`app/services/kyc_widget.py` (`security_headers`, `_PROGRAM`, the comment on `_DOJAH`), `app/tests/test_kyc_widget.py` (the policy test no longer forbids `'unsafe-eval'`: it requires it in `script-src` only; a new test pins the retry cap).

## Verification
- Real-browser reproduction before and after (above). Checks of the changed functions without a database: `unsafe-eval` only in `script-src`, no `unsafe-inline`, no `*`, only dojah.io hosts, retry cap and error text present, exactly one loader.
- The KYC widget tests need Docker for their database; they run in CI.

## Operate / roll back
Revert the PR (the page then hangs again). The caregiver app needs no change.

## Follow-ups
- A full identity run on a phone with real Dojah credentials (sandbox widget id in the staging env).
- Narrow the CSP only after that run.
- The hosted page could also report a JS error to the user earlier (e.g. a 10-second watchdog on "Loading"): not added.
