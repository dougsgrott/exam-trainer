"""Ingest: the projection is a cache of `kb/`, and the journal is untouchable.

The two halves of that sentence are the two invariants this file exists for. The
first is proved by rebuilding from scratch and comparing *keys*, not just rows --
a projection whose identifiers move on every rebuild is not a cache, it is a
second source of truth with extra steps. The second is proved by putting attempts,
marks and disputes in the database and asserting a rebuild does not so much as
touch them.

Most tests run on `tmp_kb`'s three questions. The ones that need the real 549 are
here because the numbers they assert -- 5490 fields, 78, 12 -- are the corpus's own
and a fake cannot stand in for them; they read the committed `kb/`, never `data/`,
so they cost tenths of a second rather than the `slow` marker.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import REPO_ROOT, write_kb
from sqlalchemy.orm import Session

from examkb import ingest as ingest_module
from examkb import models, queries
from examkb.ingest import IngestError, ingest, kb_fingerprint, verify
from examkb.models import corpus, journal

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
REAL_KB = REPO_ROOT / "kb"


# ------------------------------------------------------------------------------ helpers


def ingested(session: Session, kb: Path, **kwargs):
    result = ingest(session, kb, **kwargs)
    session.commit()
    return result


def all_keys(session: Session) -> dict[str, list[tuple]]:
    """Every primary key in the projection, table by table."""
    keys = {}
    for name in ingest_module.INGESTED_TABLES:
        table = models.metadata.tables[name]
        columns = list(table.primary_key.columns)
        rows = session.execute(sa.select(*columns).order_by(*columns)).all()
        keys[name] = [tuple(row) for row in rows]
    return keys


def dump(engine: sa.Engine) -> str:
    raw = engine.raw_connection()
    try:
        return "\n".join(raw.driver_connection.iterdump())
    finally:
        raw.close()


def journal_snapshot(session: Session) -> dict[str, list[tuple]]:
    snapshot = {}
    for table in models.journal_tables():
        rows = session.execute(sa.select(table).order_by(*table.primary_key.columns)).all()
        snapshot[table.name] = [tuple(row) for row in rows]
    return snapshot


def seed_journal(session: Session, question_id: str) -> None:
    """A mark, an attempt with an answered item, and a dispute -- all unregenerable."""
    session.add(journal.Mark(question_id=question_id, value="known", source="ui", created_at=NOW))
    attempt = journal.Attempt(
        certification_id="mini-a", started_at=NOW, submitted_at=NOW, seed=1, item_count=1
    )
    session.add(attempt)
    session.flush()
    session.add(
        journal.AttemptItem(
            attempt_id=attempt.id,
            position=1,
            question_id=question_id,
            snapshot_json={"prompt_md": "as displayed"},
            option_order=["B", "A", "D", "C"],
            selected_labels=["A"],
            answered_at=NOW,
            is_correct=True,
            credit=1.0,
        )
    )
    session.add(
        journal.Dispute(
            question_id=question_id, state="open", claim_md="The key looks wrong.", created_at=NOW
        )
    )
    session.commit()


# ------------------------------------------------------------------- the real corpus


def test_the_real_corpus_projects_completely(tmp_session: Session) -> None:
    result = ingested(tmp_session, REAL_KB)

    assert result.questions == 549
    counts = queries.corpus_counts(tmp_session)
    assert (counts.questions, counts.options, counts.citations) == (549, 2196, 975)
    assert counts.certifications == 2
    assert counts.exams == 9
    # The nine mirror hosts collapse: 174 raw URLs, 145 pages.
    assert counts.references == 145


def test_verify_round_trips_every_text_field(tmp_session: Session) -> None:
    ingested(tmp_session, REAL_KB)
    result = verify(tmp_session, REAL_KB)

    assert result.fields_checked == 5490
    assert result.questions_checked == 549
    assert result.ok
    assert "checked 5490 text fields across 549 questions" in result.summary()


def test_verify_catches_a_projection_that_drifted(tmp_session: Session) -> None:
    ingested(tmp_session, REAL_KB)
    tmp_session.execute(
        sa.text("UPDATE question SET prompt_md = 'tampered' WHERE id = :id"),
        {"id": "ccao-f/exam-01/q001"},
    )
    tmp_session.commit()

    result = verify(tmp_session, REAL_KB)
    assert not result.ok
    assert result.mismatches == ["ccao-f/exam-01/q001: prompt_md"]
    assert result.fields_checked == 5490


def test_per_domain_counts_match_the_manifest(tmp_session: Session) -> None:
    ingested(tmp_session, REAL_KB)
    manifest = json.loads((REAL_KB / "manifest.json").read_text(encoding="utf-8"))

    counts = {
        f"{certification} / {domain}": count
        for (certification, domain), count in queries.counts_by_domain(tmp_session).items()
    }
    assert counts == manifest["questions_by_domain"]
    assert counts["ccao-f / Output Evaluation and Validation"] == 78
    assert counts["ccar-p / Developer Productivity & Operational Enablement"] == 12


def test_vendor_domain_strings_are_stored_byte_identical(tmp_session: Session) -> None:
    """Both of the vendor's spellings of "governance", exactly as it wrote them."""
    ingested(tmp_session, REAL_KB)
    labels = set(tmp_session.scalars(sa.select(corpus.Domain.label)).all())

    assert "Governance, Risk, and Responsible Use" in labels
    assert "Governance, Safety & Risk Management" in labels


