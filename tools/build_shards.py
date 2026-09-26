#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Write kb/shards.json, the index of question shards.

The corpus is a set of shards. `parse_udemy.py` owns `kb/questions.jsonl` and
rewrites it wholesale on every run; everything generated or seeded later writes its
own file under `kb/questions/`. This script is the **only** writer of the index --
if a parser owned it, re-running that parser would drop every other producer's
questions from the corpus without saying so.

Run it after the parsers and before anything that reads the corpus:

    uv run tools/parse_udemy.py --check
    uv run tools/build_shards.py
    uv run tools/verify_lossless.py ...

Usage:
    uv run tools/build_shards.py [--kb DIR] [--check] [--quiet]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from _shared import SHARDS_FILE, discover_shards, display_path, read_shard, shards_path

INDEX_VERSION = 1


def describe(path: Path, kb: Path) -> dict:
    """One shard's index entry: where it is, who wrote it, and what it contained."""
    raw = path.read_bytes()
    questions = read_shard(path)
    providers = sorted({question["source"]["provider"] for question in questions})
    if len(providers) > 1:
        raise ValueError(
            f"{path.relative_to(kb)} mixes providers ({', '.join(providers)}); "
            "one shard is written by one parser"
        )
    return {
        "path": path.relative_to(kb).as_posix(),
        "provider": providers[0] if providers else "unknown",
        "line_count": len(questions),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def build_index(kb: Path) -> dict:
    shards = [describe(path, kb) for path in discover_shards(kb)]
    return {
        "version": INDEX_VERSION,
        "shards": shards,
        "totals": {
            "shards": len(shards),
            "questions": sum(shard["line_count"] for shard in shards),
        },
    }


def serialise(index: dict) -> str:
    return json.dumps(index, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Index the question shards in kb/.")
    parser.add_argument("--kb", default="kb", help="knowledge-base directory")
    parser.add_argument(
        "--check", action="store_true", help="verify the index is current; write nothing"
    )
    parser.add_argument("--quiet", action="store_true", help="print nothing on success")
    args = parser.parse_args()

    kb = (repo_root / args.kb).resolve()
    if not kb.is_dir():
        print(f"missing {kb}; run tools/parse_udemy.py first", file=sys.stderr)
        return 1

    try:
        index = build_index(kb)
    except ValueError as problem:
        print(problem, file=sys.stderr)
        return 1

    if not index["shards"]:
        print(f"no question shards under {kb}", file=sys.stderr)
        return 1

    target = shards_path(kb)
    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != serialise(index):
            print(f"{SHARDS_FILE} is stale; run tools/build_shards.py", file=sys.stderr)
            return 1
        if not args.quiet:
            print(f"{SHARDS_FILE} is current: {index['totals']['shards']} shard(s)")
        return 0

    target.write_text(serialise(index), encoding="utf-8")
    if not args.quiet:
        for shard in index["shards"]:
            print(f"  {shard['path']:32s} {shard['provider']:16s} {shard['line_count']:>5} questions")
        print(
            f"\nwrote {display_path(target, repo_root)}: "
            f"{index['totals']['shards']} shard(s), {index['totals']['questions']} questions"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
