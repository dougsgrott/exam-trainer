"""The schema's own invariants, asserted against a real migrated database.

Every test here runs on `tmp_db` -- built by `examkb db upgrade`, not by
`create_all` -- because most of what is being checked (the views, the triggers,
the bootstrap row, the constraint names) exists only in the migration.

Several of these belong to later issues and are smoke tests here on purpose: 013
owns the snapshot trigger and 041 owns the chat gate, but a trigger that nobody
exercises until phase 7 is a trigger that has been broken since phase 1.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from examkb import db, migrations, models
from examkb.db import new_engine, pragma
from examkb.models import JOURNAL, PROJECTION, corpus, journal, views
from examkb.services.search import FTS_TABLES

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


# ------------------------------------------------------------------------------ helpers


def objects(engine: sa.Engine, kind: str) -> list[str]:
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = ? ORDER BY name", (kind,)
        ).fetchall()
    return [row[0] for row in rows]


def schema_sql(engine: sa.Engine) -> list[str]:
    with engine.connect() as connection:
        rows = connection.exec_driver_sql("SELECT sql FROM sqlite_master").fetchall()
    return sorted(row[0] or "" for row in rows)


def seed_certification(session: Session, slug: str = "ccao-f") -> corpus.Certification:
    """A certification attributed to the bootstrap run -- the historical failure case."""
    certification = corpus.Certification(
        id=slug,
        name="Claude Certified Associate - Foundations",
        vendor="anthropic",
        level="associate",
        ingest_run_id="bootstrap",
    )
    session.add(certification)
    session.commit()
    return certification


def seed_question(session: Session, question_id: str = "ccao-f/exam-01/q001") -> corpus.Question:
    session.add(
        corpus.Question(
            id=question_id,
            content_hash="fd1c8863d839",
            certification_id="ccao-f",
            domain_label="Prompting and Task Execution",
            type="single_select",
            select_count=1,
            prompt_md="Which option is correct?",
            correct_labels=["A"],
            origin="vendor_dump",
            source_provider="udemy",
            ingest_run_id="bootstrap",
        )
    )
    session.commit()
    return session.get(corpus.Question, question_id)


def seed_attempt_item(session: Session, *, answered: bool = True, submitted: bool = True) -> int:
    attempt = journal.Attempt(
        certification_id="ccao-f",
        started_at=NOW,
        submitted_at=NOW + timedelta(minutes=30) if submitted else None,
        seed=20260102,
        item_count=1,
    )
    session.add(attempt)
    session.flush()
    item = journal.AttemptItem(
        attempt_id=attempt.id,
        position=1,
        question_id="ccao-f/exam-01/q001",
        snapshot_json={"prompt_md": "as displayed"},
        option_order=["C", "A", "D", "B"],
        selected_labels=["A"] if answered else None,
        answered_at=NOW + timedelta(minutes=1) if answered else None,
    )
    session.add(item)
    session.commit()
    return item.id


# ----------------------------------------------------- upgrade, foreign keys and the seed


def test_upgrade_produces_the_whole_schema(tmp_db: sa.Engine) -> None:
    """Amended by 009: the FTS5 index is six more rows in `sqlite_master`.

    A virtual table and its five shadow tables are counted apart from ours,
    because they are not ours -- FTS5 owns their shape, and asserting on it would
    be asserting on SQLite's internals.
    """
    tables = objects(tmp_db, "table")
    assert sorted(name for name in tables if name in FTS_TABLES) == sorted(FTS_TABLES)
    assert len([name for name in tables if name not in FTS_TABLES]) == 37  # 36 + alembic_version

    assert set(objects(tmp_db, "view")) == set(views.VIEW_NAMES)
    assert set(objects(tmp_db, "trigger")) == set(views.TRIGGER_NAMES)
    assert migrations.current_revision(str(tmp_db.url)) == migrations.head_revision()


def test_every_connection_enforces_foreign_keys(tmp_db: sa.Engine) -> None:
    """Off by default in SQLite, per connection, every time. Not here."""
    assert pragma(tmp_db, "foreign_keys") == 1
    assert pragma(tmp_db, "journal_mode") == "wal"


def test_migration_runs_with_foreign_keys_already_on(tmp_path, monkeypatch) -> None:
    """The migration itself, not only the sessions that come after it.

    This is the historical failure: a migration that seeds rows on a connection
    with SQLite's default `foreign_keys = off` inserts dangling references happily,
    and they surface much later as a database nobody can explain. The recorder
    below reads the PRAGMA on every connection Alembic actually opened -- after
    `new_engine` has configured it, which is the whole question.
    """
    observed: list[int] = []
    real_new_engine = db.new_engine

    def recording_engine(*args, **kwargs):
        engine = real_new_engine(*args, **kwargs)
        sa.event.listen(
            engine,
            "connect",
            lambda dbapi_connection, _record: observed.append(
                dbapi_connection.execute("PRAGMA foreign_keys").fetchone()[0]
            ),
        )
        return engine

    monkeypatch.setattr(db, "new_engine", recording_engine)
    url = f"sqlite:///{tmp_path / 'checked.db'}"
    migrations.upgrade(url=url)

    assert observed, "the migration opened no connection"
    assert set(observed) == {1}
    assert migrations.current_revision(url) == migrations.head_revision()


def test_bootstrap_ingest_run_exists(tmp_db: sa.Engine) -> None:
    with Session(tmp_db) as session:
        runs = session.scalars(sa.select(corpus.IngestRun)).all()
    assert [run.id for run in runs] == ["bootstrap"]
    assert runs[0].mode == "bootstrap"
    assert runs[0].started_at == datetime(1970, 1, 1, tzinfo=timezone.utc)


def test_the_historical_seed_failure_does_not_reproduce(tmp_session: Session) -> None:
    """7 seed rows pointed at an `ingest_run` that did not exist. Now one does."""
    certification = seed_certification(tmp_session)
    assert certification.ingest_run_id == "bootstrap"

    question = seed_question(tmp_session)
    assert question.origin == "vendor_dump"


def test_a_projection_row_with_no_ingest_run_is_rejected(tmp_session: Session) -> None:
    tmp_session.add(
        corpus.Certification(
            id="ghost", name="Ghost", vendor="nobody", ingest_run_id="run-that-never-ran"
        )
    )
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        tmp_session.commit()


def test_enum_checks_are_live(tmp_session: Session) -> None:
    seed_certification(tmp_session)
    tmp_session.add(
        corpus.Question(
            id="ccao-f/exam-01/q002",
            content_hash="deadbeef",
            certification_id="ccao-f",
            type="essay",  # not one of the two types this platform grades
            select_count=1,
            prompt_md="?",
            correct_labels=["A"],
            source_provider="udemy",
            ingest_run_id="bootstrap",
        )
    )
    with pytest.raises(IntegrityError, match="CHECK constraint failed"):
        tmp_session.commit()


# ------------------------------------------------------------------ up, down and up again


def test_downgrade_to_base_then_upgrade_is_byte_identical(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    engine = new_engine(url)
    try:
        migrations.upgrade(url=url)
        before = schema_sql(engine)

        migrations.downgrade("base", url=url)
        assert objects(engine, "table") == ["alembic_version"]
        assert objects(engine, "view") == []
        assert objects(engine, "trigger") == []

        migrations.upgrade(url=url)
        assert schema_sql(engine) == before
    finally:
        engine.dispose()


def test_models_match_the_migration(tmp_db: sa.Engine) -> None:
    """The drift test. Autogenerate has nothing to say about a migrated database.

    Through the same `include_object` the real `alembic/env.py` uses, so this
    asserts what `alembic revision --autogenerate` would actually produce. Without
    it the answer is six `remove_table` ops -- a migration that drops search.
    """
    with tmp_db.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "include_object": migrations.include_object}
        )
        assert compare_metadata(context, models.metadata) == []


def test_autogenerate_would_drop_search_without_the_filter(tmp_db: sa.Engine) -> None:
    """The filter is load-bearing; prove it by taking it away."""
    with tmp_db.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        dropped = {
            op[1].name for op in compare_metadata(context, models.metadata) if op[0] == "remove_table"
        }
    assert dropped == set(FTS_TABLES)


# ------------------------------------------------------------ the PROJECTION/JOURNAL split


def test_no_projection_table_uses_an_autoincrement_key(tmp_db: sa.Engine) -> None:
    """Content-derived keys, so `ingest --rebuild` reproduces every one of them."""
    for table in models.projection_tables():
        for column in table.primary_key.columns:
            assert isinstance(column.type, sa.Text), f"{table.name}.{column.name} is not TEXT"
            assert not column.autoincrement is True, f"{table.name}.{column.name} autoincrements"

    with tmp_db.connect() as connection:
        ddl = " ".join(row[0] or "" for row in connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'table'"
        ))
        # SQLite only creates this table once something declares AUTOINCREMENT.
        present = connection.exec_driver_sql(
            "SELECT count(*) FROM sqlite_master WHERE name = 'sqlite_sequence'"
        ).scalar()
    assert "AUTOINCREMENT" not in ddl.upper()
    assert present == 0


def test_no_projection_key_is_exempt_from_being_content_derived() -> None:
    """Including `ingest_run`, whose key is the corpus fingerprint itself (006)."""
    exempt = [
        cls.__tablename__ for cls in models.mapped_classes(PROJECTION) if not cls.content_keyed
    ]
    assert exempt == []


def test_journal_never_points_into_projection(tmp_db: sa.Engine) -> None:
    """A question that leaves `kb/` leaves the projection. The attempt is history."""
    projection = {table.name for table in models.projection_tables()}
    offenders = []
    with tmp_db.connect() as connection:
        for table in models.journal_tables():
            rows = connection.exec_driver_sql(f"PRAGMA foreign_key_list('{table.name}')")
            for row in rows.mappings():
                if row["table"] in projection:
                    offenders.append(f"{table.name}.{row['from']} -> {row['table']}")
    assert offenders == []


def test_the_table_class_split_is_enforced_not_documented() -> None:
    class MisplacedJournal:
        __tablename__ = "misplaced"
        table_class = JOURNAL

    MisplacedJournal.__module__ = corpus.__name__
    with pytest.raises(RuntimeError, match="belongs in"):
        models.enforce_table_class_split([MisplacedJournal])

    class Unclassified:
        __tablename__ = "unclassified"

    with pytest.raises(RuntimeError, match="neither Projection nor Journal"):
        models.enforce_table_class_split([Unclassified])


# ------------------------------------------------------------------------------ blueprints


@pytest.mark.parametrize("total", [80.0, 110.0])
def test_a_blueprint_whose_weights_miss_100_is_flagged_not_rejected(
    tmp_session: Session, total: float
) -> None:
    """AZ-104 bounds to 80/105 and SnowPro COF-C02 to 80/110, by design."""
    seed_certification(tmp_session)
    tmp_session.add(
        corpus.Blueprint(
            id=f"microsoft/az-104/{total}",
            certification_id="ccao-f",
            vendor="microsoft",
            weight_regime="range",
            weights_sum=total,
            weights_sum_to_100=False,
            max_depth=2,
            ingest_run_id="bootstrap",
        )
    )
    tmp_session.commit()

    stored = tmp_session.scalars(
        sa.select(corpus.Blueprint).where(corpus.Blueprint.weights_sum == total)
    ).one()
    assert stored.weights_sum_to_100 is False


def test_no_constraint_anywhere_mentions_a_weight_sum(tmp_db: sa.Engine) -> None:
    for statement in schema_sql(tmp_db):
        for line in statement.splitlines():
            if "CHECK" in line.upper():
                assert "100" not in line, line
                assert "sum" not in line.lower(), line


def test_a_weight_may_be_recorded_at_any_depth(tmp_session: Session) -> None:
    """Rejecting a nested weight is 017's job, in the parser, with a line number."""
    seed_certification(tmp_session)
    tmp_session.add(
        corpus.Blueprint(
            id="aws/saa-c03/1.0",
            certification_id="ccao-f",
            vendor="aws",
            weight_regime="exact",
            weights_sum=100.0,
            weights_sum_to_100=True,
            max_depth=4,
            ingest_run_id="bootstrap",
        )
    )
    tmp_session.add(
        corpus.BlueprintNode(
            id="aws/saa-c03/1.0#1.1.1.1",
            blueprint_id="aws/saa-c03/1.0",
            path="1.1.1.1",
            depth=4,
            ordinal=1,
            kind="Knowledge of:",
            label="Design secure access to AWS resources",
            weight_pct=3.0,
        )
    )
    tmp_session.commit()


