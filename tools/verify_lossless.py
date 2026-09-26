#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["beautifulsoup4", "lxml"]
# ///
"""Prove that the KB lost no text from the source pages.

Re-parses every source HTML file, reverses the Markdown conversion of every field
the Udemy parser produced, and compares the result to the source node's plain text.
Any difference means the parser dropped, duplicated, or mangled content.

Only `source.provider == "udemy"` questions are checked, because they are the only
ones with saved HTML to check against: generated and seeded questions have no
upstream page, so they are counted and skipped rather than failed. The field count
is asserted against what the corpus says it should be, so a shard silently going
missing shows up as a failure here rather than as a smaller number nobody reads.

Usage:
    uv run tools/verify_lossless.py [--kb DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _shared import (
    SHARDS_FILE,
    KBNotFound,
    load_questions,
    norm,
    read_shard_index,
    strip_markdown,
)
from bs4 import BeautifulSoup, Tag

PROVIDER = "udemy"


def source_text(node: Tag) -> str:
    """Plain text of a rich-text node: inline tags concatenate, <p> separates."""
    for paragraph in node.find_all("p"):
        paragraph.insert_after("\n")
    return norm(node.get_text(""))


def expected_fields(questions: list[dict]) -> int:
    """Prompt and overall explanation, plus a text and an explanation per option."""
    return sum(2 + 2 * len(question["options"]) for question in questions)


def indexed_count(kb: Path, provider: str) -> int | None:
    """How many questions `shards.json` says that provider's shards hold.

    Checked against what was actually loaded, so a shard that went missing or a
    file that got truncated fails here instead of quietly shrinking the corpus
    this script claims to have verified.
    """
    index = read_shard_index(kb)
    if index is None:
        return None
    return sum(entry["line_count"] for entry in index if entry["provider"] == provider)


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Verify the KB against its source HTML.")
    parser.add_argument("--kb", default="kb", help="knowledge-base directory")
    args = parser.parse_args()

    kb = (repo_root / args.kb).resolve()
    try:
        everything = load_questions(kb)
    except KBNotFound as missing:
        print(f"missing {missing.path}; run tools/parse_udemy.py first", file=sys.stderr)
        return 1

    questions = [q for q in everything if q["source"]["provider"] == PROVIDER]
    skipped = len(everything) - len(questions)
    if not questions:
        print(f"no {PROVIDER} questions in {kb}", file=sys.stderr)
        return 1

    by_file: dict[str, list[dict]] = {}
    for question in questions:
        by_file.setdefault(question["source"]["file"], []).append(question)

    compared = mismatches = 0
    bad_files: list[str] = []
    for relative_path, group in sorted(by_file.items()):
        soup = BeautifulSoup((repo_root / relative_path).read_text(encoding="utf-8"), "lxml")
        panes = soup.select('div[class*="question-result-pane-wrapper"]')
        group.sort(key=lambda q: q["exam"]["question_number"])
        if len(panes) != len(group):
            # Report it and keep going: one unreadable page should not hide the
            # state of the other eight.
            print(f"{relative_path}: {len(panes)} panes but {len(group)} questions", file=sys.stderr)
            bad_files.append(relative_path)
            continue

        file_mismatches = 0
        for pane, question in zip(panes, group):
            checks = [
                ("prompt", pane.select_one("#question-prompt"), question["prompt_md"]),
                ("overall", pane.select_one("#overall-explanation"), question["overall_explanation_md"]),
            ]
            result_panes = pane.select('[class*="result-pane--answer-result-pane"]')
            for result_pane, option in zip(result_panes, question["options"]):
                label = option["label"]
                checks.append((f"option {label}", result_pane.select_one("#answer-text"), option["text_md"]))
                checks.append(
                    (f"explanation {label}", result_pane.select_one("#question-explanation"), option["explanation_md"])
                )

            for name, node, markdown in checks:
                compared += 1
                expected, actual = source_text(node), strip_markdown(markdown)
                if expected != actual:
                    mismatches += 1
                    file_mismatches += 1
                    if file_mismatches <= 3:
                        print(f"MISMATCH {question['id']} {name}")
                        print(f"  source: {expected[:200]}")
                        print(f"  kb    : {actual[:200]}")

        if file_mismatches:
            print(f"{relative_path}: {file_mismatches} mismatch(es)", file=sys.stderr)
            bad_files.append(relative_path)

    print(f"checked {compared} fields across {len(questions)} {PROVIDER} questions")
    if skipped:
        print(f"skipped {skipped} question(s) with no saved source page")

    # Every problem is reported before returning, so one bad file cannot hide the
    # state of the rest -- and both counts are asserted, not merely printed.
    # Without the first assertion the script exits 0 on a corpus it never fully
    # looked at: lose a shard and the fields it checked and the fields it expected
    # fall by exactly the same amount.
    problems: list[str] = []

    declared = indexed_count(kb, PROVIDER)
    if declared is not None and declared != len(questions):
        problems.append(
            f"{SHARDS_FILE} declares {declared} {PROVIDER} questions but "
            f"{len(questions)} loaded; run tools/build_shards.py"
        )

    expected = expected_fields(questions)
    if compared != expected and not bad_files:
        problems.append(f"expected {expected} fields but checked {compared}")

    if bad_files:
        problems.append(f"{len(bad_files)} file(s) with problems, {mismatches} mismatch(es):")
        problems.extend(f"  - {name}" for name in bad_files)

    if problems:
        print("", file=sys.stderr)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1

    print("lossless: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
