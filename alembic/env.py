"""Alembic's entry point, wired to this project's settings and engine.

Two things it deliberately does not do: construct a database URL (that is
`examkb/settings.py`, so `DATABASE_URL` keeps working) and open a plain
connection (that is `examkb/db.py`, so a migration runs with
`PRAGMA foreign_keys = ON` already set rather than enabling it afterwards).

`render_as_batch` is on because SQLite cannot ALTER a column in place: any later
migration that changes one needs Alembic to rebuild the table, and finding that
out in the migration that needs it is finding out too late.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from examkb import models
from examkb.db import new_engine
from examkb.settings import get_settings

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = models.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection", None)
    if connectable is not None:
        _run(connectable)
        return

    engine = new_engine(_url())
    try:
        with engine.connect() as connection:
            _run(connection)
    finally:
        engine.dispose()


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