# -------------------------------------------------------------------------------- triggers


def test_a_snapshot_cannot_be_rewritten_once_answered(tmp_session: Session) -> None:
    """013's invariant, asserted against SQLite rather than through the service."""
    seed_certification(tmp_session)
    seed_question(tmp_session)
    item_id = seed_attempt_item(tmp_session, answered=True)

    with pytest.raises(IntegrityError, match="immutable once the item is answered"):
        tmp_session.execute(
            sa.text("UPDATE attempt_item SET snapshot_json = '{}' WHERE id = :id"),
            {"id": item_id},
        )
    tmp_session.rollback()

    # The grade is not the snapshot: writing it is exactly what the grader does.
    tmp_session.execute(
        sa.text("UPDATE attempt_item SET is_correct = 1, credit = 1.0 WHERE id = :id"),
        {"id": item_id},
    )
    tmp_session.commit()


def test_an_unanswered_snapshot_may_still_be_written(tmp_session: Session) -> None:
    seed_certification(tmp_session)
    seed_question(tmp_session)
    item_id = seed_attempt_item(tmp_session, answered=False, submitted=False)
    tmp_session.execute(
        sa.text("UPDATE attempt_item SET snapshot_json = '{\"prompt_md\": \"re-rendered\"}' "
                "WHERE id = :id"),
        {"id": item_id},
    )
    tmp_session.commit()


