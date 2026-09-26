"""`kb/` is a pure function of `data/`, and the pipeline is deterministic.

These are the two invariants no feature issue owns. Everything else in `kb/` --
the study Markdown, the reports, the stats, the shard index -- is downstream of
them: if the corpus is not reproducible then nothing built on it can be trusted to
be either, and `README.md`'s promise that `kb/` is "safe to delete and rebuild"
is not true.

The pipeline is run into `tmp_path`, never over the repo's own `kb/`. Building into
an empty directory proves the same thing `rm -rf kb` would -- that nothing but
`data/` was an input -- without putting the working corpus at risk to prove it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from conftest import REPO_ROOT

pytestmark = pytest.mark.slow


def run_pipeline(run_tool, kb: Path) -> None:
    """Every documented command, in the documented order, into `kb`."""
    steps = [
        ("tools/parse_udemy.py", "--out", str(kb), "--check"),
        # 017 put this in `make pipeline` and it belongs here for the same reason.
        # It was invisible until 018 actually transcribed a blueprint: with
        # `data/blueprints/` empty the step wrote no files, so leaving it out and
        # putting it in produced identical trees.
        ("tools/parse_blueprint.py", "--kb", str(kb), "--quiet"),
        ("tools/build_shards.py", "--kb", str(kb), "--quiet"),
        ("tools/build_kb.py", "--kb", str(kb)),
        ("tools/verify_lossless.py", "--kb", str(kb)),
        ("tools/kb_stats.py", "--kb", str(kb), "--quiet"),
        ("tools/build_report.py", "--kb", str(kb)),
    ]
    for step in steps:
        done = run_tool(*step)
        assert done.returncode == 0, f"{step[0]} failed:\n{done.stdout}\n{done.stderr}"


def digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_kb_is_a_pure_function_of_data(run_tool, tmp_path: Path) -> None:
    """A rebuild from nothing but `data/` reproduces the committed tree exactly."""
    rebuilt = tmp_path / "kb"
    run_pipeline(run_tool, rebuilt)

    baseline, fresh = digest(REPO_ROOT / "kb"), digest(rebuilt)
    assert sorted(fresh) == sorted(baseline), "the rebuilt tree has different files"

    differing = sorted(name for name in baseline if baseline[name] != fresh[name])
    assert not differing, f"rebuilt bytes differ from the committed kb/: {differing}"


def test_the_pipeline_is_deterministic(run_tool, tmp_path: Path) -> None:
    """Twice in a row changes nothing.

    Catches the two classic sources of drift: iteration over an unsorted dict or
    set, and a timestamp embedded in generated output.
    """
    first, second = tmp_path / "one", tmp_path / "two"
    run_pipeline(run_tool, first)
    run_pipeline(run_tool, second)

    one, two = digest(first), digest(second)
    assert sorted(one) == sorted(two)
    drifting = sorted(name for name in one if one[name] != two[name])
    assert not drifting, f"output changed between identical runs: {drifting}"


def test_rerunning_in_place_changes_nothing(run_tool, tmp_path: Path) -> None:
    """The real-world case: the pipeline run again over a corpus it already built."""
    kb = tmp_path / "kb"
    run_pipeline(run_tool, kb)
    before = digest(kb)
    run_pipeline(run_tool, kb)
    assert digest(kb) == before
