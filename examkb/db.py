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


@lru_cache(maxsize=8)
def engine_for(url: str) -> Engine:
    """A cached engine for one URL, for read paths that are handed a database.

    `get_engine()` is the process-wide one for the configured database and is what
    the app runs on. This exists for the things that are *told* which database to
    look at -- the projection status (008), the pages (010), and every test that
    points them at a copy in `tmp_path` -- so that a page load does not build an
    engine and a connection pool each time it renders.

    `create_parent=False`: being asked about a database that does not exist is a
    state to report, not a file to create.
    """
    return new_engine(url, create_parent=False)


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


# Once a database has a schema it does not lose one, so "yes" is remembered and
# "no" is not: a server started before `examkb db upgrade` begins working the
# moment the migration lands, without a restart, and every page after the first
# costs nothing. A schema that *does* vanish is the fault case -- it raises, which
# is what `test_a_dropped_table_after_the_schema_exists_is_still_an_error` wants.
_READY: set[str] = set()


def forget_projection_ready() -> None:
    """Drop the readiness cache. For tests, and for anything that just migrated."""
    _READY.clear()


def projection_ready(url: str | None = None) -> bool:
    """Can a page query the projection, or is this a database before its first use?

    Three states come before "yes": the file does not exist, it exists with no
    schema, and it has a schema but nothing has been ingested. Only the first two
    are this function's business -- an empty projection queries perfectly well and
    renders an empty page.

    It exists so the services ask **once**, rather than each growing its own
    `except OperationalError`. That distinction matters: a missing table before the
    first migration is a state, and a missing table afterwards is a fault, and a
    blanket try/except in every service would quietly turn the second into the
    first.
    """
    url = url or get_settings().database_url
    if url in _READY:
        return True

    path = database_path(url)
    if path is not None and not path.exists():
        return False
    try:
        with engine_for(url).connect() as connection:
            found = connection.exec_driver_sql(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'question'"
            ).scalar()
    except sa.exc.SQLAlchemyError:
        # A file that cannot be opened at all is not a first run; `status.py`
        # reports it and the banner says so.
        return False

    if found:
        _READY.add(url)
    return bool(found)


def session_for(url: str | None = None) -> Session:
    """A session on `url`, or on the configured database when `url` is None.

    The services open their own sessions (the web layer may not), and both of them
    need the same two-line decision about which engine that is. One copy.
    """
    return Session(engine_for(url) if url else get_engine(), expire_on_commit=False)


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