def test_chat_is_closed_while_an_attempt_on_the_question_is_open(tmp_session: Session) -> None:
    """041's gate, keyed on the question -- so `/browse` cannot route around it."""
    seed_certification(tmp_session)
    seed_question(tmp_session)
    seed_attempt_item(tmp_session, answered=True, submitted=False)

    with pytest.raises(IntegrityError, match="chat is closed"):
        tmp_session.execute(
            sa.text(
                "INSERT INTO chat_thread (question_id, scope, created_at) "
                "VALUES ('ccao-f/exam-01/q001', 'browse', '2026-01-02 03:04:05.000000')"
            )
        )
    tmp_session.rollback()

    tmp_session.execute(
        sa.text("UPDATE attempt SET submitted_at = '2026-01-02 04:00:00.000000'")
    )
    tmp_session.execute(
        sa.text(
            "INSERT INTO chat_thread (question_id, scope, created_at) "
            "VALUES ('ccao-f/exam-01/q001', 'results', '2026-01-02 05:04:05.000000')"
        )
    )
    tmp_session.commit()
    assert tmp_session.scalar(sa.select(sa.func.count()).select_from(journal.ChatThread)) == 1


# ----------------------------------------------------------------------------------- views


def test_every_view_has_the_columns_the_app_queries(tmp_db: sa.Engine) -> None:
    with tmp_db.connect() as connection:
        for view in views.VIEWS:
            rows = connection.exec_driver_sql(f"PRAGMA table_info('{view.name}')").mappings()
            assert [row["name"] for row in rows] == [c.name for c in view.columns], view.name


