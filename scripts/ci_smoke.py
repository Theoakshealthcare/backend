"""Boot the FastAPI app in-process and check that it serves its schema.

Runs in CI without a database: nothing here opens a DB connection.
"""

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient

from app.core.security import get_password_hash, verify_password
from app.main import app

client = TestClient(app)

assert client.get("/health").status_code == 200

spec = client.get("/api/v1/openapi.json")
assert spec.status_code == 200
operations = sum(len(methods) for methods in spec.json()["paths"].values())
assert operations > 0, "OpenAPI spec has no operations"

hashed = get_password_hash("ci-password")
assert verify_password("ci-password", hashed)

# Two migrations branching from the same parent break `alembic upgrade head` (and so every deploy): catch it without a database.
heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
assert len(heads) == 1, f"Alembic has {len(heads)} heads, expected 1: {heads}"

print(f"smoke OK: {operations} API operations, password hashing works, one Alembic head ({heads[0]})")
