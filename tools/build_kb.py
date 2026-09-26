#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Render the canonical KB into readable Markdown study documents.

Reads kb/questions.jsonl (produced by parse_udemy.py) and writes:

    kb/study/index.md                     entry point, all certifications
    kb/study/<cert>/00-index.md           domain listing for one certification
    kb/study/<cert>/NN-<domain>.md        every question in that domain
    kb/study/references.md                cited links, grouped by certification

Each question renders prompt-and-options first with the answer inside a collapsed
block, so a file can be read straight through as a self-quiz.

Usage:
    uv run tools/build_kb.py [--kb DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _shared import KBNotFound, display_path, load_questions, slugify

TYPE_LABEL = {"single_select": "Select ONE", "multi_select": "Select TWO or more"}


def indent_block(text: str, prefix: str = "> ") -> str:
    """Quote a multi-paragraph Markdown block, keeping blank lines quoted."""
    lines = [prefix + line if line else prefix.rstrip() for line in text.split("\n")]
    return "\n".join(lines)


def render_question(question: dict) -> str:
    out: list[str] = []
    exam = question["exam"]
    out.append(f"### {question['id']}")
    out.append("")
    out.append(
        f"`{TYPE_LABEL[question['type']]}` · {exam['title']} · question {exam['question_number']}"
    )
    out.append("")
    out.append(question["prompt_md"])
    out.append("")
    for option in question["options"]:
        out.append(f"- **{option['label']}.** {option['text_md']}")
    out.append("")
    out.append("<details>")
    out.append(
        f"<summary>Answer: <b>{', '.join(question['correct_labels'])}</b></summary>"
    )
    out.append("")
    for option in question["options"]:
        mark = "✅" if option["correct"] else "❌"
        out.append(f"**{mark} {option['label']}. {option['text_md']}**")
        out.append("")
        out.append(indent_block(option["explanation_md"]))
        out.append("")
    out.append("**Overall explanation**")
    out.append("")
    out.append(indent_block(question["overall_explanation_md"]))
    out.append("")
    if question["references"]:
        out.append("**References**")
        out.append("")
        for url in question["references"]:
            out.append(f"- <{url}>")
        out.append("")
    out.append("</details>")
    out.append("")
    return "\n".join(out)


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Render kb/study Markdown from the canonical KB.")
    parser.add_argument("--kb", default="kb", help="knowledge-base directory")
    args = parser.parse_args()

    kb = (repo_root / args.kb).resolve()
    try:
        questions = load_questions(kb)
    except KBNotFound as missing:
        print(f"missing {missing.path}; run tools/parse_udemy.py first", file=sys.stderr)
        return 1

    study = kb / "study"

    # certification slug -> certification record, preserving first-seen order
    certs: dict[str, dict] = {}
    for question in questions:
        certs.setdefault(question["certification"]["slug"], question["certification"])

    file_count = 0
    for slug, cert in sorted(certs.items()):
        cert_questions = [q for q in questions if q["certification"]["slug"] == slug]
        domains = sorted({q["domain"] for q in cert_questions})
        cert_dir = study / slug
        cert_dir.mkdir(parents=True, exist_ok=True)

        index: list[str] = [
            f"# {cert['name']}",
            "",
            f"{len(cert_questions)} questions across {len(domains)} domains, "
            f"from {len({q['exam']['slug'] for q in cert_questions})} practice exams.",
            "",
            "| # | Domain | Questions | File |",
            "| --- | --- | ---: | --- |",
        ]

        for position, domain in enumerate(domains, start=1):
            in_domain = [q for q in cert_questions if q["domain"] == domain]
            in_domain.sort(key=lambda q: (q["exam"]["number"], q["exam"]["question_number"]))
            filename = f"{position:02d}-{slugify(domain)}.md"
            index.append(f"| {position} | {domain} | {len(in_domain)} | [{filename}]({filename}) |")

            single = sum(1 for q in in_domain if q["type"] == "single_select")
            body = [
                f"# {domain}",
                "",
                f"{cert['name']} · {len(in_domain)} questions "
                f"({single} select-one, {len(in_domain) - single} select-many)",
                "",
                "Answers and explanations are collapsed, so this file can be read as a self-quiz.",
                "",
                "---",
                "",
            ]
            for question in in_domain:
                body.append(render_question(question))
                body.append("---")
                body.append("")
            (cert_dir / filename).write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")
            file_count += 1

        index.append("")
        (cert_dir / "00-index.md").write_text("\n".join(index) + "\n", encoding="utf-8")
        file_count += 1

    # --- top-level index --------------------------------------------------------
    top: list[str] = [
        "# Exam knowledge base",
        "",
        f"{len(questions)} questions parsed from saved practice-exam pages. "
        "Generated by `tools/build_kb.py` — do not edit by hand.",
        "",
        "| Certification | Level | Questions | Exams | Index |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for slug, cert in sorted(certs.items()):
        cert_questions = [q for q in questions if q["certification"]["slug"] == slug]
        exams = len({q["exam"]["slug"] for q in cert_questions})
        top.append(
            f"| {cert['name']} | {cert['level']} | {len(cert_questions)} | {exams} "
            f"| [{slug}/00-index.md]({slug}/00-index.md) |"
        )
    top.append("")
    top.append("All cited links: [references.md](references.md)")
    top.append("")
    (study / "index.md").write_text("\n".join(top), encoding="utf-8")
    file_count += 1

    # --- reference index --------------------------------------------------------
    references_path = kb / "references.json"
    if references_path.exists():
        data = json.loads(references_path.read_text(encoding="utf-8"))
        lines = [
            "# Cited references",
            "",
            f"{data['count']} unique links cited by the explanations, most-cited first.",
            "",
            "| Citations | Certifications | Link |",
            "| ---: | --- | --- |",
        ]
        for entry in data["references"]:
            lines.append(
                f"| {entry['count']} | {', '.join(entry['certifications'])} | <{entry['url']}> |"
            )
        lines.append("")
        (study / "references.md").write_text("\n".join(lines), encoding="utf-8")
        file_count += 1

    print(f"wrote {file_count} Markdown files under {display_path(study, repo_root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
