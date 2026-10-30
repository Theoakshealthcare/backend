# Caregiver KYC (identity verification) with Prembly: design and slice plan

> **Provider is Dojah as of 2026-10-30.** The owner dropped Prembly (unresponsive) and chose Dojah. This document is kept as written: the architecture, the trust rules (section 16.1), the decision rules (section 7), the states, the data model and the slice order all stand. Everything that names Prembly (sections 3, 9.3 config names, 15 questions, the K0 spike) describes the original plan and is superseded by [2026-10-30-caregiver-kyc-dojah-delta.md](2026-10-30-caregiver-kyc-dojah-delta.md) and the KD1 SOP ([docs/sop/2026-10-30-kyc-dojah-adapter.md](../sop/2026-10-30-kyc-dojah-adapter.md)). The main changes: server-minted `reference_id` instead of the e-mail/session-hint binding, hex HMAC signature with the webhook secret, no idempotency header, `Completed` is not a pass, liveness is a boolean.

| | |
|---|---|
| **Date** | 2026-10-27 |
| **Branch** | `docs/kyc-design` (worktree `wt-kyc-design`), base `oakly/main` `94f45b1`, Alembic head `0014_refunds_v2` |
| **Status** | Design. **Nothing is built.** Docs only. |
| **Decisions** | Owner, 2026-10-27 ("yes to the defaults", provider = Prembly). They are final and are *not* re-asked here (section 2). |
| **Prembly docs read** | 2026-10-27, from the public pages listed in section 3.7. Anything the public docs do not confirm is labelled **UNCONFIRMED** and collected in section 15. |
| **Related** | `HANDOFF-master.md` sections 2, 2a, 5, 6, 7; `docs/checklist/MASTER_CHECKLIST.md` (new section 14); `docs/sop/2026-10-05-caregiver-approval.md` |

---

## 1. Intent

A caregiver enters a patient's home. Before an admin can approve them, Oakly must have independent proof that the person on the account is the person on a government record. Today that proof is a human ticking "verified" in the admin console: nothing in the system checks a face or an ID. The half-built pieces (a client-posted result endpoint, a dormant Onfido service) were never wired to a screen.

This design makes the **provider's signed result**, not the phone and not a person's memory, the evidence. Admins still approve every caregiver and can still override, but they do it looking at a provider result instead of a blank card.

**Goals**
- Prembly verifies NIN/BVN/ID data and matches a live selfie to the registry photo, with liveness.
- The result reaches Oakly only over a server-to-server channel (signed webhook, confirmed by an authenticated pull). The mobile client can never set a verified state.
- Every non-pass result lands in the **existing** admin review queue. Nothing is auto-rejected.
- Oakly stores the provider's reference and a small result summary. **No ID image, no selfie, no NIN record, no raw webhook body.**
- Retries, expiry, provider outage, name/DOB mismatch and duplicate identities are handled as explicit states, not accidents.
- Small PRs (owner feedback: big slices took too long). See section 14.

**Non-goals (v1)**
- Re-verification on a schedule (annual refresh), ID-expiry tracking, PEP/AML screening add-on (Prembly sells one; not enabled).
- Verifying patients, family members or admins.
- The background check (already mandatory by founder decision; stays a manual admin decision, `decide_background_check`).
- Replacing the licence/certification document review (`CaregiverDocument`, `decide_document`); that is a separate human check.
- Any automated *rejection* or *suspension* from a provider result.

**Anti-goals (a failure even if it "works")**
- Any code path where the client's word moves `identity_verification_status` to `verified`.
- A forged or replayed webhook that verifies a caregiver.
- An image, NIN, BVN, address or next-of-kin record from Prembly sitting in our database or logs.
- A caregiver locked out of onboarding because Prembly is down or its wallet is empty.

---

## 2. Decisions taken (do not re-ask)

| ID | Decision | Source |
|---|---|---|
| **K-D1** | Provider = **Prembly** (NIN/BVN/ID + face match, liveness). `app/services/onfido.py` is replaced by a Prembly service. | Owner 2026-10-27 |
| **K-D2** | KYC is **mandatory before approval**; the admin manual override (`identity-decision`) stays. | Handoff 2a Q2 |
| **K-D3** | Failed or unclear results go to the **existing admin review queue**; never auto-reject. | Handoff 2a Q3 |
| **K-D4** | Store only the **provider's reference and result**, not ID or face images. | Handoff 2a Q4 |
| **K-D5** | Stop trusting the client: `POST /caregiver/identity-verification/result` goes away; the provider's **signed webhook** is the source of truth. | Handoff 2a Q5 |
| **K-D6** | The background check is already mandatory (founder decision); unchanged. | Handoff 2 |

Design calls I made inside those decisions (each is flagged where used, and the owner may overrule them):

| ID | Call | Why |
|---|---|---|
| **K-C1** | The signed webhook is the **trigger and the record**; before a caregiver is marked `verified`, the server also **pulls the session from Prembly with the secret key** and requires the two to agree. | Prembly's docs say the webhook HMAC key is the *public* key (section 3.3), which ships inside the app. A signature made with a key anyone can extract proves little. The pull uses the secret key. If Prembly confirms a separate secret signing key, the pull can be relaxed to a reconcile-only job (section 8.4). |
| **K-C2** | A keyed hash (HMAC) of the NIN/BVN is stored to detect one identity on two accounts. | Duplicate-identity detection was asked for; it needs a stable identifier. This is a derived value, not the number; it is one more thing than "reference and result", so it is called out for the owner (section 10.3). Removable without touching the rest. |
| **K-C3** | KYC gates the *attempt*, not the *outcome*: a caregiver may continue to consent and submission when the result is `needs_review`, or when Prembly is unavailable ("continue without for now"). Approval stays blocked until `verified` (already enforced). | Otherwise a provider outage or a slow review blocks all onboarding. |
| **K-C4** | `needs_review` is a new value of `identity_verification_status`; `failed` stays admin-only. | `decide_identity("failed")` auto-suspends an approved caregiver (`caregiver_approval.py:273`); a machine must never reach that. |

---

## 3. What Prembly's public docs say (read 2026-10-27)

I could not log in to a Prembly account, so everything below is from public pages. **UNCONFIRMED** means the public docs do not settle it.

### 3.1 Products we would use