def test_verification_is_derived_with_the_documented_precedence(tmp_session: Session) -> None:
    seed_certification(tmp_session)
    seed_question(tmp_session)

    def verification() -> str:
        return tmp_session.scalar(
            sa.text("SELECT verification FROM question_verification WHERE question_id = :q"),
            {"q": "ccao-f/exam-01/q001"},
        )

    assert verification() == "unverified"

    tmp_session.add(
        journal.VerificationEvent(
            question_id="ccao-f/exam-01/q001",
            level="ai_reviewed",
            source="ai_review",
            created_at=NOW,
        )
    )
    tmp_session.commit()
    assert verification() == "ai_reviewed"

    dispute = journal.Dispute(
        question_id="ccao-f/exam-01/q001",
        state="open",
        claim_md="The key says B; the docs say C.",
        created_at=NOW,
    )
    tmp_session.add(dispute)
    tmp_session.commit()
    assert verification() == "disputed"

    dispute.state = "rejected"
    dispute.resolved_at = NOW + timedelta(days=1)
    tmp_session.commit()
    assert verification() == "ai_reviewed"

    tmp_session.add(
        journal.VerificationEvent(
            question_id="ccao-f/exam-01/q001",
            level="known_bad",
            source="human",
            created_at=NOW + timedelta(days=2),
        )
    )
    tmp_session.add(
        journal.VerificationEvent(
            question_id="ccao-f/exam-01/q001",
            level="human_reviewed",
            source="human",
            created_at=NOW + timedelta(days=3),
        )
    )
    tmp_session.commit()
    # known_bad outranks a *later* human_reviewed: precedence, not recency.
    assert verification() == "known_bad"


