#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Compute statistics over the knowledge base.

Reads kb/questions.jsonl and kb/references.json, writes kb/stats.json, and prints a
terminal summary. Every aggregate the HTML reports display is computed here, so the
reports render numbers rather than recomputing them.

Usage:
    uv run tools/kb_stats.py [--kb DIR] [--top N] [--quiet]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from _shared import KBNotFound, display_path, load_questions, normalize_reference

# Words carrying no domain signal. Deliberately broad: these questions share a house
# style ("Which TWO changes...", "This is incorrect because..."), so the boilerplate
# would otherwise dominate every domain's term list.
STOPWORDS = set(
    """
    the a an and or of to in for is are was were be been being that this these those it its
    as with on by from not no nor but if then than so such at into over under about after
    before during while when what which who whom whose how why where all any both each few
    more most other some only own same too very can will just should now also may might must
    do does did doing done have has had having would could shall
    you your yours they them their there here we our us he she his her hers him i me my
    claude anthropic model models prompt prompts output outputs answer answers question
    questions option options team teams user users use uses used using make makes made
    need needs needed work works working run runs running new first second third next last
    set sets setting settings example examples correct incorrect select two three one
    because instead rather without within across through between per via still yet already
    """.split()
)

# Fixed histogram edges (characters). Fixed rather than data-derived so the shape stays
# comparable as more exams are added.
LENGTH_BINS = {
    "prompt": [0, 150, 250, 350, 450, 550, 700, 900, 10**9],
    "option": [0, 40, 70, 100, 130, 160, 200, 250, 10**9],
    "explanation": [0, 500, 1000, 1500, 2000, 2500, 3000, 4000, 10**9],
}

def reference_section(normalized: str) -> str:
    """A coarse grouping for the reading list: the doc area a page belongs to."""
    segments = normalized.split("/")
    if segments[0] == "support":
        return "Support articles"
    if segments[0] == "docs":
        if len(segments) < 2:
            return "Docs"
        words = segments[1].replace("-", " ").split()
        return " ".join(w.upper() if w in ("api", "sdk", "mcp") else w.title() for w in words)
    return segments[0]


def tokenize(text: str) -> list[str]:
    lowered = text.lower()
    lowered = re.sub(r"\\(.)", r"\1", lowered)  # drop markdown escapes
    words = []
    for word in re.findall(r"[a-z][a-z0-9_.\-]{2,}", lowered):
        # Interior dots and dashes are meaningful (claude.md, human-in-the-loop);
        # trailing ones are just sentence punctuation.
        word = word.rstrip(".-_")
        if len(word) > 2 and word not in STOPWORDS:
            words.append(word)
    return words


def distinctive_terms(documents: dict[str, str], limit: int, min_count: int = 3) -> dict[str, list]:
    """Rank each document's terms by tf-idf against the other documents.

    Documents are domains, so a term scores highly when it is frequent in this domain
    and rare in the rest -- which is exactly a domain fingerprint.
    """
    counts = {key: Counter(tokenize(text)) for key, text in documents.items()}

    # Fold a plural into its singular when both occur, so one concept takes one slot.
    corpus_terms: Counter = Counter()
    for counter in counts.values():
        corpus_terms.update(counter)
    plurals = {t: t[:-1] for t in corpus_terms if t.endswith("s") and t[:-1] in corpus_terms}
    if plurals:
        for key, counter in counts.items():
            for plural, singular in plurals.items():
                if plural in counter:
                    counter[singular] += counter.pop(plural)
    total_docs = len(counts) or 1
    document_frequency: Counter = Counter()
    for counter in counts.values():
        document_frequency.update(counter.keys())

    ranked: dict[str, list] = {}
    for key, counter in counts.items():
        scored = [
            (count * math.log(total_docs / document_frequency[term]), term)
            for term, count in counter.items()
            if count >= min_count and document_frequency[term] < total_docs
        ]
        # Sort by score desc, then term asc, so ties are deterministic across runs.
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        ranked[key] = [
            {"term": term, "score": round(score, 3), "count": counter[term]}
            for score, term in scored[:limit]
        ]
    return ranked


def mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 1) if values else 0.0


