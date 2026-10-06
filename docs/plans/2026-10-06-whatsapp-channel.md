# Plan: WhatsApp patient channel (Phase W)

| | |
|---|---|
| **Date** | 2026-10-06 |
| **Spec** | [WhatsApp PRD](../prd/PRD-whatsapp-patient-channel.md). `WA-FR-n` refers to it. |
| **Architecture** | **Same backend, same database, same domain services.** WhatsApp is a new *channel module* (`app/channels/whatsapp/`) that calls the existing services: pricing, booking creation, Paystack settlement, dispatch, visit code, consent and audit. It does not go through the mobile HTTP endpoints. The patient mobile app is paused. |
| **Environment** | There is no server, and Meta business verification is pending. Everything is built and tested locally. Automated tests use a **fake Meta Graph API** (the same pattern as the fake Expo in the caregiver captures). Manual tests use a Meta **developer test number** and a local tunnel (owner sets these up later). |

## Ground rules
The same as Phase 0, C and A:
- Write tests first.
- One PR per package, after CI passes locally.
- No AI attribution in commits or PRs.
- Keep the SOP and master checklist current.
- Opus review for the webhook signature, consent, payments and anything that sends patient data.
- **No health data ever goes into a WhatsApp message or template** (WA-NFR-6). Message bodies aren't stored by default.

## W1: Foundations (branch `feat/whatsapp-foundations`)
- **Phone-first identity.**
  - `users.email` becomes nullable, so a WhatsApp patient may have no email.
  - Unique phone numbers in E.164 format.
  - App registration still requires an email.
- **Data model** (PRD §10):
  - `whatsapp_contacts`: `wa_id` unique, phone, user, language, source, opt-in, marketing opt-in, opted-out, `last_inbound_at`, blocked.
  - `whatsapp_conversations`: state, context JSONB, assigned agent, automation paused.
  - `whatsapp_messages`: **metadata only**. Meta message ID (unique), direction, type, template, category, status, error code.
  - `care_recipients` and `care_relationships`.
  - `consents`: append-only, with policy version, evidence and withdrawal.
  - `support_conversations`: the handover queue for the Admin console inbox later.
  - One migration.
- **Webhook** (WA-FR-1; PRD §11.1):
  - `GET /api/v1/whatsapp/webhook` handles verification.
  - `POST /api/v1/whatsapp/webhook`:
    - checks `X-Hub-Signature-256` (HMAC-SHA256 of the raw body with the app secret; rejected when the secret is empty);
    - deduplicates on the Meta message ID;
    - **puts the work on the ARQ queue and returns 200 fast**;
    - records status updates (sent, delivered, read, failed).
- **Outbound sender** (`MetaCloudClient`): a thin httpx client for the Graph API. It sends text, interactive lists and buttons, CTA URLs, location requests, Flows and templates. It has a timeout, retries with backoff on 429 and 5xx, records errors on `whatsapp_messages`, and reads its base URL from config so tests can point it at the fake.
- **Conversation engine:** a pure state machine (PRD §9.1), with the state persisted per contact. W1 handles:
  - **first contact**: create the contact and user, greet;
  - **consent** (WA-FR-5–9): an interactive consent step with WhatsApp's own buttons (the Flow version comes in W2), stored in `consents`;
  - **STOP / STOP DATA / START**;
  - **the menu** (WA-FR-10) and unknown input, with two misses leading to an offer of a human (WA-FR-11);
  - **the emergency screen on every inbound message** (WA-FR-40): an English and Pidgin word list, an immediate reply, an urgent handover ticket, and automation paused;
  - **human handover** (WA-FR-41).
- **Config:** `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_GRAPH_URL`, and the emergency contact numbers.
- **A local fake Meta** (`scripts/fake_meta.py`, or a test fixture) that records outbound calls and can post signed inbound webhooks. It's used by the tests and by an end-to-end script.

## W2: Booking in chat
- **The booking Flow** (WA-FR-13–17) and its encrypted Flow data endpoint (RSA-OAEP + AES-GCM; the private key comes from config).
- **Location request.**
- **Quote** (WA-FR-18–21): a locked quote held for 30 minutes, from the same `compute_price`.
- **The Paystack link** (WA-FR-22–27): no email needed, using a deterministic internal address. It reuses the expiry and settlement already built.
- **Template registry.**

## W3: Notifications and my bookings
- **A channel-agnostic `NotificationService`.** It maps domain events (paid, assigned, en route, visit code, completed, refund) to WhatsApp templates for the booker, and push for caregivers (WA-FR-28–39).
- **My bookings and cancellation**, using the policy setting.

## W4: The web companion
- **The API:** magic links, step-up OTP through the authentication template, booking and tracking pages (WA-FR-43–48).
- **`apps/companion`** in the frontend monorepo.

## W5: Phase 2
- FallGuard through a Flow.
- Vitals reminders.
- Care circle.
- Pidgin.

## Order
1. W1 starts now, in parallel with Admin A0/A1.
2. W2 follows W1.
3. W3 follows W2.
4. W4 can overlap W3.
