"""Test scaffolding shared by every test in this repo.

Three rules the fixtures here exist to keep:

1. **No test writes outside `tmp_path`.** `data/` is the source of truth and `kb/`
   is the user's working corpus; a test that edits either is a test that can lose
   work. `repo_is_untouched` enforces it rather than trusting it.
2. **The default run is fast and does not read the real corpus.** Anything that
   runs a parser over the real `data/` is marked `slow` and deselected by default
   (`pyproject.toml`); `tmp_kb` gives the rest a three-question corpus instead.
3. **Nothing non-deterministic is left to chance.** `frozen_clock` and `seeded_rng`
   exist so a test never depends on the wall clock or on unseeded randomness.

`tmp_db` (005) follows the same rules: a real migrated database, in `tmp_path`,
built by the real migration rather than by `create_all` -- a fixture schema that
`db upgrade` never produced is a fixture that stops catching migration bugs on the
day it is written.
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from examkb import migrations
from examkb.db import new_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# `tools/` is not a package -- the app reaches it through examkb/compat.py and the
# scripts get it as sys.path[0]. Tests take the same route the app does.
sys.path.insert(0, str(REPO_ROOT / "tools"))

from _shared import SHARDS_FILE  # noqa: E402  -- needs the path above

# The instant every test that needs "now" agrees on. Deliberately not today.
FROZEN_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
SEED = 20260102

# Traversed for the guard below. Everything else at the repo root is either churn
# a test cannot be blamed for (`.venv`, caches) or not ours (`.git`).
IGNORED = {".git", ".venv", ".pytest_cache", "__pycache__", "node_modules"}


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Every file under `root` as name -> (size, mtime_ns). Cheap: stat only.

    A missing directory snapshots as empty rather than raising -- a clean checkout
    has no `kb/` yet, and the guard runs before every test.
    """
    root = Path(root)
    if not root.is_dir():
        return {}

    found: dict[str, tuple[int, int]] = {}
    stack = [root]
    while stack:
        for entry in sorted(stack.pop().iterdir()):
            if entry.name in IGNORED:
                continue
            if entry.is_dir():
                stack.append(entry)
            elif entry.is_file():
                info = entry.stat()
                found[str(entry.relative_to(root))] = (info.st_size, info.st_mtime_ns)
    return found


@pytest.fixture(scope="session")
def _repo_snapshot() -> dict:
    """One rolling snapshot, so the guard costs one stat pass per test, not two."""
    return {"files": _snapshot(REPO_ROOT)}


@pytest.fixture(autouse=True)
def repo_is_untouched(_repo_snapshot: dict):
    """Fail the test that wrote anywhere in the repo, naming the file it touched.

    Tests write to `tmp_path` and nowhere else. `data/` is the source of truth and
    `kb/` is the user's working corpus; a test that edits either is a test that can
    lose work, and by the time anyone notices, the run that did it is long gone.
    """
    before = _repo_snapshot["files"]
    yield
    after = _snapshot(REPO_ROOT)
    _repo_snapshot["files"] = after
    changed = sorted(
        set(before) ^ set(after)
        | {name for name in before.keys() & after.keys() if before[name] != after[name]}
    )
    assert not changed, f"test wrote inside the repo: {changed}"


@pytest.fixture
def run_tool():
    """Run a `tools/` script the way the documented pipeline does."""

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["uv", "run", *args], cwd=REPO_ROOT, capture_output=True, text=True
        )

    return run


def write_kb(kb: Path, questions: list[dict]) -> Path:
    """Write a corpus to disk in the layout the real pipeline produces."""
    import build_shards

    kb.mkdir(parents=True, exist_ok=True)
    (kb / "questions.jsonl").write_text(
        "".join(json.dumps(q, ensure_ascii=False) + "\n" for q in questions), encoding="utf-8"
    )

    by_exam: dict[tuple[str, str], list[dict]] = {}
    for question in questions:
        by_exam.setdefault(
            (question["certification"]["slug"], question["exam"]["slug"]), []
        ).append(question)
    for (cert, exam_slug), group in by_exam.items():
        target = kb / "exams" / cert
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{exam_slug}.json").write_text(
            json.dumps(
                {
                    "certification": group[0]["certification"],
                    "exam": {**group[0]["exam"], "question_count": len(group)},
                    "questions": group,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    references: dict[str, dict] = {}
    for question in questions:
        for url in question["references"]:
            entry = references.setdefault(url, {"url": url, "count": 0})
            entry["count"] += 1
    ordered = sorted(references.values(), key=lambda r: (-r["count"], r["url"]))
    (kb / "references.json").write_text(
        json.dumps({"count": len(ordered), "references": ordered}, indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    # The real index, written by the real writer -- a fixture that invents its own
    # shards.json would stop catching format changes the day the format changed.
    (kb / SHARDS_FILE).write_text(
        build_shards.serialise(build_shards.build_index(kb)), encoding="utf-8"
    )
    return kb


@pytest.fixture
def mini_questions() -> list[dict]:
    """Three questions: two certifications, three domains, both question types."""
    return json.loads((FIXTURES / "mini_corpus.json").read_text(encoding="utf-8"))


@pytest.fixture
def tmp_kb(tmp_path: Path, mini_questions: list[dict]) -> Path:
    """A throwaway corpus, complete enough to load, filter and index."""
    return write_kb(tmp_path / "kb", mini_questions)


# ---------------------------------------------------------------------------- database


@pytest.fixture(scope="session")
def _migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One migrated database per session, copied per test.

    Running the migration for every test that wants a database cost ~120 ms each
    and was most of the default run's wall clock by the time 006 landed. The
    schema is identical for all of them, so it is built once and copied; the
    checkpoint is what makes the copy complete, because in WAL mode the newest
    pages can still be sitting in `examkb.db-wal`.
    """
    path = tmp_path_factory.mktemp("migrated") / "examkb.db"
    url = f"sqlite:///{path}"
    migrations.upgrade(url=url)
    engine = new_engine(url)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    engine.dispose()
    return path


@pytest.fixture
def tmp_db(tmp_path: Path, _migrated_template: Path) -> Iterator[Engine]:
    """A migrated, empty database of its own, thrown away with the test.

    Migrated, not `create_all`-ed: the schema under test is the one
    `examkb db upgrade` actually produces, views and triggers included.
    """
    database = tmp_path / "examkb.db"
    shutil.copyfile(_migrated_template, database)
    engine = new_engine(f"sqlite:///{database}")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def tmp_session(tmp_db: Engine) -> Iterator[Session]:
    """A session on `tmp_db`, rolled back and closed afterwards."""
    with Session(tmp_db, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch):
    """A `now()` that does not move, and does not depend on the wall clock."""

    class Clock:
        now = FROZEN_NOW

        def __call__(self) -> datetime:
            return self.now

        def advance(self, **delta) -> datetime:
            from datetime import timedelta

            self.now += timedelta(**delta)
            return self.now

    return Clock()


@pytest.fixture
def seeded_rng() -> random.Random:
    """The only source of randomness a test may use."""
    return random.Random(SEED)