def test_an_override_changes_the_current_key_and_nothing_else(tmp_session: Session) -> None:
    seed_certification(tmp_session)
    seed_question(tmp_session)

    def current() -> tuple:
        return tmp_session.execute(
            sa.text(
                "SELECT correct_labels, is_override FROM current_answer_key "
                "WHERE question_id = :q"
            ),
            {"q": "ccao-f/exam-01/q001"},
        ).one()

    assert current() == ('["A"]', 0)

    tmp_session.add(
        journal.Override(
            question_id="ccao-f/exam-01/q001",
            correct_labels=["C"],
            reason_md="Accepted dispute.",
            created_at=NOW,
            effective_from=NOW,
        )
    )
    tmp_session.commit()
    assert current() == ('["C"]', 1)

    stored = tmp_session.get(corpus.Question, "ccao-f/exam-01/q001")
    assert stored.correct_labels == ["A"], "the projection's key is never rewritten"


def test_current_mark_is_the_latest_of_an_append_only_history(tmp_session: Session) -> None:
    for index, value in enumerate(("known", "unsure", "flagged")):
        tmp_session.add(
            journal.Mark(
                question_id="ccao-f/exam-01/q001",
                value=value,
                source="ui",
                created_at=NOW + timedelta(minutes=index),
            )
        )
    tmp_session.commit()

    rows = tmp_session.execute(sa.text("SELECT question_id, value FROM current_mark")).all()
    assert rows == [("ccao-f/exam-01/q001", "flagged")]
    assert tmp_session.scalar(sa.select(sa.func.count()).select_from(journal.Mark)) == 3


def test_first_exposure_counts_a_question_once(tmp_session: Session) -> None:
    seed_certification(tmp_session)
    seed_question(tmp_session)
    seed_attempt_item(tmp_session, answered=True, submitted=True)
    seed_attempt_item(tmp_session, answered=True, submitted=True)

    rows = tmp_session.execute(sa.text("SELECT question_id FROM first_exposure_response")).all()
    assert rows == [("ccao-f/exam-01/q001",)]


def test_an_abandoned_attempt_is_not_a_first_exposure(tmp_session: Session) -> None:
    seed_certification(tmp_session)
    seed_question(tmp_session)
    seed_attempt_item(tmp_session, answered=True, submitted=True)
    tmp_session.execute(sa.text("UPDATE attempt SET abandoned_at = submitted_at"))
    tmp_session.commit()

    assert tmp_session.execute(sa.text("SELECT * FROM first_exposure_response")).all() == []


# ------------------------------------------------------------------------------- integrity


def test_a_migrated_database_is_intact(tmp_db: sa.Engine) -> None:
    with tmp_db.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []


def test_sqlite_is_new_enough_for_this_schema() -> None:
    """The plan probed on 3.45.1; FTS5 arrives in 009 and needs a modern build."""
    assert tuple(int(part) for part in sqlite3.sqlite_version.split(".")) >= (3, 35, 0)
