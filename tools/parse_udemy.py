#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["beautifulsoup4", "lxml"]
# ///
"""Extract practice-exam content from saved Udemy result pages into a canonical KB.

The saved pages under data/udemy_courses/*/raw_tests/*.html are ~1.1MB DOM dumps.
This script pulls out the exam content and writes:

    kb/exams/<cert>/<exam>.json   canonical, one file per exam
    kb/questions.jsonl            flat, one question per line
    kb/references.json            every link cited by an explanation
    kb/manifest.json              run summary

Usage:
    uv run tools/parse_udemy.py [--src DIR] [--out DIR] [--check]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

PARSER_VERSION = "1.0.0"

# Every rich-text tag that appears anywhere in the corpus. The converter raises on
# anything outside this set so a future Udemy markup change fails loudly instead of
# silently dropping content.
KNOWN_TAGS = {"p", "b", "strong", "i", "em", "code", "a", "br"}

# Certification identity, keyed by the exam-code prefix in the results header.
CERTIFICATIONS = {
    "CCAO-F": {
        "slug": "ccao-f",
        "name": "Claude Certified Associate - Foundations",
        "vendor": "anthropic",
        "level": "associate",
    },
    "CCAR-P": {
        "slug": "ccar-p",
        "name": "Claude Certified Architect - Professional",
        "vendor": "anthropic",
        "level": "professional",
    },
}


# --------------------------------------------------------------------------- markdown


def to_markdown(node: Tag) -> str:
    """Convert a Udemy rich-text node to Markdown."""
    return _tidy("".join(_render(child) for child in node.children))


# Literal text may contain characters Markdown would interpret: option texts quote
# things like "Bash(scp *)" and "<thinking> tags". Underscores are left alone inside
# words (identifiers such as output_config), where Markdown does not emphasise them.
# Code-span content is exempt: the backticks already make it literal.
_ESCAPE_RE = re.compile(r"(?<![A-Za-z0-9])_|[\\*`\[\]<>]")


def _escape_text(text: str) -> str:
    return _ESCAPE_RE.sub(lambda m: "\\" + m.group(0), text)


def _render(node, *, in_code: bool = False) -> str:
    if isinstance(node, NavigableString):
        # Inside a code span the backticks already make the text literal, and a
        # backslash there would show up verbatim.
        return str(node) if in_code else _escape_text(str(node))
    if not isinstance(node, Tag):
        return ""

    name = node.name.lower()
    if name not in KNOWN_TAGS:
        raise ValueError(f"unexpected tag <{name}> in rich text near: {node.get_text()[:120]!r}")

    inner = "".join(_render(child, in_code=in_code or name == "code") for child in node.children)

    if name == "p":
        return inner.strip() + "\n\n"
    if name in ("b", "strong"):
        return f"**{inner.strip()}**" if inner.strip() else ""
    if name in ("i", "em"):
        return f"*{inner.strip()}*" if inner.strip() else ""
    if name == "code":
        return f"`{inner.strip()}`" if inner.strip() else ""
    if name == "br":
        return "\n"
    if name == "a":
        href = (node.get("href") or "").strip()
        text = inner.strip()
        return f"[{text}]({href})" if href else text
    return inner


def _tidy(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------- parsing


@dataclass
class ParseStats:
    files: int = 0
    questions: int = 0
    options: int = 0
    warnings: list[str] = field(default_factory=list)


def one(scope: Tag, selector: str, where: str) -> Tag:
    """Select exactly one node within a scope, failing loudly otherwise.

    The saved pages repeat ids like #answer-text many times per document, so every
    lookup must be scoped to a single question pane rather than the whole document.
    """
    found = scope.select(selector)
    if len(found) != 1:
        raise ValueError(f"expected 1 {selector!r} in {where}, found {len(found)}")
    return found[0]


def parse_exam(path: Path, repo_root: Path, course_url: str, stats: ParseStats) -> dict:
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "lxml")

    header = soup.select_one('[class*="results-header--title"]')
    if header is None:
        raise ValueError(f"no results header in {path.name}")
    # e.g. "CCAO-F Full Practice Exam 4: Hard Mode - Results". The header is used in
    # preference to the filename because it carries the canonical title and mode.
    title = re.sub(r"\s*-\s*Results\s*$", "", header.get_text(" ", strip=True)).strip()

    code_match = re.match(r"([A-Z]+-[A-Z])\b", title)
    if not code_match or code_match.group(1) not in CERTIFICATIONS:
        raise ValueError(f"unrecognised certification in title {title!r}")
    cert = CERTIFICATIONS[code_match.group(1)]

    num_match = re.search(r"Practice Exam (\d+)", title)
    if not num_match:
        raise ValueError(f"no exam number in title {title!r}")
    exam_number = int(num_match.group(1))
    exam_slug = f"exam-{exam_number:02d}"

    mode_match = re.search(r":\s*(\w+) Mode\s*$", title)
    mode = mode_match.group(1).lower() if mode_match else None

    source = {
        "provider": "udemy",
        "course_url": course_url,
        "file": str(path.relative_to(repo_root)),
    }
    exam_meta = {"slug": exam_slug, "title": title, "number": exam_number, "mode": mode}

    panes = soup.select('div[class*="question-result-pane-wrapper"]')
    if not panes:
        raise ValueError(f"no question panes in {path.name}")

    questions = [
        parse_question(pane, index=i, cert=cert, exam=exam_meta, source=source, stats=stats)
        for i, pane in enumerate(panes, start=1)
    ]

    stats.files += 1
    return {
        "certification": cert,
        "exam": {**exam_meta, "question_count": len(questions)},
        "source": source,
        "questions": questions,
    }


def parse_question(pane: Tag, *, index, cert, exam, source, stats: ParseStats) -> dict:
    where = f"{exam['slug']} q{index}"

    number = index
    title_node = pane.select_one('[class*="pane-title"]')
    if title_node:
        match = re.search(r"Question\s+(\d+)", title_node.get_text(" ", strip=True))
        if match:
            number = int(match.group(1))
    if number != index:
        stats.warnings.append(f"{where}: header says Question {number}, DOM position {index}")

    prompt_md = to_markdown(one(pane, "#question-prompt", where))

    # Selection icon distinguishes the two question formats: radio => choose one,
    # checkbox => choose several.
    icon = pane.select_one('[data-purpose="answer-result-body-selection-icon"] use')
    icon_href = (icon.get("xlink:href") or icon.get("href") or "") if icon else ""
    qtype = "multi_select" if "checkbox" in icon_href else "single_select"

    options = []
    for i, result_pane in enumerate(pane.select('[class*="result-pane--answer-result-pane"]')):
        answer = result_pane.select_one('[data-purpose="answer"]')
        if answer is None:
            continue
        # Nothing was ever submitted, so an option is either "correct" or "skipped"
        # (= not selected); there is no user-response noise to filter out.
        classes = " ".join(answer.get("class") or [])
        options.append(
            {
                "label": chr(ord("A") + i),
                "text_md": to_markdown(one(answer, "#answer-text", where)),
                "correct": "answer-correct" in classes,
                "explanation_md": to_markdown(one(result_pane, "#question-explanation", where)),
            }
        )
    stats.options += len(options)

    correct_labels = [o["label"] for o in options if o["correct"]]
    if qtype == "single_select" and len(correct_labels) != 1:
        stats.warnings.append(f"{where}: radio icon but {len(correct_labels)} correct options")
    if qtype == "multi_select" and len(correct_labels) < 2:
        stats.warnings.append(f"{where}: checkbox icon but {len(correct_labels)} correct options")

    overall_md = to_markdown(one(pane, "#overall-explanation", where))

    domain_pane = one(pane, '[data-purpose="domain-pane"]', where)
    domain_header = domain_pane.select_one('[class*="domain-pane-header"]')
    if domain_header:
        domain_header.extract()
    domain = domain_pane.get_text(" ", strip=True)

    references: list[str] = []
    for link in pane.select("#question-explanation a[href], #overall-explanation a[href]"):
        href = link["href"].strip()
        if href and href not in references:
            references.append(href)

    payload = prompt_md + " " + " ".join(sorted(o["text_md"] for o in options))
    content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    stats.questions += 1
    return {
        "id": f"{cert['slug']}/{exam['slug']}/q{number:03d}",
        "content_hash": content_hash,
        "certification": cert,
        "exam": {**exam, "question_number": number},
        "domain": domain,
        "type": qtype,
        "select_count": len(correct_labels),
        "prompt_md": prompt_md,
        "options": options,
        "correct_labels": correct_labels,
        "overall_explanation_md": overall_md,
        "references": references,
        "source": source,
    }


def course_url_for(course_dir: Path) -> str:
    frontmatter = course_dir / "frontmatter.md"
    if not frontmatter.exists():
        return ""
    for line in frontmatter.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("http"):
            return line
    return ""


# --------------------------------------------------------------------------- checks


def run_checks(exams: list[dict]) -> list[str]:
    problems: list[str] = []
    seen_hashes: dict[str, str] = {}
    seen_ids: set[str] = set()

    for exam in exams:
        for q in exam["questions"]:
            qid = q["id"]
            if qid in seen_ids:
                problems.append(f"{qid}: duplicate question id")
            seen_ids.add(qid)

            if len(q["options"]) != 4:
                problems.append(f"{qid}: {len(q['options'])} options (expected 4)")
            if not q["prompt_md"]:
                problems.append(f"{qid}: empty prompt")
            if not q["domain"]:
                problems.append(f"{qid}: empty domain")
            if not q["overall_explanation_md"]:
                problems.append(f"{qid}: empty overall explanation")
            for option in q["options"]:
                if not option["text_md"]:
                    problems.append(f"{qid} option {option['label']}: empty text")
                if not option["explanation_md"]:
                    problems.append(f"{qid} option {option['label']}: empty explanation")

            n_correct = len(q["correct_labels"])
            if q["type"] == "single_select" and n_correct != 1:
                problems.append(f"{qid}: single_select with {n_correct} correct")
            if q["type"] == "multi_select" and n_correct < 2:
                problems.append(f"{qid}: multi_select with {n_correct} correct")

            prior = seen_hashes.get(q["content_hash"])
            if prior:
                problems.append(f"{qid}: duplicate content, same as {prior}")
            else:
                seen_hashes[q["content_hash"]] = qid

    return problems


# --------------------------------------------------------------------------- main


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Parse saved Udemy exam pages into kb/.")
    parser.add_argument("--src", default="data/udemy_courses", help="course source directory")
    parser.add_argument("--out", default="kb", help="knowledge-base output directory")
    parser.add_argument("--check", action="store_true", help="exit non-zero on any problem")
    args = parser.parse_args()

    src = (repo_root / args.src).resolve()
    out = (repo_root / args.out).resolve()
    if not src.is_dir():
        print(f"source directory not found: {src}", file=sys.stderr)
        return 1

    stats = ParseStats()
    exams: list[dict] = []

    for html in sorted(src.glob("*/raw_tests/*.html")):
        exam = parse_exam(html, repo_root, course_url_for(html.parent.parent), stats)
        exams.append(exam)
        print(
            f"  parsed {exam['certification']['slug']}/{exam['exam']['slug']}"
            f"  {exam['exam']['question_count']:>3} questions  ({html.name})"
        )

    exams.sort(key=lambda e: (e["certification"]["slug"], e["exam"]["number"]))

    for exam in exams:
        target = out / "exams" / exam["certification"]["slug"]
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{exam['exam']['slug']}.json").write_text(
            json.dumps(exam, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    all_questions = [q for exam in exams for q in exam["questions"]]
    with (out / "questions.jsonl").open("w", encoding="utf-8") as handle:
        for question in all_questions:
            handle.write(json.dumps(question, ensure_ascii=False) + "\n")

    references: dict[str, dict] = {}
    for question in all_questions:
        for url in question["references"]:
            entry = references.setdefault(
                url,
                {"url": url, "count": 0, "certifications": [], "domains": [], "questions": []},
            )
            entry["count"] += 1
            for key, value in (
                ("certifications", question["certification"]["slug"]),
                ("domains", question["domain"]),
            ):
                if value not in entry[key]:
                    entry[key].append(value)
            entry["questions"].append(question["id"])
    ordered_refs = sorted(references.values(), key=lambda r: (-r["count"], r["url"]))
    for entry in ordered_refs:
        entry["certifications"].sort()
        entry["domains"].sort()
    (out / "references.json").write_text(
        json.dumps(
            {"count": len(ordered_refs), "references": ordered_refs}, indent=2, ensure_ascii=False
        )
        + "\n",
        encoding="utf-8",
    )

    by_domain: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for question in all_questions:
        key = f"{question['certification']['slug']} / {question['domain']}"
        by_domain[key] = by_domain.get(key, 0) + 1
        by_type[question["type"]] = by_type.get(question["type"], 0) + 1

    problems = run_checks(exams)
    manifest = {
        "parser_version": PARSER_VERSION,
        "source": str(src.relative_to(repo_root)),
        "totals": {
            "exams": len(exams),
            "questions": len(all_questions),
            "options": stats.options,
            "unique_references": len(ordered_refs),
        },
        "questions_by_type": dict(sorted(by_type.items())),
        "questions_by_domain": dict(sorted(by_domain.items())),
        "certifications": sorted({e["certification"]["slug"] for e in exams}),
        "exams": [
            {
                "certification": e["certification"]["slug"],
                "slug": e["exam"]["slug"],
                "title": e["exam"]["title"],
                "mode": e["exam"]["mode"],
                "question_count": e["exam"]["question_count"],
            }
            for e in exams
        ],
        "warnings": stats.warnings,
        "validation_problems": problems,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print(
        f"\n{len(exams)} exams | {len(all_questions)} questions | "
        f"{stats.options} options | {len(ordered_refs)} unique references"
    )
    print(f"types: {by_type}")
    if stats.warnings:
        print(f"\n{len(stats.warnings)} warning(s):")
        for warning in stats.warnings[:20]:
            print(f"  - {warning}")
    if problems:
        print(f"\n{len(problems)} validation problem(s):", file=sys.stderr)
        for problem in problems[:20]:
            print(f"  - {problem}", file=sys.stderr)
        if args.check:
            return 1
    else:
        print("validation: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
