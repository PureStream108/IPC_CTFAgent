from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from backend.persistence.database import sqlalchemy_database_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _migration_database_url() -> str:
    """Resolve the explicit Alembic target without losing env overrides.

    Runtime commands conventionally provide ``IPC_DATABASE_URL``. Standalone
    migration/import commands set ``sqlalchemy.url`` on the Alembic config,
    so use that value when no environment override is present.
    """

    configured = str(
        config.attributes.get("ipc_database_url", "")
    ).strip()
    if not configured:
        configured = os.environ.get("IPC_DATABASE_URL", "").strip()
    if not configured:
        configured = (config.get_main_option("sqlalchemy.url") or "").strip()
    return sqlalchemy_database_url(configured or None)


def run_migrations_offline() -> None:
    context.configure(
        url=_migration_database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_migration_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        lock_timeout_ms = int(os.environ.get("IPC_MIGRATION_LOCK_TIMEOUT_MS", "5000"))
        if lock_timeout_ms > 0:
            connection.exec_driver_sql(f"SET lock_timeout TO {lock_timeout_ms}")
            connection.commit()
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if hasattr(config, "get_main_option") and all(
    hasattr(context, attribute)
    for attribute in ("is_offline_mode", "configure", "begin_transaction", "run_migrations")
):
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()