| Capability | Endpoint / component | Facts from the docs |
|---|---|---|
| **Widget / SDK** (hosted capture UI, returns a session) | RN package `prembly-reactnative-kyc` (the install line says `prembly-react-native-kyc`: the docs disagree), component `PremblyIdentityWidget` (props `isVisible`, `widgetKey`, `widgetId`, `email`, `firstName`, `lastName`, `phone`, `metadata`, `callback`) or `IdentityKyc` (`configId`, `widgetKey`, `metadata{user_id,email,metadata{compare_image}}`, `onComplete`, `onError`, `buttonText`). Needs `react-native-webview`; Android 23+ needs a runtime camera permission. Workflow ("widget") is configured in the dashboard (SDK Setup, add widget, copy widget id + key). | [react-native-sdk](https://docs.prembly.com/docs/react-native-sdk.md), [face liveness RN](https://docs.prembly.com/docs/sdk-initialization-for-face-liveliness-react-native.md), [advanced features](https://docs.prembly.com/docs/advanced-features.md), [prembly-sdk-1](https://docs.prembly.com/docs/prembly-sdk-1.md) |
| **NIN + face** | `POST https://api.prembly.com/verification/nin_w_face`, header `x-api-key`, body `number`, `image` (URL or base64), `face_confidence_threshold` (70 to 100). Returns `face_data{status,message,confidence,threshold}`, `nin_data{...incl. base64 photo and signature}`, `verification.reference`. | [nin-with-face](https://docs.prembly.com/docs/nin-with-face-copy.md) |
| **BVN + face** | `POST /verification/bvn_w_face`, same shape; returns the BVN record incl. `base64Image`, `watchListed`, `face_data`. Test BVN `54651333604`. | [bvn-face-validation](https://docs.prembly.com/docs/bvn-face-validation-copy.md) |
| **Face liveness (API)** | `POST /verification/biometrics/face/liveliness_check`, body `image`; returns `data.confidence` (0 to 1) and `verification.status` (`VERIFIED`). It detects AI-generated or manipulated stills; "when called via the SDK, it also performs a liveness check". No pass threshold documented. | [face-liveliness](https://docs.prembly.com/docs/face-livelinness-check.md) |
| **Document / ID** | Nigerian driver's licence, voter's card, international passport (basic, advance, with-face, image variants). Document image requirements page exists. | [llms.txt index](https://docs.prembly.com/llms.txt) |
| **Session retrieval** | `GET /api/v1/checker-widget/sdk/sessions/{session_id}/` with `x-api-key` **and** `x-organisation-id` (the API & Webhooks page); the reference page shows `/sessions/undelivered/SESSION_ID/?include_payload=true` and lists `GET .../sessions/undelivered/` (paged). Session fields: `session_id`, `status`, `full_name`, `end_user_email`, `end_user_phone`, `widget_name`, `overall_result`, `failure_reason`, `total_cost`, `currency`, `started_at`, `completed_at`, `is_used`. **The two pages contradict each other** (path, parameter type, pagination maths): treat the exact call as UNCONFIRMED until the spike. | [api-webhooks](https://docs.prembly.com/docs/api-webhooks.md), [session by id](https://docs.prembly.com/reference/sdk-session-retrieval-by-id.md), [undelivered](https://docs.prembly.com/reference/get-undelivered-sdk-sessions.md) |

### 3.2 Result codes and errors
- Prembly returns **HTTP 200 for some non-success conditions**; read `response_code`: `00` ok, `01` record not found, `02` service unavailable (retry with back-off), `03` insufficient wallet balance, `07` BVN blocked/watch-listed (do not retry). HTTP: 400, 401, 402 (fund the wallet), 403, 404, 405, 422, 429 (the example message suggests waiting 60 s), 500, 502, 503, 504. [response codes](https://docs.prembly.com/docs/prembly-api-response-codes-verification-status.md), [SDK results](https://docs.prembly.com/docs/verification-results.md).
- SDK callback statuses seen by the *client*: `success`, `failed`, `closed`, `error`. These are hints only (K-D5).
- No documented error body schema for 4xx/5xx; no idempotency or client-reference mechanism on the REST checks (a third-party integration report says the same call twice returns two references and bills twice: **unverified**, [issue](https://github.com/Obycodez55/cliqpay/issues/55)).

### 3.3 Webhooks and signing
Source: [webhooks](https://docs.prembly.com/docs/webhooks.md), [authorization and security](https://docs.prembly.com/docs/authorization-and-security.md), [api-webhooks](https://docs.prembly.com/docs/api-webhooks.md).
- Configured in the dashboard (SDK Library, Webhook), one URL per organisation; a `callback_url` in the request overrides it.
- Headers: **`x-prembly-signature`** = base64 HMAC-SHA256 of the **raw body**, and **`token`** = a unique id per webhook.
- The page says the HMAC is computed "using your **public key**" from the dashboard, and that you *must* verify before processing. Verify the raw body, UTF-8, unmodified; compare in constant time.
- Return 200 on success, 401 invalid signature, 400 invalid payload, 500 server error ("Prembly may retry on 500"). Store `token` for idempotency/replay protection. Respond fast, process asynchronously.
- SDK payload: `event` (`verification.completed` in the sample), `session_id`, `data{verification_response, biometric_results, widget_info{email,first_name,last_name,user_ref}, face_data{status,detail,response_code,confidence}}`, `verification.status` (`VERIFIED` in the sample).
- **UNCONFIRMED:** retry schedule and count; a timestamp or replay window; whether `metadata` is echoed in the webhook (the docs say metadata is "passed back in callbacks" and "helps reconcile webhooks with internal database IDs", but no webhook sample shows it); whether the NIN/BVN number is in the payload; whether sandbox uses the same signing key; the key's true nature (public vs secret).

### 3.4 Sandbox and live
- Test without charge: add `is_test: true` inside `metadata`, or configure the widget for sandbox in the dashboard ([advanced features](https://docs.prembly.com/docs/advanced-features.md)). Test data exists per endpoint (NIN `12345678909`, BVN `54651333604`).
- Going live needs business verification and live API keys (pages exist under "Going Live"; I did not get their text). A third-party report says sandbox responses carry `is_sandbox: true` (**unverified**).
- Base URL: `https://api.prembly.com` in every example. Whether sandbox has its own host is **UNCONFIRMED**.

### 3.5 Rate limits
Only the 429 row above. **No numbers are published.** UNCONFIRMED: per-key limit, burst, widget session limits.

### 3.6 Mobile
- RN SDK: `react-native-webview` plus Prembly's package; Flutter plugin exists. **Expo is not mentioned anywhere.** Compatibility with Expo SDK 57 / React Native 0.86 (this app's stack, `apps/caregiver/package.json:19`) is **UNCONFIRMED** and is the first spike (K0).

### 3.7 Pages read
All fetched 2026-10-27: docs.prembly.com/docs/{bvn-face-validation-copy, nin-with-face-copy, face-livelinness-check, webhooks, authorization-and-security, api-webhooks, react-native-sdk, sdk-initialization-for-face-liveliness-react-native, prembly-sdk-1, advanced-features, verification-results, prembly-api-response-codes-verification-status, wallets-funding, face-recognition-notes, environment (a one-paragraph stub)}; docs.prembly.com/reference/{sdk-session-retrieval-by-id, get-undelivered-sdk-sessions}; docs.prembly.com/llms.txt (index; the second half, which may hold more, was only skimmed for the topics above).

---

## 4. Pricing and cost estimate

**Prembly does not publish a per-check price that I could confirm.** What I found:

| Finding | Source | Trust |
|---|---|---|
| "Plans start from $0.50 per check, volume discounts", estimator shows $0.00, no per-check table | Search-engine summary of [prembly.com/pricing](https://prembly.com/pricing/) (I could not fetch the page body, only its title) | Low: unconfirmed first-hand |
| "No prices published. Contact sales." for BVN | [Ninja comparison, 16 Aug 2026](https://www.ninja.ng/blog/dojah-prembly-youverify-ninja-pricing/) (a competitor) | Medium: dated, but competitor |
| A widget session sample shows `total_cost: "620.00"`, `currency: "NGN"` ("Test Document" widget) | [session reference](https://docs.prembly.com/reference/sdk-session-retrieval-by-id.md) | **Example data**, not a rate; but it shows sessions are billed in NGN and the cost is readable per session |
| Wallet model: pay-as-you-go from a wallet, minimum top-up 25,000 (currency not stated); at zero balance **all checks fail** (`03` / HTTP 402); low-balance email alerts exist | [wallets-funding](https://docs.prembly.com/docs/wallets-funding.md) | High (official) |
| A developer reports the widget page lists "BVN NGN 60, NIN NGN 100" and warns the docs are inconsistent | [GitHub issue](https://github.com/Obycodez55/cliqpay/issues/55) | Low |
| Competitor benchmarks, **not Prembly**: Monnify BVN ₦10, NIN ₦60; Ninja ₦100 per BVN/NIN lookup | [Monnify](https://monnify.com/products/identity-verification), Ninja page | Context only |

So the number the owner needs is **a quote from Prembly sales**: price per widget session (NIN or BVN data check + selfie liveness + face match), and per document-scan session. Meanwhile, a planning envelope. Model: one widget session per attempt, **1.4 attempts per caregiver** (some retries), no re-verification:

| Caregivers onboarded per year | Attempts | At ₦300 / session | At ₦620 / session (the one real sample) | At ₦1,000 / session |
|---|---|---|---|---|
| 100 | 140 | ₦42,000 | ₦86,800 | ₦140,000 |
| 500 | 700 | ₦210,000 | ₦434,000 | ₦700,000 |
| 2,000 | 2,800 | ₦840,000 | ₦1,736,000 | ₦2,800,000 |

These are assumptions, not quotes. Even the high column is small next to a caregiver's lifetime earnings, so **cost is not the risk; an empty wallet is** (everything fails at once). Hence the alerting in section 11.6 and the `provider_unavailable` handling. Add one wallet top-up of at least 25,000 before launch (minimum per the wallet page). Price per check also affects the attempt cap (K-parameter `KYC_MAX_ATTEMPTS`, section 9.3).

---

## 5. What exists today (builder check, code at `oakly/main` 94f45b1)

| Area | Fact | Where |
|---|---|---|
| Result endpoint | `POST /api/v1/caregiver/identity-verification/result` accepts `{type: document|face, result: dict}` from the **client** | `app/api/v1/endpoints/caregiver.py:248`, schema `VerificationResultRequest` `app/schemas/caregiver.py:113` |
| ...already demoted | The service stores the client's dict as evidence and can only move the status to `in_progress`; `verified`/`failed` are never overwritten. **So the "client decides" hole is already closed**; what remains is storing arbitrary client JSON (could hold images) and the endpoint being a lie about who decides. | `app/services/caregiver.py:166-188` |
| Nobody calls it | No mobile screen references identity/KYC; `ConsentScreen` submits straight to `under_review`. `profile_completion_step` 3 is set only by that endpoint. | `apps/caregiver/src/screens/setup/ConsentScreen.tsx:18`, `services/caregiver.py:186`, `navigation/gate.ts:40-44` (`setupRouteFor`: step 2 or more goes to consent) |
| Gate to approval | `unmet_for_approval` already blocks approval unless `identity_verification_status == "verified"` (`IDENTITY_NOT_VERIFIED`). **K-D2 is already true in code.** | `app/services/caregiver_approval.py:350` |
| Admin override | `POST /admin/caregivers/{id}/identity-decision` body `{decision: verified|failed, notes}`; permission `caregivers.approve`; audited; `failed` on an approved caregiver auto-suspends | `app/api/v1/endpoints/admin_caregivers.py:131`, `app/schemas/admin_caregiver.py:36`, `caregiver_approval.py:264-275` |
| Admin detail | `identity` block = `{status, document_verification_result, face_verification_status, face_verification_result}` dumped raw to the console; viewing is an audited read | `caregiver_approval.py:162`, `admin_caregiver.py:123` |
| Review queue | The A1 queue lists by `profile_status` and shows `identity_verification_status` per row; there is no identity filter | `caregiver_approval.py:73-107`, `apps/admin/src/app/(console)/caregivers/applications/queue.tsx` |
| Admin UI | Identity card with Verify/Fail + notes | `apps/admin/src/app/(console)/caregivers/applications/[id]/detail.tsx:160-173` |
| Model | `identity_verification_status` (String: pending, in_progress, verified, failed; no CHECK), `face_verification_status`, `document_verification_result`, `face_verification_result` (JSON), `onfido_applicant_id`, `onfido_check_id`, `CaregiverDocument.onfido_document_id` | `app/db/models/caregiver.py:38,55-56,64-67,97` |
| Dormant Onfido | `onfido_service = OnfidoService()` built at import, **imported by nothing** (dead code); `ONFIDO_*` settings and `.env.example` lines | `app/services/onfido.py`, `app/core/config.py:69-72`, `.env.example:116-118` |
| Erasure | Wipes `document_verification_result`, `face_verification_result`, `face_verification_status`, `onfido_applicant_id` | `app/services/account_erasure.py`, test `app/tests/test_account_deletion.py:57-80` |
| Audit | `admin_audit_log.admin_user_id` is **NOT NULL** and FK to `admin_users`: a webhook-driven (system) decision **cannot** be written there | `app/db/models/audit.py:23`, `app/services/audit.py:45` |
| Consents | Append-only `consents` with a CHECK on `type` (`terms`, `health_processing`, `marketing`, `share_with_family`); the ORM and a DB trigger refuse UPDATE/DELETE | `app/db/models/consent.py:11-45` |
| Webhook patterns | **Paystack**: raw body, HMAC, 400 on a bad signature, processes inline; **no dedupe table** (idempotency lives in the handlers) (`app/api/v1/endpoints/payment.py:145-175`). **Meta**: capped raw body (413), `sha256=` HMAC, constant-time compare, empty secret refused, dedupe by a UNIQUE message id, then ARQ; enqueue failure compensates and returns 503 (`app/api/v1/endpoints/whatsapp.py:25-93`, `app/channels/whatsapp/signature.py`, `app/db/models/whatsapp.py:81`). **The Prembly webhook copies Meta's pattern**, not Paystack's. |
| Worker | ARQ `WorkerSettings` with cron jobs (payout/refund dispatch + reconcile every ~15 min); new jobs register in `app/worker.py:159-186` | |
| Test fakes | `FakeMetaGraph` (`handle(method,path,headers,body)`, `transport_handler` for an `httpx.MockTransport`) and `FakePaystack` live in `app/tests/fake_meta.py`; `FakeExpo` in `app/tests/fakes.py` | |
| Migrations | `app/alembic/versions/`, head `0014_refunds_v2`; baseline is built from models; greenfield, so squashing and dropping columns is allowed (memory: no production data) | |
| Mobile deps | `expo ~57.0.27`, `expo-camera`, `expo-image-picker`, `expo-document-picker`; **no `react-native-webview`** | `apps/caregiver/package.json` |
| Checklist | "Admin approval for caregiver identity; stop accepting the verification result from the client" (section 2); "Identity verification against the real backend flow" (section 4); "KYC: ID and face" (section 8) | `MASTER_CHECKLIST.md:64,103,206` |

**Design (Figma, `ss/caregiver/`)**: `8.14.07` Step 3 "Identity Verification" with two rows ("Scan identification: National ID, Password, or Driver's Licence" and "Face Scan Verification: Turn on your Camera"), a disabled Save & Continue, then a black Document Scan frame with a white rectangle and `8.14.21` a black Face Scan frame with a dashed oval; the same strip has "Background Checks & Data Usage Consent" and "Profile Setup Completed: under review, you will be updated in 24hrs". **Missing:** consent for ID/biometrics, permission denied, capture feedback, processing, retry, failed, needs-review, expired, provider down, rejected.

### Contradictions between the handoff and the code
1. Handoff 2a calls the result endpoint "client posts a result" as if it decided. In code it is already non-authoritative (`app/services/caregiver.py:171-188`). The real remaining work is removing it and the arbitrary-JSON storage, not "stop letting it verify".
2. Handoff 2a Q2 "mandatory before approval? Rec: yes" is **already enforced** (`caregiver_approval.py:350`). The decision costs nothing; the new work is *how* identity becomes `verified`.
3. Handoff 2a says the KYC UI is "partial, ~7 frames". The **app has no KYC screen at all** and onboarding goes Step 1, Step 2, Consent (`gate.ts:40-44`). The caregiver today reaches `under_review` with identity `pending`.
4. Handoff 6 and 2a imply the Paystack webhook is the pattern to copy (signature, dedupe, ARQ). Paystack has no dedupe table; **Meta's** has the dedupe and the queue.
5. Not a contradiction but a constraint the handoff does not mention: `admin_audit_log` cannot record a system actor.

---

## 6. Flow

Actors: **App** (caregiver Expo app), **API** (Oakly FastAPI), **Worker** (ARQ), **Prembly**, **Admin** (console).

```
Caregiver finishes Step 2                         (profile_completion_step = 2)
 |
 | App: GET /caregiver/identity-verification       -> {status:"pending", attempts_left:3, provider_available:true}
 | App shows the intro + consent screen; caregiver ticks the KYC consent
 |
 | App: POST /caregiver/identity-verification/sessions   {consent:true, policy_version}
 |   API: records consent (append-only `consents`, type identity_verification)
 |        creates identity_verifications row  status=created, reference=<opaque uuid>
 |        <- {verification_id, widget:{widget_id, widget_key(public), email, first_name, last_name,
 |            metadata:{oakly_ref:<reference>}}}
 |
 | App opens the Prembly widget (react-native-webview) with that config
 |   caregiver: enters NIN/BVN or scans ID -> liveness selfie -> Prembly matches face to the registry photo
 |   images go phone -> Prembly only; Oakly never sees them
 |
 | App (widget callback): POST /caregiver/identity-verification/{id}/client-event  {event:"completed", session_id}
 |   API: NON-AUTHORITATIVE hint. status created->awaiting_result, remembers session_id to speed polling.
 |        It can never touch caregivers.identity_verification_status.
 |
 | Prembly -> API: POST /api/v1/kyc/prembly/webhook      (x-prembly-signature, token)
 |   API: read capped raw body -> verify HMAC -> INSERT kyc_webhook_events(token UNIQUE) ON CONFLICT DO NOTHING
 |        duplicate token -> 200 now.  New -> enqueue kyc_process_event(event_id) -> 200 (500 if enqueue failed)
 |
 | Worker: kyc_process_event
 |   1. find the attempt: metadata.oakly_ref (from the signed body)  else  session_id hint + provider email match
 |   2. PULL the session from Prembly with the SECRET key (K-C1)  -> the authoritative result
 |   3. webhook and pull must agree on pass/fail; else -> needs_review (reason provider_disagreement)
 |   4. decide: name match, DOB match, face >= threshold, liveness ok, no duplicate fingerprint
 |   5. write the attempt outcome (reason codes, scores) + an append-only event row
 |   6. update caregivers.identity_verification_status (pass->verified, retryable->in_progress,
 |      exhausted or ambiguous->needs_review); set profile_completion_step = 3
 |   7. push + email the caregiver ("verified" / "try again" / "under review")
 |
 | Cron kyc_reconcile (every ~10 min): attempts stuck in started/awaiting_result -> pull the session -> same step 2-7
 |                                     attempts older than the TTL -> expired
 |
 | App polls GET /caregiver/identity-verification (3 s backing off to 10 s) until a terminal state
 |
Caregiver continues to Consent -> under_review          (allowed when verified, needs_review, or provider deferred)
 |
Admin console: application detail shows the provider summary (scores, name/DOB match, reason codes, attempts);
               needs_review rows are filterable in the queue; Verify / Fail via the existing identity-decision;
               Approve is blocked until identity is verified (existing check).
```

---

## 7. State machine

### 7.1 Per attempt (`identity_verifications.status`)

| Status | Meaning | Entered by | Leaves to |
|---|---|---|---|
| `created` | Row exists, widget not yet opened | `POST .../sessions` | `started`, `expired` |
| `started` | Client says the widget opened and reported a `session_id` (hint) | `client-event: opened` | `awaiting_result`, `expired` |
| `awaiting_result` | Client says the widget finished/closed (hint) | `client-event: completed` | any outcome, `expired` |
| `passed` | Provider confirmed (webhook and pull agree), checks all clear | worker | terminal |
| `failed` | Provider says the checks failed (clean failure: face mismatch, liveness, unreadable ID, record not found) | worker | terminal; may allow another attempt |
| `needs_review` | Ambiguous or risky: grey-band face score, name or DOB mismatch, duplicate identity, watch-listed, webhook/pull disagreement, attempts exhausted | worker | terminal; a human decides |
| `expired` | No result within `KYC_SESSION_TTL_HOURS` (24) | cron | terminal; not counted as a failed attempt |
| `error` | Prembly/our fetch failed permanently for this attempt (e.g. persistent `03`/402) | cron after retries | terminal; not counted as a failed attempt |

One **active** attempt per caregiver (`created`, `started`, `awaiting_result`) is enforced by a partial unique index; `POST .../sessions` returns the existing active attempt instead of creating a second one. Terminal states ignore later events (a replayed webhook is a no-op).

### 7.2 Caregiver level (`caregivers.identity_verification_status`)

```
pending --(session created)--> in_progress --passed--------------------------> verified
   ^                              |  \--failed, attempts left----------------> in_progress ("try again")
   |                              |  \--failed, attempts exhausted ----------> needs_review
   |                              \----needs_review outcome-------------------> needs_review
   |
   |        needs_review --(admin identity-decision)--> verified | failed
   |        in_progress/pending --(admin identity-decision)--> verified | failed
   |        failed --(admin identity-reset, new)------> pending      (a fresh set of attempts)
   \--------------------------------------------------------------------------'
verified, failed: set by a provider pass (verified) or by an admin. A provider event NEVER changes a caregiver
that is already verified or failed; it is recorded and ignored (no overwrite of a human decision).
```

- `failed` is **admin-only** (K-C4). A provider failure produces `in_progress` ("try again") or `needs_review`, never `failed`.
- Existing `identity-decision` keeps its two values and its required `notes`; an override of a provider `needs_review` or pass is audited with the attempt id.

### 7.3 Cases the owner asked about

| Case | Behaviour |
|---|---|
| **Retry** | A clean failure while `attempts_used < KYC_MAX_ATTEMPTS` (default 3): caregiver status `in_progress`, app shows "Try again" with a reason-specific tip. After the last allowed failed attempt: `needs_review`. Admin `identity-reset` grants a fresh set. |
| **Expiry** | A session with no result after 24 h becomes `expired` (not a failed attempt); the caregiver starts a new one. A `passed` result does not expire in v1 (re-verification is a non-goal). |
| **Provider outage** | Prembly unreachable or returns 02/5xx/429: attempts stay `awaiting_result`; reconcile retries with back-off. Three consecutive provider errors set a `provider_available=false` flag (in-memory/Redis TTL 15 min) which the status endpoint returns; the app shows "Verification is temporarily unavailable" and offers **Continue without for now** (`POST .../defer`: sets step 3, identity stays `pending`). Approval remains blocked until a later successful verification. Attempts stuck over 24 h also show in the admin queue bucket "identity stuck". |
| **Wallet empty** (`03` / HTTP 402) | Treated as outage for the caregiver **and** raises an ops alert (section 11.6), because it breaks every caregiver at once. |
| **Mismatch** | Provider name or DOB differs from the caregiver's own profile (Step 1 self-declared, `Caregiver.full_name`, `date_of_birth`): `needs_review`, reason `name_mismatch` / `dob_mismatch`. Never auto-fail: a legal name, a married name, or a typo in Step 1 is common. |
| **Face in the grey band** | Score between the provider floor (70) and `KYC_FACE_THRESHOLD`: `needs_review`, reason `face_grey_band`. Below the floor: clean `failed`, reason `face_mismatch`. |
| **Duplicate identity** | The keyed hash of the NIN/BVN already belongs to a `passed` row of **another** caregiver: `needs_review`, reason `duplicate_identity`. The caregiver is told only "under review" (do not reveal who or that a duplicate exists). The admin sees the other caregiver's id (audited read). Needs the NIN/BVN number in the provider payload: **UNCONFIRMED** (section 15). |
| **Watch-listed BVN** (`07`) | `needs_review`, reason `watchlisted`; do not retry with the same BVN. |
| **Webhook says pass, pull says fail (or the reverse)** | `needs_review`, reason `provider_disagreement`; page an admin. |
| **Unknown `oakly_ref` / session** | Recorded as an orphan event, 200, no state change. |
| **Late event after an admin decision** | Recorded, no state change. |

---

## 8. Webhook handling

### 8.1 Endpoint
`POST /api/v1/kyc/prembly/webhook`, public, `include_in_schema=False` (like WhatsApp), mounted next to the other webhooks.

1. Read the raw body through the capped reader (reuse `_read_capped_body`, move it to a shared module; cap 1 MB, 413).
2. Verify `x-prembly-signature`: base64 HMAC-SHA256 of the raw body with `PREMBLY_WEBHOOK_KEY`; `hmac.compare_digest`; an **empty key is refused** (same rule as `app/channels/whatsapp/signature.py`); invalid or missing returns **401** (Prembly's documented code). Log "bad signature" with no body.
3. Parse JSON (400 on invalid). Require `event` and `session_id`.
4. `INSERT INTO kyc_webhook_events (token, ...) ON CONFLICT (token) DO NOTHING`. If nothing inserted: **200** (duplicate). A request with no `token` header is treated as untrusted: 400.
5. Enqueue `kyc_process_event(event_id)` on ARQ. On enqueue failure: delete the just-inserted row (the Meta "compensate" pattern, `whatsapp.py:76-85`) and return **500** (Prembly documents retry on 500) so the retry is processed, not deduped away.
6. Return 200 `{"status":"ok"}` immediately. No Prembly call and no heavy DB work in the request.

### 8.2 What is stored from an event
`kyc_webhook_events`: `token`, `provider_session_id`, `event_type`, `signature_ok`, `body_sha256`, `received_at`, `processed_at`, `outcome` (applied / duplicate / orphan / ignored_terminal / disagreement / error), `verification_id`. **The body is not stored.** The Prembly payload carries the registry record (name, address, next of kin, base64 photo and signature per the NIN sample): persisting it would break K-D4. If a debugging need appears, store an allow-listed subset (`overall_result`, `failure_reason`, numeric scores), never the body. Never log bodies.

### 8.3 Idempotency and replay
- Exactly-once application: `token` UNIQUE (transport level) plus **monotonic, guarded transitions** (state level): terminal attempts ignore events; `UPDATE ... WHERE status IN (non-terminal)`; the worker takes `SELECT ... FOR UPDATE` on the attempt row, and the caregiver row where it changes the caregiver status (same lock order as `CaregiverApprovalService._lock`).
- Replay: Prembly documents no timestamp, so there is no replay *window*. A replayed old body is harmless by construction: same `token` is a duplicate, and a different `token` over an old session hits a terminal attempt. A replay can still waste a pull; the endpoint is rate-limited per IP at the edge (nginx) and the pull is skipped for terminal attempts.
- Concurrency: webhook job and reconcile cron may race on one attempt; the row lock plus the terminal guard make the second one a no-op. Tested with a forced race (section 12).

### 8.4 Why a pull as well (K-C1)
Prembly's HMAC key is documented as the **public** key (the one in the app bundle). Anyone who unpacks the APK can mint a "valid" signature. So a webhook body alone is never enough to reach `verified`. The worker re-fetches the session with `x-api-key` (secret) plus `x-organisation-id` and requires `overall_result` and the face data to agree with the webhook. **If Prembly says the signing key is a separate secret** (open question Q4), keep the pull anyway but the webhook may be applied first and the pull treated as a reconcile check; the owner decides at that point.

---

## 9. Data model (migration `0015_caregiver_kyc`)

Greenfield: no data to preserve (memory: oakly-greenfield-no-data), so columns are dropped, not deprecated.

### 9.1 New tables

**`identity_verifications`** (one row per attempt)

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `caregiver_id` | UUID FK `caregivers.id` | indexed |
| `attempt_no` | int | 1-based per caregiver; counted for the cap |
| `reference` | UUID unique | opaque; sent as `metadata.oakly_ref`; the binding key from the signed body |
| `provider` | text default `prembly` | |
| `provider_session_id` | text unique nullable | from the client hint, then confirmed from the pull |
| `status` | text CHECK | the eight states in 7.1 |
| `reason_codes` | text[] | `face_mismatch`, `liveness_failed`, `face_grey_band`, `name_mismatch`, `dob_mismatch`, `duplicate_identity`, `watchlisted`, `record_not_found`, `document_unreadable`, `provider_disagreement`, `provider_failed`, `attempts_exhausted` |
| `id_type` | text nullable | `nin`, `bvn`, `drivers_licence`, `voters_card`, `passport` |
| `face_confidence`, `liveness_confidence` | numeric nullable | normalised to 0 to 100 (Prembly's scale is inconsistent: Q6) |
| `face_threshold` | int nullable | the threshold applied |
| `name_match`, `dob_match` | bool nullable | results only; the provider's name and DOB are **not stored** |
| `id_fingerprint` | text nullable, indexed | `HMAC-SHA256(KYC_FINGERPRINT_KEY, type + ":" + normalised number)` (K-C2); NULL if the payload has no number |
| `provider_reference` | text nullable | Prembly's `verification.reference` |
| `provider_cost` | numeric nullable | `total_cost` from the session, for cost tracking |
| `decided_via` | text | `provider` or `admin` |
| `started_at`, `completed_at`, `created_at`, `updated_at` | timestamps | |

Partial unique index `(caregiver_id) WHERE status IN ('created','started','awaiting_result')`. Duplicate check: inside the worker transaction take `pg_advisory_xact_lock(hashtext(fingerprint))`, look for a `passed` row with the same fingerprint and a **different** `caregiver_id`.

**`kyc_webhook_events`** as in 8.2, `token` UNIQUE.

**`identity_verification_events`** (append-only, like `consents`): `id`, `verification_id`, `caregiver_id`, `at`, `actor` (`provider`, `reconcile`, `client`, `admin:<id>`), `from_status`, `to_status`, `reason_codes`. This is the **audit trail for system decisions**, because `admin_audit_log.admin_user_id` is NOT NULL (section 5). Admin decisions are *also* written to `admin_audit_log` as today. Same ORM and trigger refusal of UPDATE/DELETE as `consents`.

### 9.2 Changes to existing tables
- `caregivers`: add CHECK on `identity_verification_status IN ('pending','in_progress','needs_review','verified','failed')`; **drop** `onfido_applicant_id`, `onfido_check_id`, `document_verification_result`, `face_verification_result`, `face_verification_status`. (The two JSON columns hold whatever the client posted; they could contain images.)
- `caregiver_documents`: drop `onfido_document_id`. (`extraction_data` is OCR output from the unrelated licence upload; untouched.)
- `consents`: extend `ck_consents_type` with `identity_verification` (policy version string e.g. `kyc-2026-10`; channel `caregiver_app`).
- `app/db/models/caregiver.py` comments "Onfido"/"Prembly Verification Results" rewritten.

### 9.3 Config (names only; no values anywhere in the repo or this doc)
`PREMBLY_BASE_URL` (default `https://api.prembly.com`), `PREMBLY_SECRET_KEY`, `PREMBLY_ORG_ID`, `PREMBLY_WIDGET_ID`, `PREMBLY_WIDGET_KEY` (public; sent to the app by the API so it can rotate without an app release), `PREMBLY_WEBHOOK_KEY`, `KYC_FINGERPRINT_KEY`, `KYC_MAX_ATTEMPTS=3`, `KYC_SESSION_TTL_HOURS=24`, `KYC_FACE_THRESHOLD` (default 85, tuned in the spike), `KYC_FACE_FLOOR=70`. In production the app refuses to boot if the webhook key, secret key or fingerprint key is empty (same pattern as `config.py:214` for Paystack). Remove `ONFIDO_*` from `config.py` and `.env.example`; add the new names to `.env.example` commented out.

---

## 10. Privacy (NDPA 2023 / NDPR)

Biometric data used to identify a person is treated as special-category personal data under Nigeria's data protection law; have counsel confirm the lawful basis and whether a DPIA filing applies before launch. The design supports the usual principles:

### 10.1 Minimisation and what we keep
| Kept | Not kept |
|---|---|
| Provider reference; status; reason codes; face and liveness scores; name/DOB match booleans; id type; the keyed fingerprint; timestamps; cost; the consent record | ID or selfie images; the NIN/BVN number; the provider's name, DOB, address, next of kin, phone, photo or signature; the raw webhook or pull body; any client-posted JSON |

Images go phone to Prembly inside the widget and never reach Oakly. (This is the main reason to prefer the widget, section 11.)

### 10.2 Consent (new, explicit, separate from the terms)
Shown on the intro screen before the widget opens; the ticked consent is posted with `POST .../sessions` and recorded in `consents` (append-only; withdrawal is a new row). Proposed text, for counsel to edit:

> **Verify your identity.** To keep patients safe, Oakly checks that you are who you say you are. With your agreement, our verification partner Prembly will check your National ID (NIN or BVN) or ID document against government records and compare a live photo of your face with the record. Prembly handles your ID details and photo. Oakly keeps only the result of the check and a reference, not your ID or your photo. You can ask us to delete your data at any time. If the check can't confirm you, a member of our team reviews it. [Privacy Policy]  [ ] I agree to this identity check.

Declining stops the KYC step; the caregiver cannot be approved without verification (K-D2) but is not deleted, and an admin may verify manually (the override stays).

### 10.3 Retention and erasure
- Kept for the life of the caregiver account. Proposal for the owner and counsel to confirm: after account erasure or closure keep only status, timestamp and reference for **12 months**, then drop them (audit and fraud trail), and delete the fingerprint at erasure.
- `account_erasure` (`app/services/account_erasure.py`) is extended: delete or null the caregiver's `identity_verifications` PII-ish fields (`provider_session_id`, `id_fingerprint`, `provider_reference`), and scrub `kyc_webhook_events.provider_session_id` for them. Update `test_account_deletion.py`.
- The fingerprint (K-C2) is the one addition beyond "reference and result". It is a keyed hash, not reversible without the key, and only useful for the duplicate check. **Owner call:** keep it (recommended) or drop it and rely on Prembly/admin judgement for duplicates.
- **Cross-border and processor terms:** where Prembly processes and stores data, its retention of images, and a data processing agreement are **UNCONFIRMED** from public docs (Q9). The owner needs a DPA before go-live.

---

## 11. Mobile capture

### 11.1 Recommendation: Prembly widget (SDK) in a WebView, with a spike first

| | A. Prembly widget (recommended) | B. Our own camera + server call |
|---|---|---|
| How | `prembly-reactnative-kyc` `PremblyIdentityWidget`/`IdentityKyc` over `react-native-webview`; capture, liveness and face match run inside Prembly's UI; the result arrives by webhook | `expo-camera` (already a dependency) captures ID and selfie; app uploads to our API; our API calls `nin_w_face` / `liveliness_check` with base64 images |
| Images touch Oakly? | **No** (phone to Prembly) | Yes, in memory on our server, then discarded; one more place a bug leaks them |
| Liveness | Provider's live challenge ("a live camera challenge to prevent spoofing") | Only the still-image `liveliness_check` (AI/manipulation detection), weaker against a replayed photo |
| Result channel | Signed webhook + pull (matches K-D5) | A synchronous HTTPS response to our own call (equally server-to-server, no webhook) |
| Figma fit | Prembly's UI replaces the black Document Scan / Face Scan frames (design divergence, the owner must accept) | Matches the Figma frames |
| Effort | Smaller app code, more integration risk | Much more code, camera UX, image quality, retries |
| Unknowns | Expo SDK 57 / RN 0.86 compatibility; WebView camera permission on iOS and Android; opaque UI | None on the app side, but liveness weakness is a product risk |

**Recommend A, gated by spike K0**, with B as the fallback if the spike fails on Expo compatibility or WebView camera access. Reasons: it satisfies "never store images" by construction, gives real liveness, and is less code.

### 11.2 Expo constraints
- Not Expo Go: it needs a custom dev client. The project already uses `expo prebuild` and a local iOS dev build (`HANDOFF-master.md` 7), so a native module is workable, **but `react-native-webview` is a new native dependency** (not in `package.json`), so the next dev/EAS build must include it; no OTA delivery for the change.
- iOS needs `NSCameraUsageDescription` (the app already has the camera for the AI note taker, confirm the string and that the WebView's `getUserMedia` is allowed: needs `allowsInlineMediaPlayback` and a recent iOS; Prembly's SDK is said to wrap this, UNCONFIRMED). Android needs the `CAMERA` runtime permission (the docs only mention API 23+).
- The npm package name in Prembly's docs is inconsistent; the spike installs the real one and checks its peer dependencies against RN 0.86. Known state: the iOS Debug build does not link today and Release builds are used (`HANDOFF-master.md` 7), so the spike runs on the Release recipe.
- The widget key is public; the app receives it from `POST .../sessions` so it is not hard-coded.

### 11.3 Screens and states (the Figma lacks most of them)
Inserted after Step 2; `setupRouteFor` becomes: step 0 to Step 1, 1 to Step 2, 2 to **IdentityVerification**, 3 and up to Consent (`navigation/gate.ts:40-44`). The server sets step 3 (not the client).

| # | State | What the caregiver sees | API |
|---|---|---|---|
| 1 | **Intro and consent** | "Verify your identity": what is checked, what we keep, the consent checkbox, "Start". | `GET /identity-verification`, `POST .../sessions` |
| 2 | **Camera permission denied** | Explains why, deep link to Settings, "Try again" | local |
| 3 | **Widget open** | Prembly's UI inside our header with a Close control | |
| 4 | **Checking** | "Checking your details...", poll 3 s backing off to 10 s; after 2 min: "This is taking longer. You can leave this screen; we'll notify you." | `GET /identity-verification` |
| 5 | **Verified** | Green check, "Identity verified", Continue to Consent | |
| 6 | **Try again** (clean fail, attempts left) | Reason-specific tip (good light, remove cap or glasses, hold the ID flat and fill the frame, use the ID matching your name) and "Attempt 2 of 3" | `POST .../sessions` |
| 7 | **Under review** (`needs_review`) | "A person on our team will check this, usually within 24 hours. You can continue." Continue to Consent. No reason shown for duplicate/watch-list (privacy, anti-fraud). | |
| 8 | **Provider unavailable** | "Verification is temporarily unavailable. Your progress is saved." with "Continue without for now" | `POST .../defer` |
| 9 | **Expired** | "That session timed out. Start again." | `POST .../sessions` |
| 10 | **Rejected** (admin `failed`) | Reuse the existing rejected status copy (`VerificationStatus.tsx`) plus an "identity" line | existing status |
| 11 | **Closed or error in widget** | "You closed verification before finishing" or "Something went wrong", Retry (an `error`/`closed` client event does not consume an attempt) | `client-event` |
| 12 | **Offline** | Standard offline banner; the widget cannot open | |

After approval the caregiver can open identity from the status screen too (`VerificationStatus.tsx`) when status is `pending`, `in_progress` or admin-reset. No change to the approved app.

---

## 12. Endpoints

### 12.1 Caregiver (`/api/v1/caregiver/...`, caregiver auth; 403 for other user types, same guard as today)
| Method and path | Purpose | Notes |
|---|---|---|
| `GET /identity-verification` | Current state: `{status, attempts_used, attempts_max, active_verification_id, can_start, failure_hint, provider_available, review_eta_hours}` | Never returns a reason such as `duplicate_identity` or scores; `failure_hint` is a coarse enum (`lighting`, `document`, `mismatch_generic`) |
| `POST /identity-verification/sessions` | Body `{consent: true, policy_version}`; creates or returns the active attempt; returns widget config | 409 if verified, or no attempts left (`ATTEMPTS_EXHAUSTED`), 422 if consent false, 503 `PROVIDER_UNAVAILABLE` |
| `POST /identity-verification/{id}/client-event` | Body `{event: opened|completed|closed|error, session_id?}`; advances `created`/`started`/`awaiting_result` only; **non-authoritative** | 404 if the attempt is not the caller's (IDOR test) |
| `POST /identity-verification/defer` | Only while `provider_available=false`; sets step 3 | 409 otherwise |
| `POST /identity-verification/result` | **Removed: answers `410 Gone`**, same precedent as `POST /wallet/withdraw` (`caregiver.py:494`). `VerificationResultRequest` and `process_verification_result` deleted. |

### 12.2 Webhook (public)
`POST /api/v1/kyc/prembly/webhook` (section 8).

### 12.3 Admin (`/api/v1/admin/caregivers/...`)
| Method and path | Change | Permission |
|---|---|---|
| `GET /{id}` | `identity` block becomes `{status, review_reason, attempts:[{id, attempt_no, status, started_at, completed_at, id_type, face_confidence, liveness_confidence, face_threshold, name_match, dob_match, reason_codes, provider_reference, decided_via}], duplicate_of_caregiver_id?}`. **No images, no NIN.** Still an audited read. | `caregivers.read` |
| `GET /applications` | New filter `identity=needs_review|stuck|...`; a `needs_review_count` for a badge | `caregivers.read` |
| `POST /{id}/identity-decision` | Unchanged contract (`verified|failed`, `notes`); also closes the open `needs_review` attempt (`decided_via=admin`), writes the `identity_verification_events` row and the existing audit row | `caregivers.approve` |
| `POST /{id}/identity-reset` (new) | Body `{notes}`; sets identity back to `pending`, grants a fresh set of attempts, audited. Needed to get a `failed` or exhausted caregiver another go. | `caregivers.approve` |

No new permission strings (so no RBAC seed or custom-role migration). Finance has neither, as today.

---

## 13. Admin console changes (`apps/admin`)
- **Queue** (`queue.tsx`): a filter chip "Identity needs review" and a count badge; a column showing the identity status already exists.
- **Detail** (`detail.tsx:160`): the Identity card shows the provider summary (attempts, outcome, scores with the threshold, name/DOB match as yes/no, reason codes in plain words, duplicate-of link, provider reference) and keeps Verify / Fail with required notes; add "Allow another attempt" (identity-reset) when failed or exhausted.
- Copy states plainly that **images are not stored here**; to see the person, the admin uses the Prembly dashboard (reference shown), which is outside Oakly.
- API types regenerated with `OAKLY_BACKEND_DIR=<backend> pnpm api:sync` (`HANDOFF-master.md` 7); Vitest/RTL for each state; no new auth surface.

---

## 14. Slice plan

Rules from the owner (handoff section 6): small PRs, Sonnet implements, Opus reviews auth/PII/webhook-signature code **once plus one re-review**, minor findings go to SOP follow-ups; max 3 sessions at once; backend CI parity locally before each push (`poetry check --lock`, ruff E9/F63/F7/F82, `scripts.ci_smoke`, both `render_fe_guide.py --check`, full pytest); monorepo CI parity for mobile/admin; every PR carries an SOP and checklist update, and the FE guide section when an endpoint shape changes. Sizes are net lines including tests: **S** < 300, **M** 300 to 700, **L** > 700 (none planned).

| Slice | Scope | Repo | Migration | Endpoints | Sensitivity (Opus?) | Size | Depends on |
|---|---|---|---|---|---|---|---|
| **K0** Spike and provider setup | Owner: Prembly account, a dashboard widget (NIN or BVN + liveness + face match, doc scan optional), sandbox keys, webhook URL (via ngrok, as the Meta test), wallet top-up. Dev: install the RN SDK in a throwaway dev build on Expo 57, open the widget, capture the **real** callback and **real** webhook + session body (redacted copies kept in `meta-test/`-style scratch) to settle Q1 to Q8. Output: a 1-page findings note and the go/no-go for widget vs fallback B. **No product code merged.** | monorepo (scratch) + notes | no | none | No (no secrets committed; captures redacted) | S | owner keys |
| **K1** Foundation | `0015_caregiver_kyc`: tables, CHECKs, drops, `consents` type; models; config names; `app/services/prembly.py` (httpx client, injectable transport: session get, undelivered list, error mapping 01/02/03/07/402/429); `app/tests/fake_prembly.py`; delete `onfido.py` and `ONFIDO_*`; erasure update; model and migration tests | backend | **yes** | none | PII data model: Sonnet + one Opus pass on the migration and erasure only if cheap; otherwise Sonnet | M | none |
| **K2** Caregiver API | `GET .../identity-verification`, `POST .../sessions` (consent recording, attempt cap, one-active), `client-event`, `defer`, `result` to 410; coarse failure hints; `profile_completion_step` handling; IDOR and consent tests; FE guide section from live captures against the fake | backend | no | the five in 12.1 | Auth + PII: Sonnet with explicit IDOR/guard tests; Opus **not** required | M | K1 |
| **K3** Webhook ingest and confirm | The endpoint, signature verify, capped body, event ledger, dedupe, enqueue and compensation; ARQ job `kyc_process_event` that locates the attempt, **pulls** the session and records the raw outcome (scores, reason codes) on the attempt; tests for bad/missing signature, empty key, tamper, replay, orphan, enqueue failure | backend | no | webhook | **Webhook signature + trust boundary: Opus review required** | M | K1 (K0 for real payload shapes; if K0 slips, build on the documented shapes and adjust) |
| **K4** Decision engine | Map outcomes to caregiver status (7.2), name/DOB matching, duplicate fingerprint + advisory lock, attempts and expiry, `kyc_reconcile_job` cron, provider-availability flag and wallet-empty alert, push + email to the caregiver, `identity_verification_events` writes; the never-overwrite-a-human-decision guard; forced-race and property tests (caregiver is never `verified` without a pull-confirmed pass) | backend | no | none | **Approval trust + PII: Opus review required** | M | K3 |
| **K5** Admin API | Identity block, queue filter and count, `identity-decision` closes the attempt and writes the event, `identity-reset`; audit entries; FE guide admin section; permission tests (Finance denied) | backend | no | 12.3 | Permission checks: Sonnet + Opus quick pass on the reset/permissions only | S to M | K1 (merge after K4 so reasons exist) |
| **K6** Admin console | Queue filter and badge, Identity card, reset action; types sync; Vitest/RTL; update the existing Playwright approve flow if it touches identity | monorepo `apps/admin` | no | none | No | S | K5 |
| **K7a** Mobile screens and states | `IdentityVerification` route, intro and consent, states 4 to 12 with a mocked service, polling hook, `setupRouteFor` change and `gate.ts` tests, status-screen entry; no native dependency | monorepo `apps/caregiver` | no | none | No (no secrets) | M | K2's OpenAPI |
| **K7b** Mobile widget integration | `react-native-webview` + Prembly package, config plugin/permissions, open the widget with the server-provided config, forward `client-event`s, new dev build recipe in the SOP; **or** fallback B if K0 said no-go (that variant is its own larger plan) | monorepo `apps/caregiver` | no | none | No | S to M | K0, K7a |
| **K8** Live sandbox pass and docs | Owner-run end-to-end on a device with sandbox keys (and one live-key test with the owner's own NIN, never committed); capture and record differences; FE guide "verified live" rows; SOP; checklist ticks; resolve open questions in this doc | backend + monorepo | no | none | No | S | K1 to K7 |

**Order and parallelism** (max 3 working sessions, no two writers on the same files):

```
Wave 1:  K0 (owner + mobile spike)        ||  K1 (backend foundation)
Wave 2:  K2 (caregiver API)  ||  K3 (webhook ingest, Opus)          [K5 may start here if a slot is free]
Wave 3:  K4 (decision engine, Opus)  ||  K5 (admin API)  ||  K7a (mobile screens; needs K2's API types)
Wave 4:  K6 (console; needs K5)  ||  K7b (widget; needs K0 and K7a)
Wave 5:  K8 live pass (owner present)
```
Backend, admin and mobile streams are independent after K1/K2 (K6 and K7a/b touch the monorepo only; K2 to K5 touch the backend, and they share no files except `app/api/v1/api.py` route registration and `app/tests/golden/response_shapes.json`, so rebase between them). Merge order: K1, K2, K3, K4, K5, K6, K7a, K7b, K8; the lead merges only on the owner's "merge #N".

**Before K4 merges** the endpoint set is dark: with no widget wired (K7b), nothing in production starts a session, so earlier PRs are safe to merge in order.

---

## 15. Open questions

For **Prembly support / a sandbox spike** (K0) unless marked owner:

| # | Question | Why it matters |
|---|---|---|
| Q1 | Can one widget workflow do NIN or BVN data check **plus** liveness selfie **plus** face match against the registry photo, and does the ID number come back? Or must we use separate API calls? | Whether option A works at all; whether duplicate detection (K-C2) has a number to hash |
| Q2 | Is `metadata` (our `oakly_ref`) echoed in the **webhook** and in the session pull? Is `user_ref` settable? | The binding key from the signed body (section 6 step 1). Fallback is email match |
| Q3 | Exact session-retrieval call (path, auth headers, `x-organisation-id`, rate limit); the two doc pages contradict each other | The K-C1 pull |
| Q4 | Is the webhook HMAC key really the public widget key, or is there a separate signing secret? Does the sandbox use the same one? | Whether the signature alone is trustworthy (section 8.4) |
| Q5 | Webhook retry schedule and count; is there a timestamp or event id besides `token`; max body size; source IPs to allowlist | Replay handling, the 500-retry behaviour |
| Q6 | Meaning and scale of `face_data.confidence` (a sample shows `1` next to a threshold of `90`), the liveness pass rule, recommended thresholds for caregiver onboarding | Setting `KYC_FACE_THRESHOLD` and the grey band |
| Q7 | Sandbox: host, keys, how to trigger each outcome (fail, no match, `07`, `03`); whether test mode still bills | The fake mirrors these; the live pass needs them |
| Q8 | `react-native-webview` / `prembly-reactnative-kyc` on Expo SDK 57 and RN 0.86 (iOS and Android); the real package name; camera in the WebView; widget theming (logo, colours, language) | Go/no-go for option A (K0) |
| Q9 | Price per widget session by workflow and per document scan; volume tiers; wallet top-up minimum currency; where data is processed and stored, image retention, a DPA; uptime/SLA and a status page | Section 4 budget; section 10 compliance |
| Q10 | Rate limits (per key, per second, per widget) | Back-off and the attempt cap |

For the **owner**:
| # | Question |
|---|---|
| O1 | Keep the NIN/BVN fingerprint for duplicate detection (K-C2)? Recommended: yes. |
| O2 | Accept the widget UI replacing the Figma Document Scan / Face Scan frames (K0 may force this anyway)? |
| O3 | Retention after erasure (proposal: 12 months of status + timestamp + reference), consent wording, and a counsel check on the lawful basis and DPIA (section 10) |
| O4 | Attempt cap (3) and the face threshold: defaults are placeholders until the spike. Also the "Continue without for now" outage path (K-C3): accept or require KYC before consent? |
| O5 | Who is told when the Prembly wallet is low or empty (needs a named email or channel; section 16 R2) |

---

## 16. Security review points and risks

### 16.1 Review checklist (Opus passes on K3 and K4 should walk these)
1. Signature verified over the **raw** body before any parse; constant-time compare; empty key refused; 401 on failure; no body in logs (including exceptions and the access log).
2. A webhook alone can never set `verified` (K-C1); the pull uses the secret key; a disagreement goes to `needs_review`.
3. `token` UNIQUE inserted before enqueue; enqueue failure compensates; duplicate returns 200.
4. State changes only under `FOR UPDATE`; terminal states are inert; no event overwrites an admin `verified`/`failed`.
5. Binding: the attempt comes from the signed body's `oakly_ref`, never from the client's hint alone; the client-reported `session_id` is only a hint and must match the provider's own session email/ref before use. An attacker who completes **their own** verification and tries to bind it to someone else's account is stopped because the hint cannot override `oakly_ref`.
6. IDOR: `client-event` and any attempt route check `caregiver_id == caller`.
7. No PII persisted: grep the diff for `nin`, `bvn`, `base64`, `photo`, `address` in models, events, logs and tests; the fake's fixtures use obviously fake numbers.
8. The public widget key is not a secret; the secret, org id, webhook and fingerprint keys live only in server env (names in `.env.example`, values never in git); tests use generated placeholders.
9. The 410 route reveals nothing; the removed schema leaves no dead import.
10. Admin read of the identity block is audited (existing); the reset and decision endpoints need `caregivers.approve`; Finance denied.
11. Erasure removes the new identifiers.
12. Rate limits: the sessions endpoint per user (attempt cap does the real work because Prembly bills per attempt), the webhook per IP at the edge.

### 16.2 Risks
| # | Risk | Mitigation |
|---|---|---|
| R1 | Expo or WebView camera incompatibility kills option A | K0 spike first; fallback B is designed (11.1) |
| R2 | **Prembly wallet empties** and every caregiver fails | Low-balance email alert in Prembly (they offer it, section 4); our `03`/402 detection raises an ops alert and flips `provider_available=false`; the defer path keeps onboarding moving |
| R3 | The webhook signature key is public (section 8.4) | The mandatory pull; ask Prembly (Q4) |
| R4 | Public docs are thin or contradictory (session path, package name, confidence scale) | Every such point is a K0 question; the fake is built against documented shapes and updated from the real captures; the K8 live pass is the final proof. **Until K8, nothing here is verified against real Prembly.** |
| R5 | Borrowed identity: a friend completes the check for the applicant | Name and DOB must match the applicant's own profile, face match, duplicate fingerprint, and a human still approves. It is reduced, not eliminated (a friend with the same name and DOB is not realistic) |
| R6 | NIN or face data quality (new NIN records, poor selfies) causes honest failures | Reason-specific tips, 3 attempts, then a person decides; never auto-reject |
| R7 | Provider vendor lock | One `prembly.py` boundary plus `provider` columns keep a second provider addable |
| R8 | The mobile ban on images is broken by a future debugging habit | Section 8.2 rule, review point 7 |
| R9 | Billing surprise per attempt | Attempt cap, `provider_cost` stored, monthly review |
| R10 | Admin `identity-decision` becomes a rubber stamp | The queue shows the provider summary and reason codes; the audit trail records who overrode a failure |

---

## 17. Test strategy

**`FakePrembly`** (`app/tests/fake_prembly.py`), modelled on `FakeMetaGraph` and `FakePaystack`:
- `handle(method, path, headers, body) -> (status, json)` plain request-in/response-out, `transport_handler` for an `httpx.MockTransport` (so nothing leaves the machine), and an optional HTTP server for the capture script; `install(monkeypatch)` swaps the service transport.
- `script_session(session_id, overall_result, face_confidence, liveness_confidence, name, dob, id_number, reason)`; `outage`, `wallet_empty` (`03` and HTTP 402), `rate_limited` (429), `server_error`, `timeout`, `undelivered` list.
- Helpers `sign(body, key)` (base64 HMAC-SHA256) and `signed_webhook(...)`, with `token`, mirroring `fake_meta.sign` / `signed_headers`.
- Fixtures contain only obviously fake data (labelled placeholder names, the documented test NIN/BVN).

**Backend tests (no docker/pytest is run in this design pass)**
- Webhook: valid, bad, missing, empty-key, tampered body, wrong encoding, oversize 413, missing `token`, duplicate `token`, replayed old session, orphan session, enqueue failure then retry processes.
- State machine: every transition in 7.1/7.2; attempt cap; one-active-attempt unique index; expiry; reconcile; terminal inertness; a webhook after an admin decision changes nothing; admin reset.
- Trust: **property test** that over random event sequences (including duplicates, reorderings, disagreements) `identity_verification_status == 'verified'` only if a pull-confirmed `passed` attempt or an admin decision exists, and `failed` only through admin.
- Decision rules: pass; face fail; grey band; name mismatch (casing, accents, order, missing middle name); DOB mismatch; duplicate fingerprint across two caregivers (race with advisory lock); watch-listed; webhook/pull disagreement.
- Caregiver API: IDOR, consent required and recorded append-only, cap 409, `result` 410, `defer` only when unavailable, response shapes (golden `response_shapes.json`).
- Admin API: block shape without images, filter, decision closes the attempt, reset, Finance 403.
- Migration: upgrade from `0014`, CHECKs, partial unique index, dropped columns; erasure wipes the new fields (extend `test_account_deletion.py`).
- No-PII test: run the full flow with sentinel values in the fake's payload (a fake NIN, a base64 photo string) and assert none appear in any table, event row, log record or audit row.
- Forced race: webhook job vs reconcile on one attempt.

**FE guide**: a `scripts/capture_kyc_flow.py` (like `capture_caregiver_flow.py`) runs the flow against the fake and the rendered guide uses those captures; every Prembly-dependent row is marked "verified against the fake, not live" until K8. Unit tests for mobile (`gate.ts` table, polling hook, each state) and admin (Vitest/RTL). Live: K8 on a device with sandbox keys, then one owner-run check with live keys.

---

## 18. Files touched (for the slices)

Backend: `app/alembic/versions/0015_caregiver_kyc.py`; `app/db/models/{caregiver,consent}.py` + new `app/db/models/identity_verification.py`; `app/services/{prembly.py (new), caregiver.py, caregiver_approval.py, account_erasure.py}` and `onfido.py` deleted; `app/api/v1/endpoints/{caregiver.py, admin_caregivers.py}` + new `kyc_webhook.py`; `app/schemas/{caregiver.py, admin_caregiver.py}`; `app/worker.py`; `app/core/config.py`, `.env.example`; `app/tests/fake_prembly.py` and new test files; `scripts/capture_kyc_flow.py`; `docs/fe-integration-guide-{caregiver,admin}.md`; `docs/sop/`; `docs/checklist/MASTER_CHECKLIST.md`.
Monorepo: `apps/caregiver/src/{navigation/gate.ts, screens/setup/IdentityVerification*.tsx, hooks/queries.ts, services/caregiver.service.ts}`, `package.json` (webview, Prembly SDK), `app.json` (permissions); `apps/admin/src/app/(console)/caregivers/applications/{queue,[id]/detail}.tsx`, `src/lib/caregivers.ts`; `packages/api-client` regenerated.

### Monorepo checklist block (paste into `docs/checklist/README.md` when K6/K7 start; not edited here because this worktree is the backend repo only)
```
## Caregiver KYC (Prembly)
- [ ] K7a Identity step: route after Step 2, intro + consent, checking / verified / try again / under review / unavailable / expired / error states, `setupRouteFor` + gate tests
- [ ] K7b Prembly widget in a dev build (react-native-webview, camera permissions), client-event forwarding, build recipe in the SOP
- [ ] K6 Admin console: "Identity needs review" filter and badge, provider summary card, Verify / Fail, Allow another attempt
- [ ] K8 Device pass against sandbox keys (and one live-key check)
```
