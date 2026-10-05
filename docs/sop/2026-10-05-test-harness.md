# SOP: Backend test harness

| | |
|---|---|
| **Date** | 2026-10-05 |
| **Branch** | `test/harness` (base `chore/poetry-migration`) |
| **Status** | Verified locally |

## What shipped
A working pytest suite foundation:
- a throwaway Postgres for every test run;
- plain factory functions for users, patients, caregivers, admins, bookings and wallets;
- auth header helpers;
- a guard that blocks all outbound SMTP and HTTP.

CI now runs the suite. The old tests were removed.

## Why
- The previous tests (`app/tests/test_*.py`) called `http://localhost:8000` and the **live Supabase database**, using hardcoded IDs and **hardcoded JWTs**. Running them would have read and written production data.
- No security fix can be proven without tests that hit a real database.

## How
| Decision | Choice | Why |
|---|---|---|
| Database | `testcontainers` Postgres 16, or `TEST_DATABASE_URL` if set | Same engine as production. Runs the same way locally and on GitHub runners, which have Docker. |
| Wiring | Set `DATABASE_URL` *before* importing the app | The app creates its engine at import, and endpoints import `get_db` from two modules (`app.db.session`, `app.api.deps`). Pointing the URL at the test DB covers both without dependency overrides. |
| Schema | `Base.metadata.create_all` once per session | The Alembic history can't build an empty DB yet (two roots, missing tables). Replacing it with a baseline is WP-E1. |
| Isolation | `TRUNCATE … RESTART IDENTITY CASCADE` after each test | Works even though the app commits its own transactions. |
| Secrets | `OAKLY_ENV_FILE=""` turns off `.env` loading (`app/core/config.py`) | A developer's real `.env` can never leak into tests. |
| Network | Autouse fixture patches `smtplib.SMTP/SMTP_SSL`, `requests.Session.request` and `httpx.HTTPTransport.handle_request` to raise `NetworkBlocked` | A test can never send real email, charge Paystack or call Gemini. Tests must mock the service. |
| Server errors | `TestClient(raise_server_exceptions=False)` | Tests assert on the status code a real client would see. |

## What's involved
- `app/tests/conftest.py`: environment, container, schema, cleanup, network guard, `client`, `db` and `sent_emails` fixtures.
- `app/tests/factories.py`: `make_user/patient/caregiver/admin/booking/caregiver_wallet`, `auth_headers`, `refresh_headers`.
- `app/tests/guards.py`: `NetworkBlocked`.
- `app/tests/test_harness.py`: self-tests.
- `app/core/config.py`: `env_file` comes from `OAKLY_ENV_FILE`, with `.env` as the default.
- `pyproject.toml`: `testcontainers[postgres]` (dev) and `pythonpath = ["."]`.
- `.github/workflows/ci.yml`: new pytest step.
- Deleted: `test_admin_prescription_settings.py`, `test_caregiver_profile.py`, `test_nutritionist_booking.py`, `test_prescription_flow.py`, `test_vitals.py`.

## Verification
Every CI step passes locally: `poetry check --lock`, ruff bug-class rules, the smoke test, and `pytest` (5 passed). The self-tests prove:
- an authenticated request reaches the test DB;
- each test starts with empty tables;
- HTTP and SMTP calls raise `NetworkBlocked`.

## Operate
- Run: `poetry run pytest`. It needs Docker running locally.
- Use an existing database instead: `TEST_DATABASE_URL=postgresql://… poetry run pytest`.
- Don't import `conftest` from tests. Import helpers from `app.tests.factories` / `app.tests.guards`. Importing conftest a second time would start a second container.

## Follow-ups
- Build the schema with Alembic migrations once the baseline exists (WP-E1).
- Coverage reporting.
