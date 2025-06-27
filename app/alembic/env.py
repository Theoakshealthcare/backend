import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# Add the project root directory to Python's path
sys.path.append(str(Path(__file__).parent.parent))

# Import settings to get database URL
from core.config import settings

# Import Base and all models to ensure they are registered
from app.db.base import Base

# Dynamically import all model modules from the models directory
# This ensures all models are loaded and registered with SQLAlchemy
import importlib
import pkgutil

def import_models_recursively(package_name):
    """Recursively import all modules in a package to register all models with SQLAlchemy"""
    package = importlib.import_module(package_name)
    for _, name, is_pkg in pkgutil.iter_modules(package.__path__, package.__name__ + '.'):
        if is_pkg:
            import_models_recursively(name)
        else:
            importlib.import_module(name)

# Import all models from the models directory
try:
    import_models_recursively('app.db.models')
except ImportError as e:
    print(f"Warning: Error importing models: {e}")

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Override sqlalchemy.url with the actual database URL from settings
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set metadata to Base.metadata for autogeneration support
target_metadata = Base.metadata

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, 
            target_metadata=target_metadata,
            # Additional recommended settings
            compare_type=True,         # Compare column types
            compare_server_default=True, # Compare server defaults
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()