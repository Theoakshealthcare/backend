# Oaklycare Healthcare Provider Backend API

A FastAPI-based backend service for the Oaklycare healthcare provider mobile application.

## Features

- User authentication (register, login, password reset)
- Email verification with OTP
- Dockerized setup with PostgreSQL
- Database migrations with Alembic
- Comprehensive API documentation with Swagger UI

## Tech Stack

- **FastAPI**: Modern, fast web framework for building APIs
- **PostgreSQL**: Relational database for data storage
- **SQLAlchemy**: SQL toolkit and ORM
- **Alembic**: Database migration tool
- **Pydantic**: Data validation and settings management
- **JWT**: JSON Web Tokens for authentication
- **Docker & Docker Compose**: Containerization and orchestration
- **Jinja2**: Templating engine for email templates

## Project Structure

```
oaklycare-backend/
├── app/
│   ├── api/
│   │   ├── endpoints/
│   │   │   ├── auth.py
│   │   │   └── ...
│   │   └── api.py
│   ├── core/
│   │   ├── config.py
│   │   ├── security.py
│   │   └── ...
│   ├── db/
│   │   ├── base.py
│   │   ├── session.py
│   │   └── ...
│   ├── models/
│   │   ├── user.py
│   │   └── ...
│   ├── schemas/
│   │   ├── user.py
│   │   └── ...
│   ├── services/
│   │   ├── email.py
│   │   └── ...
│   ├── templates/
│   │   ├── emails/
│   │   │   ├── verification.html
│   │   │   └── password_reset.html
│   │   └── ...
│   └── main.py
├── migrations/
│   ├── versions/
│   ├── env.py
│   └── ...
├── scripts/
│   ├── start.sh
│   ├── migrate.sh
│   └── ...
├── tests/
│   ├── api/
│   ├── conftest.py
│   └── ...
├── .env.example
├── .gitignore
├── docker-compose.yml
├── Dockerfile
├── alembic.ini
└── requirements.txt
```

## Getting Started

Dependencies are managed with [Poetry](https://python-poetry.org/) (2.2.1). `pyproject.toml`
declares them and `poetry.lock` pins exact versions — always commit both together.

### Prerequisites

- Python 3.11 (see `.python-version`)
- Poetry 2.2.1 — `pipx install poetry==2.2.1`
- Docker (optional, for the container build)

### Local setup

```bash
git clone git@github.com:innocent98/backend.git
cd backend
cp .env.example .env            # then fill in real values
poetry env use python3.11
poetry install                  # creates .venv/ in the project (see poetry.toml)
poetry run alembic upgrade head
poetry run uvicorn app.main:app --reload
```

Swagger UI: http://localhost:8000/docs · OpenAPI: http://localhost:8000/api/v1/openapi.json

### Managing dependencies

| Task | Command |
|---|---|
| Add a runtime package | `poetry add <pkg>` |
| Add a dev-only package | `poetry add --group dev <pkg>` |
| Upgrade within constraints | `poetry update <pkg>` |
| Check lock is in sync | `poetry check --lock` |
| Install prod deps only | `poetry install --only main --no-root` |

### Checks (same as CI)

```bash
poetry check --lock
poetry run ruff check --select E9,F63,F7,F82 .
poetry run python -m scripts.ci_smoke
```

### Docker

```bash
docker build -t oaklycare-backend .
docker compose up
```
