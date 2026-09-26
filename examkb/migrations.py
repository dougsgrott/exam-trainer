"""The one module that knows about Alembic.

Everything else -- the CLI, the tests, 007's pre-migration backup gate, 047's
doctor, 008's startup check -- asks these functions instead of building an
`alembic.config.Config` of its own. That matters for one specific reason: a second
Config is a second place that decides which database is being migrated, and the
project has exactly one (`examkb/settings.py`, overridable with `DATABASE_URL`).
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from examkb.db import database_path, new_engine
from examkb.services.search import FTS_TABLES
from examkb.settings import get_settings

INI_NAME = "alembic.ini"
SCRIPT_DIR_NAME = "alembic"


def include_object(_object, name: str, type_: str, _reflected, _compare_to) -> bool:
    """What autogenerate is allowed to see. Everything except the FTS5 index.

    A virtual table and its five shadow tables are real rows in `sqlite_master`
    with no counterpart in `Base.metadata`, so without this filter every
    `alembic revision --autogenerate` writes a migration that drops search. It
    lives here rather than in `alembic/env.py` so that the drift test can use the
    same predicate the real thing uses instead of a copy of it that agrees today.
    """
    return not (type_ == "table" and name in FTS_TABLES)


class MigrationsNotFound(FileNotFoundError):
    """The migration scripts are not on disk, and no database can be built without them."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"missing {path}: run examkb from a checkout of the repository, "
            "where alembic/ lives beside pyproject.toml"
        )
        self.path = path


def script_location() -> Path:
    path = get_settings().repo_root / SCRIPT_DIR_NAME
    if not (path / "env.py").exists():
        raise MigrationsNotFound(path / "env.py")
    return path


def alembic_config(url: str | None = None) -> Config:
    """A Config pointed at this repo's scripts and at one explicit database URL."""
    settings = get_settings()
    ini = settings.repo_root / INI_NAME
    config = Config(str(ini) if ini.exists() else None)
    config.set_main_option("script_location", str(script_location()))
    config.set_main_option("sqlalchemy.url", url or settings.database_url)
    # `alembic upgrade` from the CLI should print what it did and nothing else;
    # re-running fileConfig here would also reset logging for the host process.
    config.attributes["configure_logger"] = False
    return config


def upgrade(revision: str = "head", url: str | None = None) -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(revision: str, url: str | None = None) -> None:
    command.downgrade(alembic_config(url), revision)


def stamp(revision: str, url: str | None = None) -> None:
    command.stamp(alembic_config(url), revision)


def current_revision(url: str | None = None) -> str | None:
    """The revision the database is at, or None for a missing or unmigrated one.

    Reads the database rather than the scripts, so it answers for a database whose
    revision is not in `alembic/versions/` at all -- which is exactly the case 047
    needs to report rather than crash on. A SQLite file that does not exist is
    never opened: connecting would create an empty database, and a *reporting*
    command that quietly creates the thing it was asked about is a bad answer.
    """
    url = url or get_settings().database_url
    path = database_path(url)
    if path is not None and not path.exists():
        return None

    engine = new_engine(url, create_parent=False)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def head_revision(url: str | None = None) -> str | None:
    return ScriptDirectory.from_config(alembic_config(url)).get_current_head()


def revisions(url: str | None = None) -> list[tuple[str, str]]:
    """Every revision, newest first, as `(revision, description)`."""
    script = ScriptDirectory.from_config(alembic_config(url))
    return [(rev.revision, rev.doc.splitlines()[0]) for rev in script.walk_revisions()]


def is_up_to_date(url: str | None = None) -> bool:
    return current_revision(url) == head_revision(url)
