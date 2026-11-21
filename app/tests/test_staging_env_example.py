"""The two env example files must stay in step with Settings, and the deploy one with docker-compose.prod.yml.

.env.example is the development template (copy to .env). deploy/env.example is the template for .env.staging and
.env.production (then encrypted). A setting missing from either is a setting nobody knows exists; a key Settings does not
declare stops the app booting (extra="forbid" when it loads a .env file). Neither file carries comments.
"""

import ipaddress
import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
DEV_EXAMPLE = ROOT / ".env.example"
DEPLOY_EXAMPLE = ROOT / "deploy" / "env.example"
COMPOSE = ROOT / "docker-compose.prod.yml"
BOTH = [pytest.param(DEV_EXAMPLE, id=".env.example"), pytest.param(DEPLOY_EXAMPLE, id="deploy/env.example")]

# Read by docker compose only (the stack's name, port, subnet, the database service and DATABASE_URL), or by the container's
# boot script (GUNICORN_WORKERS): none of them is a Settings field. REDIS_MAXMEMORY is also compose-only.
COMPOSE_ONLY = {
    "POSTGRES_PASSWORD",
    "COMPOSE_PROJECT_NAME",
    "API_PORT",
    "DOCKER_SUBNET",
    "REDIS_MAXMEMORY",
    "GUNICORN_WORKERS",
}
# docker-compose.prod.yml builds these in `environment`, so the deploy example leaves them out.
SET_BY_COMPOSE = {"DATABASE_URL", "REDIS_URL"}

KEY_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def _lines(path):
    """(key, value) for every `KEY=value` line."""
    found = []
    for line in path.read_text().splitlines():
        match = KEY_LINE.match(line)
        if match:
            found.append((match.group(1), match.group(2)))
    return found


def _values(path):
    return dict(_lines(path))


@pytest.mark.parametrize("path", BOTH)
def test_the_file_has_no_comments(path):
    comments = [line for line in path.read_text().splitlines() if line.lstrip().startswith("#")]
    assert not comments, f"{path.name} must not carry comments: {comments[:3]}"


@pytest.mark.parametrize("path", BOTH)
def test_every_setting_is_named(path):
    named = {key for key, _ in _lines(path)}
    expected = set(Settings.model_fields) - (SET_BY_COMPOSE if path == DEPLOY_EXAMPLE else set())
    missing = sorted(expected - named)
    assert not missing, f"settings missing from {path.name}: {missing}"


@pytest.mark.parametrize("path", BOTH)
def test_names_nothing_settings_does_not_declare(path):
    allowed = set(Settings.model_fields) | (COMPOSE_ONLY if path == DEPLOY_EXAMPLE else set())
    unknown = sorted({key for key, _ in _lines(path)} - allowed)
    assert not unknown, f"keys in {path.name} that Settings would refuse: {unknown}"


@pytest.mark.parametrize("path", BOTH)
def test_no_key_is_listed_twice(path):
    keys = [key for key, _ in _lines(path)]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("path", BOTH)
def test_the_file_loads_as_an_env_file(path, monkeypatch, tmp_path):
    """Every line, as written, must be a valid Settings value (compose-only keys removed, compose-built URLs added)."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    body = [f"{key}={value}" for key, value in _lines(path) if key not in COMPOSE_ONLY]
    if path == DEPLOY_EXAMPLE:
        body += ["DATABASE_URL=postgresql://oakly:x@db:5432/oakly", "REDIS_URL=redis://:x@redis:6379/0"]
    env = tmp_path / "loaded.env"
    env.write_text("\n".join(body) + "\n")
    Settings(_env_file=env)


def test_compose_built_urls_are_not_in_the_deploy_example():
    assert not ({key for key, _ in _lines(DEPLOY_EXAMPLE)} & SET_BY_COMPOSE), "compose builds DATABASE_URL / REDIS_URL"


def test_the_deploy_example_is_the_staging_template(monkeypatch, tmp_path):
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    body = [f"{key}={value}" for key, value in _lines(DEPLOY_EXAMPLE) if key not in COMPOSE_ONLY]
    body += ["DATABASE_URL=postgresql://oakly:x@db:5432/oakly", "REDIS_URL=redis://:x@redis:6379/0"]
    env = tmp_path / "staging.env"
    env.write_text("\n".join(body) + "\n")
    s = Settings(_env_file=env)
    assert s.ENVIRONMENT == "staging"
    assert s.VOICE_NOTES_ENABLED is False
    assert s.SMTP_PORT == 465  # the code only speaks implicit TLS
    assert s.TRUSTED_PROXIES == ["172.29.0.0/24"]


def test_trusted_proxies_covers_the_compose_subnet():
    values = _values(DEPLOY_EXAMPLE)
    # The compose file takes the subnet from DOCKER_SUBNET (so two stacks can share a host) with the same default.
    assert "subnet: ${DOCKER_SUBNET:-" + values["DOCKER_SUBNET"] + "}" in COMPOSE.read_text()
    assert ipaddress.ip_network(values["DOCKER_SUBNET"]) == ipaddress.ip_network(values["TRUSTED_PROXIES"])


def test_the_example_names_the_staging_stack():
    """The deploy refuses an env file whose COMPOSE_PROJECT_NAME is not oakly-<environment>."""
    values = _values(DEPLOY_EXAMPLE)
    assert values["COMPOSE_PROJECT_NAME"] == "oakly-staging"
    assert values["API_PORT"] == "8000"


def test_the_compose_file_has_no_environment_specific_values():
    """One file serves both stacks: the project name, port and subnet come from .env, never from a literal."""
    text = COMPOSE.read_text()
    assert "name: ${COMPOSE_PROJECT_NAME:-oakly}" in text
    assert '"127.0.0.1:${API_PORT:-8000}:8000"' in text
    assert "image: ${API_IMAGE:?" in text
    assert "build:" not in text, "production never builds on the server: the image comes from GHCR by digest"


def test_the_documented_fernet_command_makes_a_valid_key():
    """The runbook tells the owner to run `openssl rand -base64 32 | tr '+/' '-_'`: that is a urlsafe base64 of 32 bytes."""
    import base64
    import os

    standard = base64.b64encode(os.urandom(32)).decode()
    Fernet(standard.replace("+", "-").replace("/", "_").encode())  # raises if the shape is wrong
