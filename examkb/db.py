"""The engine, the connection PRAGMAs, and the session factory.

Every connection this project opens comes from here, which is the only way the
PRAGMAs below can be a guarantee rather than a hope:

- **`foreign_keys = ON`.** SQLite defaults it *off*, per connection, every time.
  The plan's probe of this schema failed exactly here -- seed rows pointing at an
  `ingest_run` row that did not exist -- and a database that only enforces its keys
  in the sessions that remembered to ask is worse than one that does not enforce
  them at all, because the violations arrive silently and are found much later.
- **`journal_mode = WAL`.** Persistent on the file, set on connect anyway so a
  fresh database gets it without a separate step. It is also why `cp` is the wrong
  way to back this up (007 uses `VACUUM INTO`).
- **`busy_timeout`.** One writer, but `uvicorn` and a CLI job can both be running;
  a five-second wait is better than an immediate "database is locked".
- **`synchronous = NORMAL`.** The documented companion to WAL: durable across a
  process crash, which is the failure this journal actually faces.

It also takes SQLAlchemy's documented route around pysqlite's legacy transaction
handling: the driver is told not to open transactions by itself, and SQLAlchemy
emits `BEGIN` where it says it does. Left as it comes, the driver decides when a
transaction starts and ends -- DDL is not transactional, and `begin()` does not
mean what it says. Ingest (006) writes its whole projection in one transaction so
that a crash leaves the previous one intact, and that promise is only as good as
the transaction boundaries underneath it.

Alembic uses this module too (`alembic/env.py`), so `examkb db upgrade` runs with
foreign keys already enforced instead of enabling them afterwards.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from examkb.settings import get_settings

CONNECTION_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("foreign_keys", "ON"),
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("busy_timeout", "5000"),
)


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _apply_pragmas(dbapi_connection: Any, _record: Any) -> None:
    # pysqlite opens and commits transactions on its own schedule, which makes DDL
    # non-transactional and an explicit `begin()` a lie. Turning it off hands the
    # transaction boundaries back to SQLAlchemy, which emits them in `_begin` below.
    dbapi_connection.isolation_level = None

    cursor = dbapi_connection.cursor()
    try:
        for pragma, value in CONNECTION_PRAGMAS:
            cursor.execute(f"PRAGMA {pragma} = {value}")
            cursor.fetchall()  # journal_mode returns a row; leaving it unread wedges it
    finally:
        cursor.close()


def _begin(connection: Any) -> None:
    connection.exec_driver_sql("BEGIN")


def database_path(url: str) -> Path | None:
    """The file behind a SQLite URL, or None for `:memory:` and other dialects."""
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    target = url[len(prefix) :]
    if not target or target == ":memory:":
        return None
    return Path(target)


def new_engine(url: str | None = None, *, echo: bool = False, create_parent: bool = True) -> Engine:
    """An engine with the PRAGMAs attached. `url` defaults to the configured one."""
    url = url or get_settings().database_url
    if create_parent:
        path = database_path(url)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
    engine = sa.create_engine(url, echo=echo, future=True)
    if _is_sqlite(url):
        event.listen(engine, "connect", _apply_pragmas)
        event.listen(engine, "begin", _begin)
    return engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """The process-wide engine for the configured database."""
    return new_engine()


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transaction: commit on success, roll back on anything else."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def pragma(engine: Engine, name: str) -> Any:
    """Read one PRAGMA on a fresh connection. Used by the tests and by 047."""
    with engine.connect() as connection:
        return connection.exec_driver_sql(f"PRAGMA {name}").scalar()