# ------------------------------------------------------------------- keys and rebuilds


def test_rebuild_reproduces_every_primary_key(tmp_db: sa.Engine, tmp_kb: Path) -> None:
    with Session(tmp_db) as session:
        ingested(session, tmp_kb)
        before = all_keys(session)

        ingested(session, tmp_kb, rebuild=True)
        after = all_keys(session)

    assert after == before
    assert before["question"] == [
        ("mini-a/exam-01/q001",),
        ("mini-a/exam-01/q002",),
        ("mini-b/exam-01/q001",),
    ]
    assert ("mini-a/exam-01/q001#A",) in before["question_option"]


def test_rebuild_twice_produces_an_identical_database(tmp_db: sa.Engine, tmp_kb: Path) -> None:
    """Byte-identical, run log included -- re-projecting an unchanged corpus is a no-op."""
    with Session(tmp_db) as session:
        ingested(session, tmp_kb, rebuild=True)
    first = dump(tmp_db)

    with Session(tmp_db) as session:
        ingested(session, tmp_kb, rebuild=True)
    second = dump(tmp_db)

    assert hashlib.sha256(first.encode()).hexdigest() == hashlib.sha256(second.encode()).hexdigest()


def test_an_unchanged_corpus_writes_nothing(tmp_session: Session, tmp_kb: Path) -> None:
    ingested(tmp_session, tmp_kb)
    again = ingested(tmp_session, tmp_kb)

    assert not again.changed
    assert "nothing written" in again.summary()


def test_the_run_id_is_the_corpus_fingerprint(tmp_session: Session, tmp_kb: Path) -> None:
    result = ingested(tmp_session, tmp_kb)

    assert result.run_id == kb_fingerprint(tmp_kb)
    assert ingest_module.projection_fingerprint(tmp_session) == result.run_id
    assert not ingest_module.is_stale(tmp_session, tmp_kb)

    run = queries.current_ingest_run(tmp_session)
    assert run.question_count == 3
    assert run.shard_count == 1


def test_a_changed_corpus_is_stale_until_re_ingested(
    tmp_session: Session, tmp_path: Path, mini_questions
) -> None:
    kb = write_kb(tmp_path / "kb", mini_questions)
    ingested(tmp_session, kb)
    assert not ingest_module.is_stale(tmp_session, kb)

    edited = json.loads(json.dumps(mini_questions))
    edited[0]["prompt_md"] = "A different prompt entirely."
    write_kb(kb, edited)

    assert ingest_module.is_stale(tmp_session, kb)
    result = ingested(tmp_session, kb)
    assert result.changes["question"].updated == 1
    assert result.changes["question"].inserted == 0
    assert not ingest_module.is_stale(tmp_session, kb)


# ----------------------------------------------------------------------------- deletion


