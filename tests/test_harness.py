"""The harness tests itself.

Fixtures nobody has used yet are fixtures nobody has checked, and a guard that
never fires is indistinguishable from a guard that cannot. These keep both honest
before the issues that depend on them arrive.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from conftest import FROZEN_NOW, REPO_ROOT, SEED, _snapshot, write_kb

from _shared import load_questions


def test_tmp_kb_is_a_throwaway_that_builds_in_well_under_a_second(
    tmp_path: Path, mini_questions
) -> None:
    started = time.perf_counter()
    kb = write_kb(tmp_path / "kb", mini_questions)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"tmp_kb took {elapsed:.2f}s"
    assert tmp_path in kb.parents or kb.parent == tmp_path
    assert REPO_ROOT not in kb.parents, "the fixture corpus must not live in the repo"


def test_tmp_kb_covers_both_question_types_and_two_certifications(tmp_kb: Path) -> None:
    questions = load_questions(tmp_kb)
    assert len(questions) == 3
    assert {q["type"] for q in questions} == {"single_select", "multi_select"}
    assert {q["certification"]["slug"] for q in questions} == {"mini-a", "mini-b"}
    assert len({q["domain"] for q in questions}) == 3


def test_the_repo_guard_notices_a_new_file(tmp_path: Path) -> None:
    """`repo_is_untouched` is only worth having if it actually detects a write."""
    before = _snapshot(tmp_path)
    (tmp_path / "written.txt").write_text("x", encoding="utf-8")
    assert set(_snapshot(tmp_path)) - set(before) == {"written.txt"}


def test_the_repo_guard_notices_an_edit(tmp_path: Path) -> None:
    target = tmp_path / "edited.txt"
    target.write_text("before", encoding="utf-8")
    before = _snapshot(tmp_path)
    target.write_text("after!", encoding="utf-8")  # same length, different bytes
    assert _snapshot(tmp_path) != before, "an in-place edit went unnoticed"


def test_the_repo_guard_survives_a_missing_directory(tmp_path: Path) -> None:
    """A clean checkout has no `kb/` yet, and the guard runs before every test."""
    assert _snapshot(tmp_path / "does-not-exist") == {}


def test_frozen_clock_does_not_move(frozen_clock) -> None:
    assert frozen_clock() == FROZEN_NOW
    assert frozen_clock() == frozen_clock()
    assert frozen_clock.advance(hours=2) == frozen_clock()
    assert frozen_clock() > FROZEN_NOW


def test_seeded_rng_is_reproducible(seeded_rng) -> None:
    import random

    reference = random.Random(SEED)
    drawn = [seeded_rng.random() for _ in range(5)]
    assert drawn == [reference.random() for _ in range(5)]
    assert len(set(drawn)) == 5, "a fresh Random per draw would repeat the first value"


# ------------------------------------------------------------------ the CLI runner
#
# `run_cli` replaced seven subprocesses at the end of phase 1 and took the default
# run from 31 s to 22 s. That is only a good trade while it keeps proving the same
# things, so the runner gets tested against the thing it replaced.


def test_run_cli_agrees_with_a_real_process(run_cli) -> None:
    """The same command, both ways, character for character.

    One deliberately succeeds and one deliberately fails, because the exit code is
    half of what these tests assert and the in-process path reaches it through a
    caught `SystemExit` rather than through a process exiting.
    """
    import subprocess
    import sys

    for arguments in (["--version"], ["ingest", "--kb", "/nonexistent-kb"]):
        spawned = subprocess.run(
            [sys.executable, "-m", "examkb.cli", *arguments],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
            env={**os.environ, "DATABASE_URL": "sqlite:///./does-not-exist.db"},
        )
        in_process = run_cli(*arguments, env={"DATABASE_URL": "sqlite:///./does-not-exist.db"})

        assert in_process.returncode == spawned.returncode, arguments
        assert in_process.stdout == spawned.stdout, arguments
        assert in_process.stderr == spawned.stderr, arguments


def test_run_cli_does_not_leak_one_database_into_the_next(run_cli, tmp_path: Path) -> None:
    """The subtle part of running in process: what the app memoised last time.

    `get_settings` and the engines are `lru_cache`d, so without
    `reset_app_caches()` the second command here would happily report the *first*
    database's revision. This is the test that fails if that reset is ever dropped.
    """
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"

    upgraded = run_cli(
        "db", "upgrade", "--no-backup", env={"DATABASE_URL": f"sqlite:///{first}"}
    )
    assert upgraded.returncode == 0, upgraded.stderr
    assert first.exists()

    asked = run_cli("db", "current", env={"DATABASE_URL": f"sqlite:///{second}"})

    assert "no database yet" in asked.stdout.lower() or asked.returncode != 0
    assert str(first) not in asked.stdout
    assert not second.exists(), "asking about a database should not create one"


def test_run_cli_captures_both_streams_separately(run_cli, tmp_path: Path) -> None:
    result = run_cli("ingest", env={"DATABASE_URL": f"sqlite:///{tmp_path / 'empty.db'}"})

    assert result.returncode == 1
    assert "db upgrade" in result.stderr
    assert result.stdout == ""


def test_run_cli_reads_standard_input_when_a_command_wants_it(run_cli, tmp_path: Path) -> None:
    """`import-marks -` is the one command that reads stdin."""
    import json

    database = tmp_path / "examkb.db"
    assert run_cli(
        "db", "upgrade", "--no-backup", env={"DATABASE_URL": f"sqlite:///{database}"}
    ).returncode == 0

    result = run_cli(
        "import-marks",
        "-",
        env={"DATABASE_URL": f"sqlite:///{database}"},
        stdin=json.dumps({"ccao-f/exam-01/q001": "known"}),
    )

    assert result.returncode == 0
    assert "1 mark(s) read" in result.stdout
