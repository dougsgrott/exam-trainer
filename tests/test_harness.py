"""The harness tests itself.

Fixtures nobody has used yet are fixtures nobody has checked, and a guard that
never fires is indistinguishable from a guard that cannot. These keep both honest
before the issues that depend on them arrive.
"""

from __future__ import annotations

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