def test_a_question_that_leaves_kb_leaves_the_projection(
    tmp_session: Session, tmp_path: Path, mini_questions
) -> None:
    kb = write_kb(tmp_path / "kb", mini_questions)
    ingested(tmp_session, kb)
    doomed = mini_questions[1]["id"]
    seed_journal(tmp_session, doomed)

    write_kb(kb, [question for question in mini_questions if question["id"] != doomed])
    result = ingested(tmp_session, kb)

    assert result.changes["question"].deleted == 1
    assert tmp_session.get(corpus.Question, doomed) is None
    assert (
        tmp_session.scalar(
            sa.select(sa.func.count())
            .select_from(corpus.QuestionOption)
            .where(corpus.QuestionOption.question_id == doomed)
        )
        == 0
    )

    # The attempt is history. It is not a foreign key cascade.
    assert tmp_session.scalar(
        sa.select(sa.func.count())
        .select_from(journal.AttemptItem)
        .where(journal.AttemptItem.question_id == doomed)
    ) == 1
    assert tmp_session.scalar(
        sa.select(sa.func.count()).select_from(journal.Mark).where(journal.Mark.question_id == doomed)
    ) == 1
    assert tmp_session.scalar(
        sa.select(sa.func.count())
        .select_from(journal.Dispute)
        .where(journal.Dispute.question_id == doomed)
    ) == 1


# ------------------------------------------------------------------ the journal is safe


def test_ingest_never_writes_a_journal_row(tmp_session: Session, tmp_kb: Path) -> None:
    ingested(tmp_session, tmp_kb)
    seed_journal(tmp_session, "mini-a/exam-01/q001")
    before = journal_snapshot(tmp_session)
    assert sum(len(rows) for rows in before.values()) == 4

    ingested(tmp_session, tmp_kb, rebuild=True)

    assert journal_snapshot(tmp_session) == before


def test_no_journal_table_is_in_the_write_set() -> None:
    """The mirror invariant: nothing in the journal is derivable from `kb/`."""
    written = set(ingest_module.INGESTED_TABLES)
    projection = {table.name for table in models.projection_tables()}
    journal_names = {table.name for table in models.journal_tables()}

    assert written <= projection
    assert written.isdisjoint(journal_names)
    # Tables a later issue projects from its own files, and which ingest does not
    # write yet: blueprints (017), annotations and taxonomy (027).
    assert projection - written == {
        "blueprint",
        "blueprint_domain_map",
        "blueprint_node",
        "blueprint_source",
        "format_profile",
        "question_annotation",
        "question_taxon",
        "taxon",
        "taxon_scheme",
    }


# ------------------------------------------------------------------------ crash safety


def test_a_crash_mid_ingest_changes_nothing(
    tmp_db: sa.Engine, tmp_path: Path, mini_questions, monkeypatch
) -> None:
    """One transaction: the projection is either the old one or the new one."""
    kb = write_kb(tmp_path / "kb", mini_questions)
    with Session(tmp_db) as session:
        ingested(session, kb)
        before = all_keys(session)

    write_kb(kb, mini_questions[:1])

    real_upserts = ingest_module.apply_upserts
    calls = {"n": 0}

    def explode(session, plan, run_id):
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("killed mid-ingest")
        return real_upserts(session, plan, run_id)

    monkeypatch.setattr(ingest_module, "apply_upserts", explode)
    with Session(tmp_db) as session:
        with pytest.raises(RuntimeError, match="killed mid-ingest"):
            ingest(session, kb)
        session.rollback()

    with Session(tmp_db) as session:
        assert all_keys(session) == before
        assert queries.question_count(session) == 3


@pytest.mark.slow
def test_a_killed_process_leaves_a_whole_projection(tmp_path: Path, mini_questions) -> None:
    """SIGKILL a real ingest of the real corpus over a small existing projection.

    The assertion is not "the kill landed mid-write" -- that is a race nobody can
    pin down -- it is that whatever survives is **one whole projection**: the three
    questions that were there, or the 549 that were arriving, never a blend.
    """
    database = tmp_path / "examkb.db"
    kb = write_kb(tmp_path / "kb", mini_questions)
    environment = {**os.environ, "DATABASE_URL": f"sqlite:///{database}"}

    subprocess.run(
        [sys.executable, "-m", "examkb.cli", "db", "upgrade"],
        cwd=REPO_ROOT, env=environment, check=True, capture_output=True,
    )
    subprocess.run(
        [sys.executable, "-m", "examkb.cli", "ingest", "--kb", str(kb), "--quiet"],
        cwd=REPO_ROOT, env=environment, check=True, capture_output=True,
    )

    process = subprocess.Popen(
        [sys.executable, "-m", "examkb.cli", "ingest", "--rebuild", "--quiet"],
        cwd=REPO_ROOT, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.25)
    process.send_signal(signal.SIGKILL)
    process.wait(timeout=10)

    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    questions = connection.execute("SELECT count(*) FROM question").fetchone()[0]
    options = connection.execute("SELECT count(*) FROM question_option").fetchone()[0]
    fingerprints = connection.execute("SELECT DISTINCT ingest_run_id FROM question").fetchall()
    connection.close()

    assert (questions, options) in {(3, 12), (549, 2196)}
    assert len(fingerprints) == 1


