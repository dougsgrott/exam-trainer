"""The shard layout: re-running a parser must not delete another producer's work.

Integration fix 1 from `plans/03-study-platform.md`, and the most dangerous one:
`parse_udemy.py` rewrites `kb/questions.jsonl` and `kb/manifest.json` wholesale on
every run, so before the shard layout existed, one documented pipeline command
would have silently erased every approved generated question.

Most of these run against `tmp_kb`'s three questions in milliseconds. The four that
have to drive the real parser over the real `data/` -- because "re-running the real
parser is safe" is not a claim a fake corpus can support -- are marked `slow`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import write_kb

from _shared import (
    SHARDS_FILE,
    StaleShardIndex,
    discover_shards,
    load_questions,
    read_shard_index,
)

PLANTED_ID = "gen-run-001/q001"


def plant_shard(kb: Path) -> Path:
    """A shard no parser owns, shaped like the real thing."""
    source = load_questions(kb)[0]
    question = json.loads(json.dumps(source))
    question["id"] = PLANTED_ID
    question["source"] = {"provider": "local_generation", "run_id": "run-001"}

    shard = kb / "questions" / "generated.jsonl"
    shard.parent.mkdir(parents=True, exist_ok=True)
    shard.write_text(json.dumps(question, ensure_ascii=False) + "\n", encoding="utf-8")
    return shard


@pytest.fixture
def parsed_kb(run_tool, tmp_path: Path) -> Path:
    """The real 549-question corpus, freshly parsed into a throwaway directory."""
    kb = tmp_path / "kb"
    done = run_tool("tools/parse_udemy.py", "--out", str(kb), "--check")
    assert done.returncode == 0, done.stderr
    return kb


# --------------------------------------------------------------------- the index


def test_index_records_provider_line_count_and_hash(run_tool, tmp_kb: Path) -> None:
    plant_shard(tmp_kb)
    assert run_tool("tools/build_shards.py", "--kb", str(tmp_kb), "--quiet").returncode == 0

    index = json.loads((tmp_kb / SHARDS_FILE).read_text(encoding="utf-8"))
    by_path = {entry["path"]: entry for entry in index["shards"]}
    assert by_path["questions.jsonl"]["provider"] == "udemy"
    assert by_path["questions.jsonl"]["line_count"] == 3
    assert by_path["questions/generated.jsonl"]["provider"] == "local_generation"
    assert by_path["questions/generated.jsonl"]["line_count"] == 1
    assert len(by_path["questions.jsonl"]["sha256"]) == 64
    assert index["totals"] == {"shards": 2, "questions": 4}


def test_no_index_falls_back_to_todays_behaviour(tmp_kb: Path) -> None:
    """With only questions.jsonl and no index, nothing about loading changes."""
    (tmp_kb / SHARDS_FILE).unlink()
    assert read_shard_index(tmp_kb) is None
    assert [p.name for p in discover_shards(tmp_kb)] == ["questions.jsonl"]
    assert len(load_questions(tmp_kb)) == 3


def test_a_shard_missing_from_the_index_is_loud(tmp_kb: Path) -> None:
    """Silently skipping it is the failure mode; raising is the point."""
    plant_shard(tmp_kb)  # planted after tmp_kb wrote the index

    with pytest.raises(StaleShardIndex) as raised:
        load_questions(tmp_kb)
    assert "questions/generated.jsonl" in str(raised.value)
    assert "build_shards" in str(raised.value)


def test_build_shards_check_detects_a_stale_index(run_tool, tmp_kb: Path) -> None:
    fresh = run_tool("tools/build_shards.py", "--kb", str(tmp_kb), "--check", "--quiet")
    assert fresh.returncode == 0, fresh.stderr

    plant_shard(tmp_kb)
    stale = run_tool("tools/build_shards.py", "--kb", str(tmp_kb), "--check", "--quiet")
    assert stale.returncode == 1
    assert "stale" in stale.stderr


def test_build_shards_refuses_a_shard_with_two_providers(run_tool, tmp_kb, mini_questions) -> None:
    """One shard is written by one parser. Mixed provenance means something is wrong."""
    mixed = json.loads(json.dumps(mini_questions))
    mixed[0]["source"]["provider"] = "local_generation"
    (tmp_kb / "questions.jsonl").write_text(
        "".join(json.dumps(q, ensure_ascii=False) + "\n" for q in mixed), encoding="utf-8"
    )

    done = run_tool("tools/build_shards.py", "--kb", str(tmp_kb), "--quiet")
    assert done.returncode == 1
    assert "mixes providers" in done.stderr


def test_provider_filter_reads_the_records_not_the_index(run_tool, tmp_kb: Path) -> None:
    plant_shard(tmp_kb)
    assert run_tool("tools/build_shards.py", "--kb", str(tmp_kb), "--quiet").returncode == 0

    assert len(load_questions(tmp_kb, provider="udemy")) == 3
    assert len(load_questions(tmp_kb, provider="local_generation")) == 1
    assert load_questions(tmp_kb, provider="nobody") == []


def test_no_parser_writes_the_index() -> None:
    """The index has one writer. A parser owning it is the original bug."""
    from conftest import REPO_ROOT

    writers = [
        script.name
        for script in sorted((REPO_ROOT / "tools").glob("parse_*.py"))
        if SHARDS_FILE in script.read_text(encoding="utf-8")
    ]
    assert writers == [], f"a parser references {SHARDS_FILE}: {writers}"


def test_write_kb_round_trips_through_the_real_index(tmp_path: Path, mini_questions) -> None:
    """The fixture writes what the real writer writes, or it proves nothing."""
    kb = write_kb(tmp_path / "kb", mini_questions)
    index = read_shard_index(kb)
    assert index == [
        {
            "path": "questions.jsonl",
            "provider": "udemy",
            "line_count": 3,
            "sha256": index[0]["sha256"],
        }
    ]
    assert len(load_questions(kb)) == 3


# ------------------------------------------------- against the real corpus


@pytest.mark.slow
def test_parser_rerun_keeps_a_shard_it_does_not_own(run_tool, parsed_kb: Path) -> None:
    """The regression this whole layout exists for."""
    shard = plant_shard(parsed_kb)
    assert run_tool("tools/build_shards.py", "--kb", str(parsed_kb), "--quiet").returncode == 0
    assert len(load_questions(parsed_kb)) == 550

    # The dangerous command: the first step of the documented pipeline, run again.
    done = run_tool("tools/parse_udemy.py", "--out", str(parsed_kb), "--check")
    assert done.returncode == 0, done.stderr

    assert shard.exists(), "re-running the parser deleted a shard it does not own"
    assert "questions/generated.jsonl" in {e["path"] for e in read_shard_index(parsed_kb)}

    questions = load_questions(parsed_kb)
    assert len(questions) == 550
    assert PLANTED_ID in {question["id"] for question in questions}


@pytest.mark.slow
def test_verify_lossless_skips_questions_with_no_source_page(run_tool, parsed_kb: Path) -> None:
    plant_shard(parsed_kb)
    assert run_tool("tools/build_shards.py", "--kb", str(parsed_kb), "--quiet").returncode == 0

    done = run_tool("tools/verify_lossless.py", "--kb", str(parsed_kb))
    assert done.returncode == 0, done.stderr
    assert "checked 5490 fields across 549 udemy questions" in done.stdout
    assert "skipped 1 question(s) with no saved source page" in done.stdout


@pytest.mark.slow
def test_verify_lossless_reports_every_bad_file_not_just_the_first(run_tool, parsed_kb) -> None:
    questions = load_questions(parsed_kb)
    corrupt_files = sorted({q["source"]["file"] for q in questions})[:2]
    assert len(corrupt_files) == 2

    seen = set()
    for question in questions:
        name = question["source"]["file"]
        if name in corrupt_files and name not in seen:
            question["prompt_md"] = "this text is nowhere in the source page"
            seen.add(name)

    (parsed_kb / "questions.jsonl").write_text(
        "".join(json.dumps(q, ensure_ascii=False) + "\n" for q in questions), encoding="utf-8"
    )

    done = run_tool("tools/verify_lossless.py", "--kb", str(parsed_kb))
    assert done.returncode == 1
    for name in corrupt_files:
        assert name in done.stderr, f"{name} not reported"
    assert "2 file(s) with problems" in done.stderr


@pytest.mark.slow
def test_verify_lossless_catches_a_shard_that_shrank(run_tool, parsed_kb: Path) -> None:
    """The assertion that makes the printed count worth reading."""
    assert run_tool("tools/build_shards.py", "--kb", str(parsed_kb), "--quiet").returncode == 0

    lines = (parsed_kb / "questions.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
    (parsed_kb / "questions.jsonl").write_text("".join(lines[:-1]), encoding="utf-8")

    done = run_tool("tools/verify_lossless.py", "--kb", str(parsed_kb))
    assert done.returncode == 1
    assert "declares 549 udemy questions but 548 loaded" in done.stderr
