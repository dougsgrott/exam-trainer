"""The first thing a new user sees.

`examkb serve` before `examkb db upgrade` used to give a stack trace on four of six
pages. Only `/` survived, because only `/` was built to ask about the database
before querying it — 008's banner already knew how to say "the database has no
schema, run `examkb db upgrade`", and the other pages simply never asked.

So every page is walked through the three states that come before a usable corpus:
no file, no schema, nothing ingested. And the last test here is the one that keeps
the fix honest: a **dropped table after the schema exists** is a fault, and must
still be one. A blanket `except OperationalError` in each service would have turned
every future bug into a blank page, which is worse than the stack trace it replaced.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from starlette.testclient import TestClient

from examkb import db as db_module
from examkb import migrations
from examkb import status as status_module
from examkb.ingest import ingest
from sqlalchemy.orm import Session

PAGES = ["/", "/healthz", "/browse", "/exam", "/results"]


@pytest.fixture(autouse=True)
def clean_caches():
    db_module.engine_for.cache_clear()
    db_module.forget_projection_ready()
    status_module.forget_corpus_fingerprint()
    yield
    db_module.engine_for.cache_clear()
    db_module.forget_projection_ready()
    status_module.forget_corpus_fingerprint()


def client_for(url: str, kb: Path, *, raising: bool = False) -> TestClient:
    from examkb.web.app import create_app

    app = create_app(
        database_url=url, status_provider=lambda: status_module.projection_status(url=url, kb=kb)
    )
    return TestClient(app, raise_server_exceptions=raising, follow_redirects=True)


# ---------------------------------------------------------------- the three states


@pytest.mark.parametrize("path", PAGES)
def test_no_database_file_at_all(path: str, tmp_path: Path, tmp_kb: Path) -> None:
    """The criterion. This is a fresh checkout, `serve` before anything else."""
    client = client_for(f"sqlite:///{tmp_path / 'absent.db'}", tmp_kb)

    assert client.get(path).status_code == 200
    assert not (tmp_path / "absent.db").exists(), "looking must not create one"


@pytest.mark.parametrize("path", PAGES)
def test_a_database_with_no_schema(path: str, tmp_path: Path, tmp_kb: Path) -> None:
    """An empty file is a valid SQLite database with no tables in it."""
    empty = tmp_path / "empty.db"
    empty.touch()

    assert client_for(f"sqlite:///{empty}", tmp_kb).get(path).status_code == 200


@pytest.mark.parametrize("path", PAGES)
def test_a_migrated_database_with_nothing_ingested(path: str, tmp_db: Engine, tmp_kb: Path) -> None:
    """The criterion: an empty projection queries fine and renders an empty page."""
    assert client_for(str(tmp_db.url), tmp_kb).get(path).status_code == 200


def test_the_pages_are_empty_rather_than_wrong(tmp_path: Path, tmp_kb: Path) -> None:
    client = client_for(f"sqlite:///{tmp_path / 'absent.db'}", tmp_kb)

    browse = client.get("/browse").text
    assert "Nothing matches" in browse
    assert 'class="hit"' not in browse

    exam = client.get("/exam").text
    assert "ccao-f" not in exam and "ccar-p" not in exam

    assert "No submitted attempts yet" in client.get("/results").text


# -------------------------------------------------------------- the banner explains


def test_the_banner_names_the_command_for_each_state(tmp_path: Path, tmp_kb: Path) -> None:
    """The criterion: it says which of the three, and what to run."""
    absent = client_for(f"sqlite:///{tmp_path / 'absent.db'}", tmp_kb).get("/browse").text
    assert "No database yet" in absent
    assert "examkb db upgrade" in absent

    empty = tmp_path / "empty.db"
    empty.touch()
    no_schema = client_for(f"sqlite:///{empty}", tmp_kb).get("/browse").text
    assert "no schema" in no_schema
    assert "examkb db upgrade" in no_schema


def test_a_migrated_but_uningested_database_says_so(tmp_db: Engine, tmp_kb: Path) -> None:
    body = client_for(str(tmp_db.url), tmp_kb).get("/browse").text

    assert "Nothing ingested yet" in body
    assert "examkb ingest" in body


def test_with_no_corpus_it_says_that_instead(tmp_db: Engine, tmp_path: Path) -> None:
    body = client_for(str(tmp_db.url), tmp_path / "no-kb-here").get("/").text

    assert "No corpus" in body
    assert "make pipeline" in body


# ------------------------------------------------------------- the predicate itself


def test_projection_ready_knows_the_difference(tmp_path: Path) -> None:
    missing = f"sqlite:///{tmp_path / 'nope.db'}"
    assert db_module.projection_ready(missing) is False

    empty = tmp_path / "empty.db"
    empty.touch()
    assert db_module.projection_ready(f"sqlite:///{empty}") is False

    migrated = tmp_path / "migrated.db"
    migrations.upgrade(url=f"sqlite:///{migrated}")
    db_module.engine_for.cache_clear()
    db_module.forget_projection_ready()
    assert db_module.projection_ready(f"sqlite:///{migrated}") is True


def test_looking_does_not_create_the_database(tmp_path: Path) -> None:
    target = tmp_path / "untouched.db"

    assert db_module.projection_ready(f"sqlite:///{target}") is False
    assert not target.exists()


# --------------------------------------------------- a fault is still a fault


def test_a_dropped_table_after_the_schema_exists_is_still_an_error(
    tmp_db: Engine, tmp_kb: Path
) -> None:
    """The criterion that keeps the fix from becoming a blanket except.

    `question` is there, so this is not a first run -- it is a database somebody
    has broken. The page must fail loudly rather than render an empty list, or
    every future bug becomes a silently blank page.
    """
    import sqlite3

    database = Path(tmp_db.url.database)
    with Session(tmp_db) as session:
        ingest(session, tmp_kb)
        session.commit()
    tmp_db.dispose()

    # Through plain sqlite3, which is what somebody with a prompt and a bad idea
    # actually has. `question` still points at `exam`; the app is simply broken now.
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE exam")
        connection.commit()
    finally:
        connection.close()
    db_module.engine_for.cache_clear()
    db_module.forget_projection_ready()

    url = f"sqlite:///{database}"
    assert client_for(url, tmp_kb).get("/browse").status_code == 500

    db_module.engine_for.cache_clear()
    db_module.forget_projection_ready()
    with pytest.raises(sa.exc.OperationalError, match="no such table: exam"):
        client_for(url, tmp_kb, raising=True).get("/browse")


def test_a_working_database_is_not_affected(real_db: Engine, tmp_kb: Path) -> None:
    """The guard must not cost the normal path anything or change what it shows."""
    client = client_for(str(real_db.url), tmp_kb)

    body = client.get("/browse").text
    assert "549" in body
    assert 'class="hit"' in body
