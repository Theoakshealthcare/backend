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

### Prerequisites

- Docker and Docker Compose
- Python 3.9+

### Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/yourusername/oaklycare-backend.git
   cd oaklycare-backend