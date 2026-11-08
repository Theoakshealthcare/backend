"""deploy/staging.env.example must stay in step with Settings, and with docker-compose.staging.yml.

The staging file is what an owner copies onto a server. A setting missing from it is a setting nobody knows
exists; a key Settings does not declare stops the app booting (extra="forbid" when it loads a .env file).
"""

import ipaddress
import re
from pathlib import Path

from cryptography.fernet import Fernet

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "deploy" / "staging.env.example"
COMPOSE = ROOT / "docker-compose.staging.yml"

# Read by docker compose only (the database service and DATABASE_URL), not by the app.
COMPOSE_ONLY = {"POSTGRES_PASSWORD"}
# Set by docker-compose.staging.yml `environment`, so the example only names them (commented out).
SET_BY_COMPOSE = {"DATABASE_URL", "REDIS_URL"}

KEY_LINE = re.compile(r"^(#\s)?([A-Z][A-Z0-9_]*)=(.*)$")


def _lines():
    """(is_active, key, value) for every `KEY=value` or `# KEY=value` line."""
    found = []
    for line in EXAMPLE.read_text().splitlines():
        match = KEY_LINE.match(line)
        if match:
            found.append((match.group(1) is None, match.group(2), match.group(3)))
    return found


def test_every_setting_is_named_in_the_example():
    named = {key for _, key, _ in _lines()}
    missing = sorted(set(Settings.model_fields) - named)
    assert not missing, f"settings missing from deploy/staging.env.example: {missing}"


def test_example_names_nothing_settings_does_not_declare():
    unknown = sorted({key for _, key, _ in _lines()} - set(Settings.model_fields) - COMPOSE_ONLY)
    assert not unknown, f"keys in deploy/staging.env.example that Settings would refuse: {unknown}"


def test_no_key_is_listed_twice():
    keys = [key for _, key, _ in _lines()]
    assert len(keys) == len(set(keys))


def test_compose_built_urls_are_not_active_in_the_example():
    active = {key for is_active, key, _ in _lines() if is_active}
    assert not (active & SET_BY_COMPOSE), "DATABASE_URL / REDIS_URL are built by compose: keep them commented out"


def test_example_loads_as_an_env_file(monkeypatch, tmp_path):
    """With the compose-only key removed and the two compose-built URLs added, the file is a valid Settings env file."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    body = [f"{key}={value}" for is_active, key, value in _lines() if is_active and key not in COMPOSE_ONLY]
    body += ["DATABASE_URL=postgresql://oakly:x@db:5432/oakly", "REDIS_URL=redis://:x@redis:6379/0"]
    env = tmp_path / "staging.env"
    env.write_text("\n".join(body) + "\n")
    s = Settings(_env_file=env)
    assert s.ENVIRONMENT == "staging"
    assert s.VOICE_NOTES_ENABLED is False
    assert s.SMTP_PORT == 465  # the code only speaks implicit TLS
    assert s.TRUSTED_PROXIES == ["172.29.0.0/24"]


def test_trusted_proxies_covers_the_compose_subnet():
    subnet = re.search(r"subnet:\s*(\S+)", COMPOSE.read_text()).group(1)
    trusted = dict((key, value) for is_active, key, value in _lines() if is_active)["TRUSTED_PROXIES"]
    assert ipaddress.ip_network(subnet) == ipaddress.ip_network(trusted)


def test_the_documented_fernet_command_makes_a_valid_key():
    """The example tells the owner to run `openssl rand -base64 32 | tr '+/' '-_'`: that is a urlsafe base64 of 32 bytes."""
    import base64
    import os

    standard = base64.b64encode(os.urandom(32)).decode()
    Fernet(standard.replace("+", "-").replace("/", "_").encode())  # raises if the shape is wrong
