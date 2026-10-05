"""Alembic environment.

The database URL comes from app settings (DATABASE_URL), the same source the app uses; no
.env is loaded here (settings handles that, and the test suite disables it). A caller may pass
an open connection via ``config.attributes["connection"]`` (used by the migration tests).
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import app.db.models  # noqa: F401  (registers every model on Base.metadata)
from app.core.config import settings
from app.db.base import Base

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=settings.DATABASE_URL, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    # '%' must be escaped for configparser (URL-encoded passwords contain it).
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL.replace("%", "%%"))
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as conn:
        _configure(connection=conn)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