# ------------------------------------------------------------------------- derived data


def test_mirror_hosts_collapse_to_one_reference(
    tmp_session: Session, tmp_path: Path, mini_questions
) -> None:
    questions = json.loads(json.dumps(mini_questions))
    questions[0]["references"] = [
        "https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching",
        "https://platform.claude.com/docs/en/build-with-claude/prompt-caching",
    ]
    kb = write_kb(tmp_path / "kb", questions)
    ingested(tmp_session, kb)

    stored = tmp_session.get(corpus.Reference, "docs/build-with-claude/prompt-caching")
    assert stored is not None
    assert stored.question_count == 1
    assert stored.display_url.startswith("https://docs.anthropic.com/")
    citations = tmp_session.scalar(
        sa.select(sa.func.count())
        .select_from(corpus.QuestionReference)
        .where(corpus.QuestionReference.question_id == questions[0]["id"])
    )
    assert citations == 1


def test_the_dedup_hash_columns_are_filled(tmp_session: Session, tmp_kb: Path) -> None:
    """033's cheap layer. Case and whitespace collapse; a different question does not."""
    ingested(tmp_session, tmp_kb)
    rows = tmp_session.execute(
        sa.select(
            corpus.Question.id, corpus.Question.prompt_sha256,
            corpus.Question.normalized_sha256, corpus.Question.item_sha256,
        )
    ).all()

    assert all(all(value for value in row[1:]) for row in rows)
    assert len({row[2] for row in rows}) == len(rows)

    hashes = ingest_module.question_hashes(
        {"prompt_md": "  WHICH   option?  ", "options": [{"text_md": "A"}]}
    )
    same = ingest_module.question_hashes(
        {"prompt_md": "Which option?", "options": [{"text_md": "a"}]}
    )
    assert hashes["normalized_sha256"] == same["normalized_sha256"]
    assert hashes["item_sha256"] == same["item_sha256"]
    assert hashes["prompt_sha256"] != same["prompt_sha256"]


def test_origins_come_from_the_provider(tmp_session: Session, tmp_kb: Path) -> None:
    ingested(tmp_session, tmp_kb)
    assert queries.counts_by_origin(tmp_session) == {"vendor_dump": 3}


def test_an_unknown_provider_refuses_rather_than_guessing() -> None:
    """Guessing `vendor_dump` would label a future parser's output as a vendor's."""
    question = {"id": "x/q1", "source": {"provider": "some-new-vendor"}}
    with pytest.raises(IngestError, match="unknown source provider"):
        ingest_module.origin_of(question)

    assert ingest_module.origin_of({"id": "x/q1", "source": {"provider": "udemy"}}) == "vendor_dump"
    # An explicit origin on the record wins: 043 seeds `official_sample` that way.
    assert (
        ingest_module.origin_of(
            {"id": "x/q1", "origin": "official_sample", "source": {"provider": "anything"}}
        )
        == "official_sample"
    )


# -------------------------------------------------------------------------- the command


@pytest.fixture
def examkb(run_cli):
    def call(*arguments: str, database: Path):
        return run_cli(*arguments, env={"DATABASE_URL": f"sqlite:///{database}"})

    return call


def test_the_cli_reports_what_it_checked(tmp_path: Path, examkb) -> None:
    """`examkb ingest --verify` over the real corpus, end to end."""
    database = tmp_path / "examkb.db"

    upgraded = examkb("db", "upgrade", database=database)
    assert upgraded.returncode == 0, upgraded.stderr

    result = examkb("ingest", "--verify", database=database)

    assert result.returncode == 0, result.stderr
    assert "549 questions from 1 shard" in result.stdout
    assert "checked 5490 text fields across 549 questions" in result.stdout


def test_ingest_refuses_before_the_migrations_are_applied(tmp_path: Path, examkb) -> None:
    result = examkb("ingest", database=tmp_path / "empty.db")

    assert result.returncode == 1
    assert "db upgrade" in result.stderr
