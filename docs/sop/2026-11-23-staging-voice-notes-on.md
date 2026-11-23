# SOP: voice notes switched on for staging

| | |
|---|---|
| **What shipped** | `VOICE_NOTES_ENABLED=true` in `.env.staging.enc` (was false), and the staging template and its test now say true. Production is untouched. |
| **Why** | The owner wants every built feature on while building and testing; the caregiver app's voice-note flow (record, transcribe, review, save) was off in both the app and the server. The Gemini key and all `VOICE_NOTE_*` settings are already set on staging. |
| **Not verified** | A real recording through Gemini on staging (the handoff lists it as an owner-blocked live pass). Only the setting and the template test were checked. |

## What's involved
`.env.staging.enc` (one name changed, decrypted in memory, round trip checked, no values printed), `deploy/env.example`, `app/tests/test_staging_env_example.py`.

## Verification
`pytest app/tests/test_staging_env_example.py` (16 passed). After the deploy: record a note in the caregiver build made with `EXPO_PUBLIC_VOICE_NOTES=live`; if it fails, the worker log names the provider error.

## Operate / roll back
Set `VOICE_NOTES_ENABLED=false` again with `scripts/env.sh` and deploy. Staging redeploys on every push to `develop`.

## Follow-ups
Live pass with a real recording; watch the Gemini quota and the daily audio cap (`VOICE_NOTE_DAILY_AUDIO_SECONDS_CAP`).
