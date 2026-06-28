import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _db_url() -> str:
    """Resolve the SQLite DB path from env (same logic as config/settings.py)."""
    db_path = os.getenv("LEDGERLENS_DB_PATH", "./ledgerlens.db")
    return f"sqlite:///{db_path}"


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url") or _db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    ini_section = config.get_section(config.config_ini_section, {})
    if not ini_section.get("sqlalchemy.url"):
        ini_section["sqlalchemy.url"] = _db_url()

    connectable = engine_from_config(
        ini_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
