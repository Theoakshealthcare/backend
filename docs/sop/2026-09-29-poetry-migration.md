# SOP — Backend dependency management moved to Poetry

| | |
|---|---|
| **Date** | 2026-09-29 |
| **Branch** | `chore/poetry-migration` |
| **Status** | Verified locally; not yet deployed |

## What shipped

`requirements.txt` has been replaced by `pyproject.toml` plus `poetry.lock`, managed by Poetry 2.2.1. The Dockerfile, Render config and boot script now install from the lock file. The repo also gets its first CI workflow and a `.gitignore`. Two crash bugs were fixed because the new lint gate flagged them.

## Why

- `requirements.txt` used only `>=` floors. So **every Render build pulled the newest versions** of every package, which is non-reproducible and already brittle. passlib only works because `bcrypt==4.0.1` was hand-pinned.
- It listed four packages the code never imports: `asyncpg`, `emails`, `tenacity` and `pytest-asyncio`. It also missed `requests`, which Paystack needs and which only arrived by accident as a sub-dependency.
- It installed plain `uvicorn`, which has no WebSocket library. The `/booking/ws/*` endpoints relied on `websockets` arriving transitively.
- There was no CI and no `.gitignore`.

## How

| Decision | Choice | Rejected alternative / reason |
|---|---|---|
| Manifest format | PEP 621 `[project]` table (Poetry 2.x style) | Legacy `[tool.poetry.dependencies]` is deprecated in Poetry 2 |
| Packaging mode | `package-mode = false` | The app isn't a library, so there's nothing to build or publish |
| Python | 3.11 (`.python-version`); `requires-python >=3.11,<3.15` | Keeps the migration to packaging only. The Docker image was already 3.11. Moving to 3.12 or 3.13 is a separate change. |
| Version pins | Caret-style ranges starting at today's latest; the lock file pins exact versions | Floors only (the old approach) aren't reproducible |
| passlib / bcrypt | Kept `passlib==1.7.4` + `bcrypt==4.0.1` | bcrypt 4.1+ breaks passlib, and 5.x fails its self-test. Switch to `pwdlib` in a follow-up. |
| google-generativeai | Kept at 0.8.6 | Deprecated upstream. Moving to `google-genai` changes code, so it's a separate change. |
| uvicorn | `uvicorn[standard]` | Brings `websockets` and `uvloop` explicitly |
| Virtualenv | In-project `.venv/` (`poetry.toml`) | Predictable path for IDEs and the Docker copy step |
| Docker | Two-stage build: Poetry only in the builder, copy `.venv` into the runtime image, `slim-bookworm` | Old single-stage image shipped `gcc` and ran on bullseye |
| Lint gate | `ruff check --select E9,F63,F7,F82` (syntax errors and undefined names) | Full ruleset has 333 findings and `ruff format` would rewrite 68 files. That churn would collide with the security fixes coming next. Enforce the full set after cleanup. |

## What's involved

| File | Change |
|---|---|
| `pyproject.toml` | **New.** Dependencies, dev group (pytest, httpx, ruff), ruff and pytest config |
| `poetry.lock` | **New.** Exact resolved versions |
| `poetry.toml` | **New.** `virtualenvs.in-project = true` |
| `.python-version` | **New.** `3.11` (read by Render and `actions/setup-python`) |
| `requirements.txt` | **Deleted** |
| `Dockerfile` | Two-stage Poetry build; venv at `/app/.venv` |
| `boot/docker-run.sh` | Activates `/app/.venv` instead of `/opt/venv` |
| `render.yaml` | Build: `poetry install --only main --no-root`. Start: `poetry run uvicorn …`. Env `POETRY_VERSION=2.2.1`. |
| `.github/workflows/ci.yml` | **New.** Lock check, install, bug-class lint, smoke test |
| `scripts/ci_smoke.py` | **New.** Boots the app in-process, checks `/health`, the OpenAPI spec and password hashing. Makes no DB connection. |
| `.gitignore` | **New.** `.env*`, `__pycache__`, `.venv`, `uploads/`, caches |
| `.dockerignore` | Also ignores `.venv/` |
| `README.md` | Setup section rewritten for Poetry |
| `app/api/v1/endpoints/caregiver.py:180` | **Bug fix.** A local named `status` hid `fastapi.status`, so the 403/404 paths of `GET /caregiver/verification-status` returned **500** |
| `app/services/caregiver.py` | Removed a dead Onfido block that referenced an undefined `onfido_service`. It always raised and was swallowed, so behavior is unchanged. |

**Dependencies removed:** `asyncpg`, `emails`, `tenacity`, `pytest-asyncio`, the duplicate `python-multipart` and `python-dotenv` entries.
**Dependencies added:** `requests`, `uvicorn[standard]` extras, `ruff` (dev).
**Resolved versions (main libraries):** fastapi 0.141.1 · pydantic 2.13.5 · sqlalchemy 2.0.54 · alembic 1.20.0 · uvicorn 0.53.0 · gunicorn 26.2.0.

## Verification

| Check | Result |
|---|---|
| `poetry check --lock` | All set |
| Every CI `poetry` step run verbatim in a clean copy (no `.env`, fresh venv, Python 3.11) | All passed |
| `python -m scripts.ci_smoke` | 89 API operations served; bcrypt hashing verifies |
| All 9 routers mounted | Confirmed through the OpenAPI spec (78 paths, 89 operations) |
| `alembic heads` | Single head `6fb30639d5f4` |
| `GET /caregiver/verification-status` as a patient | **500 → 403** (before and after checked with dependency overrides) |
| Caregiver without a profile | → 404 |
| `docker build` + gunicorn/UvicornWorker boot inside the image | `/health` 200, OpenAPI 200, runs as the `app` user, image 828 MB |

**Not run:** `app/tests/*`. They call `http://localhost:8000` and the live Supabase database with hardcoded IDs and expired tokens, so running them would touch production data. A real test suite is a follow-up.

## Operate / roll back

- **Render — important.** `render.yaml` only takes effect if the service is managed as a Render Blueprint. If `fastapi-service` was created manually in the dashboard, update it there too:
  - Build command: `poetry install --only main --no-root --no-interaction`
  - Start command: `poetry run uvicorn app.main:app --host 0.0.0.0 --port 10000`
  - Environment variable: `POETRY_VERSION=2.2.1`
- **Render — secrets.** Production currently reads secrets from the committed `.env`. This change adds `.env` to `.gitignore` but does **not** untrack it, so deploys still see it. Before untracking it, copy every key into Render's Environment settings. That belongs to the secret-rotation task.
- **Roll back:** revert the merge commit. `requirements.txt` comes back and Render's old build command works again.

## Follow-ups

- [ ] Rotate all leaked secrets, move them to Render env vars, `git rm --cached .env`, and remove the hardcoded DB password from `config.py`, `alembic.ini` and `docker-compose.yml`
- [ ] Remove the committed `__pycache__/*.pyc` files (`git rm -r --cached '**/__pycache__'`)
- [ ] passlib → `pwdlib[bcrypt]`, python-jose → `PyJWT`, google-generativeai → `google-genai`
- [ ] Python 3.11 → 3.12 or 3.13
- [ ] Real pytest suite against a throwaway Postgres; add it to CI
- [ ] Clean up the ruff backlog (333 findings), then enforce the full ruleset and `ruff format --check`
- [ ] Upgrade Poetry to 2.5.x locally and in `POETRY_VERSION` / CI together