def histogram(values: list[int], edges: list[int]) -> list[dict]:
    bins = []
    for low, high in zip(edges, edges[1:]):
        count = sum(1 for v in values if low <= v < high)
        label = f"{low}+" if high >= 10**9 else f"{low}-{high - 1}"
        bins.append({"label": label, "low": low, "high": None if high >= 10**9 else high, "count": count})
    return bins


def length_profile(questions: list[dict]) -> dict:
    prompts = [len(q["prompt_md"]) for q in questions]
    options = [len(o["text_md"]) for q in questions for o in q["options"]]
    explanations = [len(q["overall_explanation_md"]) for q in questions]
    per_option_explanations = [len(o["explanation_md"]) for q in questions for o in q["options"]]
    return {
        "prompt_mean": mean(prompts),
        "option_mean": mean(options),
        "explanation_mean": mean(explanations),
        "option_explanation_mean": mean(per_option_explanations),
        "total_chars": sum(prompts) + sum(options) + sum(explanations) + sum(per_option_explanations),
    }


def type_split(questions: list[dict]) -> dict:
    counter = Counter(q["type"] for q in questions)
    return {
        "single_select": counter.get("single_select", 0),
        "multi_select": counter.get("multi_select", 0),
    }


def build_stats(questions: list[dict], top: int) -> dict:
    certifications: dict[str, dict] = {}
    for question in questions:
        certifications.setdefault(question["certification"]["slug"], question["certification"])

    # --- reference index, mirror hosts merged ---------------------------------
    references: dict[str, dict] = {}
    for question in questions:
        for url in question["references"]:
            key = normalize_reference(url)
            entry = references.setdefault(
                key,
                {
                    "key": key,
                    "url": url,
                    "section": reference_section(key),
                    "count": 0,
                    "variants": [],
                    "certifications": [],
                    "domains": [],
                    "questions": [],
                },
            )
            entry["count"] += 1
            if url not in entry["variants"]:
                entry["variants"].append(url)
            for field, value in (
                ("certifications", question["certification"]["slug"]),
                ("domains", f"{question['certification']['slug']} / {question['domain']}"),
            ):
                if value not in entry[field]:
                    entry[field].append(value)
            entry["questions"].append(question["id"])
    for entry in references.values():
        entry["variants"].sort()
        entry["certifications"].sort()
        entry["domains"].sort()
        # Prefer the shortest variant as the canonical link: the mirrors differ only
        # by locale/prefix noise.
        entry["url"] = min(entry["variants"], key=lambda u: (len(u), u))
    ordered_references = sorted(references.values(), key=lambda r: (-r["count"], r["key"]))

    # --- domains ---------------------------------------------------------------
    by_domain: dict[str, list[dict]] = defaultdict(list)
    for question in questions:
        by_domain[f"{question['certification']['slug']} / {question['domain']}"].append(question)

    corpus = {
        key: " ".join(q["prompt_md"] + " " + " ".join(o["text_md"] for o in q["options"]) for q in group)
        for key, group in by_domain.items()
    }
    terms = distinctive_terms(corpus, limit=12)

    domains = []
    for key in sorted(by_domain):
        group = by_domain[key]
        cert_slug, _, domain_name = key.partition(" / ")
        domain_references = Counter()
        for question in group:
            for url in question["references"]:
                domain_references[normalize_reference(url)] += 1
        domains.append(
            {
                "key": key,
                "certification": cert_slug,
                "domain": domain_name,
                "questions": len(group),
                "types": type_split(group),
                "lengths": length_profile(group),
                "terms": terms.get(key, []),
                "exams": dict(sorted(Counter(q["exam"]["slug"] for q in group).items())),
                "top_references": [
                    {"key": k, "url": references[k]["url"], "count": c}
                    for k, c in sorted(domain_references.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
                ],
            }
        )

    # --- exams -----------------------------------------------------------------
    by_exam: dict[str, list[dict]] = defaultdict(list)
    for question in questions:
        by_exam[f"{question['certification']['slug']}/{question['exam']['slug']}"].append(question)

    exams = []
    for key in sorted(by_exam):
        group = by_exam[key]
        first = group[0]
        exams.append(
            {
                "key": key,
                "certification": first["certification"]["slug"],
                "slug": first["exam"]["slug"],
                "title": first["exam"]["title"],
                "mode": first["exam"]["mode"],
                "questions": len(group),
                "types": type_split(group),
                "lengths": length_profile(group),
                "domains": dict(sorted(Counter(q["domain"] for q in group).items())),
            }
        )

    # --- coverage matrix, per certification ------------------------------------
    coverage = {}
    for slug in sorted(certifications):
        cert_questions = [q for q in questions if q["certification"]["slug"] == slug]
        domain_names = sorted({q["domain"] for q in cert_questions})
        exam_slugs = sorted({q["exam"]["slug"] for q in cert_questions})
        grid = Counter((q["domain"], q["exam"]["slug"]) for q in cert_questions)
        coverage[slug] = {
            "domains": domain_names,
            "exams": exam_slugs,
            "rows": [[grid.get((d, e), 0) for e in exam_slugs] for d in domain_names],
        }

    # --- answer position -------------------------------------------------------
    position_counts = Counter()
    for question in questions:
        position_counts.update(question["correct_labels"])
    total_correct = sum(position_counts.values())
    expected = total_correct / 4 if total_correct else 0
    positions = [
        {
            "label": label,
            "count": position_counts.get(label, 0),
            "expected": round(expected, 1),
            "deviation": position_counts.get(label, 0) - expected,
            "deviation_pct": round(
                (position_counts.get(label, 0) - expected) / expected * 100, 1
            )
            if expected
            else 0.0,
        }
        for label in ("A", "B", "C", "D")
    ]

    # --- mode comparison -------------------------------------------------------
    # Keyed by (certification, mode): the modes only mean anything within a single
    # certification. Comparing ccao-f "hard" against ccar-p would conflate exam mode
    # with a different exam entirely.
    by_mode: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for question in questions:
        by_mode[(question["certification"]["slug"], question["exam"]["mode"] or "standard")].append(
            question
        )
    modes = [
        {
            "certification": cert_slug,
            "mode": mode,
            "questions": len(group),
            "exams": len({q["exam"]["slug"] for q in group}),
            "types": type_split(group),
            "lengths": length_profile(group),
            "multi_select_pct": round(type_split(group)["multi_select"] / len(group) * 100, 1),
        }
        for (cert_slug, mode), group in sorted(by_mode.items())
    ]

    return {
        "totals": {
            "questions": len(questions),
            "certifications": len(certifications),
            "domains": len(by_domain),
            "exams": len(by_exam),
            "options": sum(len(q["options"]) for q in questions),
            "references": len(ordered_references),
            "reference_citations": sum(r["count"] for r in ordered_references),
            "corpus_chars": length_profile(questions)["total_chars"],
        },
        "certifications": [
            {
                **certifications[slug],
                "questions": sum(1 for q in questions if q["certification"]["slug"] == slug),
                "exams": len({q["exam"]["slug"] for q in questions if q["certification"]["slug"] == slug}),
                "domains": len({q["domain"] for q in questions if q["certification"]["slug"] == slug}),
                "types": type_split([q for q in questions if q["certification"]["slug"] == slug]),
            }
            for slug in sorted(certifications)
        ],
        "types": type_split(questions),
        "answer_positions": positions,
        "modes": modes,
        "domains": domains,
        "exams": exams,
        "coverage": coverage,
        "lengths": {
            "prompt": histogram([len(q["prompt_md"]) for q in questions], LENGTH_BINS["prompt"]),
            "option": histogram(
                [len(o["text_md"]) for q in questions for o in q["options"]], LENGTH_BINS["option"]
            ),
            "explanation": histogram(
                [len(q["overall_explanation_md"]) for q in questions], LENGTH_BINS["explanation"]
            ),
        },
        "references": ordered_references,
        "reference_sections": [
            {"section": section, "pages": pages, "citations": citations}
            for section, pages, citations in sorted(
                (
                    (
                        section,
                        sum(1 for r in ordered_references if r["section"] == section),
                        sum(r["count"] for r in ordered_references if r["section"] == section),
                    )
                    for section in {r["section"] for r in ordered_references}
                ),
                key=lambda row: (-row[2], row[0]),
            )
        ],
        "reference_density": [
            {"references": n, "questions": c}
            for n, c in sorted(Counter(len(q["references"]) for q in questions).items())
        ],
    }


# --------------------------------------------------------------------------- output


def bar(value: float, peak: float, width: int = 28) -> str:
    """A proportional block bar for the terminal summary."""
    if peak <= 0:
        return ""
    filled = int(round(value / peak * width))
    return "█" * filled + "·" * (width - filled)


def print_summary(stats: dict, top: int) -> None:
    totals = stats["totals"]
    print(f"\n{'=' * 78}\n  EXAM KNOWLEDGE BASE\n{'=' * 78}")
    print(
        f"  {totals['questions']} questions · {totals['options']} options · "
        f"{totals['domains']} domains · {totals['exams']} exams · "
        f"{totals['references']} unique references"
    )
    print(f"  {totals['corpus_chars']:,} characters of prompt, option, and explanation text")

    print(f"\n  CERTIFICATIONS\n  {'-' * 76}")
    for cert in stats["certifications"]:
        types = cert["types"]
        print(
            f"  {cert['slug']:8s} {cert['name']:44s} {cert['questions']:>4} q  "
            f"{cert['exams']} exams  {types['single_select']}/{types['multi_select']} single/multi"
        )

    print(f"\n  QUESTIONS PER DOMAIN\n  {'-' * 76}")
    peak = max(d["questions"] for d in stats["domains"])
    for domain in sorted(stats["domains"], key=lambda d: (-d["questions"], d["key"])):
        print(
            f"  {domain['certification']:7s} {domain['domain'][:40]:40s} "
            f"{domain['questions']:>3}  {bar(domain['questions'], peak)}"
        )

    print(f"\n  CORRECT-ANSWER POSITION (uniform expectation "
          f"{stats['answer_positions'][0]['expected']})\n  {'-' * 76}")
    position_peak = max(p["count"] for p in stats["answer_positions"])
    for position in stats["answer_positions"]:
        print(
            f"  {position['label']}  {position['count']:>4}  "
            f"{position['deviation_pct']:>+6.1f}%  {bar(position['count'], position_peak)}"
        )

    print(f"\n  EXAM MODE COMPARISON\n  {'-' * 76}")
    print(
        f"  {'cert':8s} {'mode':10s} {'q':>4} {'multi%':>7} {'prompt':>8} "
        f"{'option':>8} {'overall expl':>13}"
    )
    for mode in stats["modes"]:
        lengths = mode["lengths"]
        print(
            f"  {mode['certification']:8s} {mode['mode']:10s} {mode['questions']:>4} "
            f"{mode['multi_select_pct']:>6.1f}% {lengths['prompt_mean']:>8.0f} "
            f"{lengths['option_mean']:>8.0f} {lengths['explanation_mean']:>13.0f}"
        )

    print(f"\n  TOP {top} REFERENCES\n  {'-' * 76}")
    for reference in stats["references"][:top]:
        print(f"  {reference['count']:>4}  {reference['key'][:70]}")

    print(f"\n  DOMAIN FINGERPRINTS (distinctive terms)\n  {'-' * 76}")
    for domain in stats["domains"]:
        terms = ", ".join(t["term"] for t in domain["terms"][:7])
        print(f"  {domain['certification']:7s} {domain['domain'][:34]:34s} {terms[:60]}")
    print()


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Compute knowledge-base statistics.")
    parser.add_argument("--kb", default="kb", help="knowledge-base directory")
    parser.add_argument("--top", type=int, default=15, help="rows in the long terminal lists")
    parser.add_argument("--quiet", action="store_true", help="write stats.json without printing")
    args = parser.parse_args()

    kb = (repo_root / args.kb).resolve()
    try:
        questions = load_questions(kb)
    except KBNotFound as missing:
        print(f"missing {missing.path}; run tools/parse_udemy.py first", file=sys.stderr)
        return 1

    stats = build_stats(questions, args.top)

    # Cross-check against the parser's own manifest: a divergence means one of the two
    # is aggregating wrongly.
    manifest_path = kb / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))["totals"]
        for field in ("questions", "options", "exams"):
            if manifest[field] != stats["totals"][field]:
                print(
                    f"mismatch with manifest.json: {field} "
                    f"{stats['totals'][field]} != {manifest[field]}",
                    file=sys.stderr,
                )
                return 1

    (kb / "stats.json").write_text(
        json.dumps(stats, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    if not args.quiet:
        print_summary(stats, args.top)
    print(f"wrote {display_path(kb / 'stats.json', repo_root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
