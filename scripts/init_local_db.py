"""Development only: build an empty database from the models and stamp Alembic at head.

The Alembic history cannot build an empty database yet (two roots, tables created
outside migrations); a proper baseline replaces this in WP-E1. Refuses to run
when ENVIRONMENT=production or when the database already has tables.
"""

import os
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

import app.db.models  # noqa: F401  (registers every model)
from app.db.base import Base
from app.db.session import engine


def main() -> int:
    if os.environ.get("ENVIRONMENT", "").lower() == "production":
        print("init_local_db: refusing to run in production", file=sys.stderr)
        return 1
    if inspect(engine).get_table_names():
        print("init_local_db: database is not empty; nothing to do")
        return 0
    Base.metadata.create_all(engine)
    command.stamp(Config("alembic.ini"), "head")
    print("init_local_db: schema created from models and stamped at head")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
