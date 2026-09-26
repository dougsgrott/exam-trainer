#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Render the knowledge base into self-contained HTML reports.

Reads kb/stats.json and kb/questions.jsonl, writes kb/reports/:

    index.html    overview dashboard
    domains.html  per-domain profiles
    browse.html   filterable question browser with quiz mode
    reading.html  deduplicated documentation reading list

Every page is standalone: CSS and JS inline, no CDN, no external asset. The only
outbound links are the Anthropic documentation pages the explanations cite.

Usage:
    uv run tools/build_report.py [--kb DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from html import escape
from pathlib import Path

import charts
from _shared import KBNotFound, display_path, load_questions, md_to_html, slugify

PAGES = [
    ("index.html", "Overview"),
    ("domains.html", "Domains"),
    ("browse.html", "Browse"),
    ("reading.html", "Reading list"),
]

# Palette from the dataviz reference instance, validated with scripts/validate_palette.js
# in both modes (all checks pass; light-mode aqua sits at 2.74:1, which is why every
# chart ships direct labels and a table view).
PALETTE_CSS = """
:root {
  color-scheme: light;
  --surface-0: #f7f7f5;
  --surface-1: #fcfcfb;
  --surface-2: #f0efec;
  --border: #e2e1dc;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --text-muted: #78766f;
  --grid: #dedcd6;
  --series-1: #2a78d6;
  --series-2: #eb6834;
  --series-3: #1baf7a;
  --diverge-pos: #2a78d6;
  --diverge-neg: #e34948;
  --good: #1baf7a;
  --seq-0: #f0efec;
  --seq-1: #cde2fb;
  --seq-2: #9ec5f4;
  --seq-3: #6da7ec;
  --seq-4: #3987e5;
  --seq-5: #2a78d6;
  --seq-6: #1c5cab;
  --seq-7: #104281;
  /* ink for sequential cells: "low" = steps 1-3 (pale fills), "high" = steps 4-7 */
  --seq-ink-low: #0b0b0b;
  --seq-ink-high: #ffffff;
  --shadow: 0 1px 2px rgba(11, 11, 11, .05), 0 1px 8px rgba(11, 11, 11, .04);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --surface-0: #131312;
    --surface-1: #1a1a19;
    --surface-2: #232322;
    --border: #333330;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted: #94938a;
    --grid: #38383550;
    --series-1: #3987e5;
    --series-2: #d95926;
    --series-3: #199e70;
    --diverge-pos: #3987e5;
    --diverge-neg: #e66767;
    --good: #199e70;
    --seq-0: #26262a;
    --seq-1: #104281;
    --seq-2: #184f95;
    --seq-3: #256abf;
    --seq-4: #3987e5;
    --seq-5: #6da7ec;
    --seq-6: #9ec5f4;
    --seq-7: #cde2fb;
    /* the ramp inverts for the dark surface, so the ink assignments invert with it */
    --seq-ink-low: #ffffff;
    --seq-ink-high: #0b0b0b;
    --shadow: none;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface-0: #131312;
  --surface-1: #1a1a19;
  --surface-2: #232322;
  --border: #333330;
  --text-primary: #ffffff;
  --text-secondary: #c3c2b7;
  --text-muted: #94938a;
  --grid: #38383550;
  --series-1: #3987e5;
  --series-2: #d95926;
  --series-3: #199e70;
  --diverge-pos: #3987e5;
  --diverge-neg: #e66767;
  --good: #199e70;
  --seq-0: #26262a;
  --seq-1: #104281;
  --seq-2: #184f95;
  --seq-3: #256abf;
  --seq-4: #3987e5;
  --seq-5: #6da7ec;
  --seq-6: #9ec5f4;
  --seq-7: #cde2fb;
  --seq-ink-low: #ffffff;
  --seq-ink-high: #0b0b0b;
  --shadow: none;
}
"""

BASE_CSS = """
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--surface-0); color: var(--text-primary);
  font-family: ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
  font-size: 15px; line-height: 1.6; -webkit-font-smoothing: antialiased;
}
a { color: var(--series-1); }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .92em; }
code { background: var(--surface-2); padding: .1em .35em; border-radius: 4px; }
nav {
  position: sticky; top: 0; z-index: 10; display: flex; gap: 4px; align-items: center;
  padding: 10px 24px; background: var(--surface-1); border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}
nav .brand { font-weight: 700; margin-right: 16px; letter-spacing: -.01em; }
nav a {
  padding: 6px 12px; border-radius: 7px; text-decoration: none;
  color: var(--text-secondary); font-size: 14px; font-weight: 500;
}
nav a:hover { background: var(--surface-2); color: var(--text-primary); }
nav a[aria-current="page"] { background: var(--surface-2); color: var(--text-primary); }
nav .spacer { flex: 1; }
button.ghost {
  background: var(--surface-1); color: var(--text-secondary); cursor: pointer;
  border: 1px solid var(--border); border-radius: 7px; padding: 6px 12px;
  font: inherit; font-size: 13px;
}
button.ghost:hover { color: var(--text-primary); border-color: var(--text-muted); }
main { max-width: 1100px; margin: 0 auto; padding: 28px 24px 80px; }
h1 { font-size: 30px; letter-spacing: -.02em; margin: 8px 0 4px; }
h2 { font-size: 19px; letter-spacing: -.01em; margin: 40px 0 4px; }
h3 { font-size: 15px; margin: 0 0 2px; }
.lede { color: var(--text-secondary); margin: 0 0 8px; }
.hint { color: var(--text-muted); font-size: 13px; margin: 0 0 14px; }
.card {
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
  padding: 18px 20px; margin: 14px 0; box-shadow: var(--shadow);
}
.grid { display: grid; gap: 14px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.tile {
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
  padding: 14px 16px; box-shadow: var(--shadow);
}
.tile-label { font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: .05em; }
.tile-value { font-size: 27px; font-weight: 700; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }
.tile-note { font-size: 12px; color: var(--text-secondary); }
.hero { font-size: 56px; font-weight: 800; letter-spacing: -.035em; line-height: 1; }
.chart { display: block; overflow: visible; max-width: 100%; }
.chart-scroll { overflow-x: auto; }
.legend { display: flex; gap: 16px; flex-wrap: wrap; margin: 0 0 10px; font-size: 13px; color: var(--text-secondary); }
.legend .key { display: inline-flex; align-items: center; gap: 6px; }
.legend i { width: 11px; height: 11px; border-radius: 3px; display: inline-block; }
.table-view { margin-top: 10px; }
.table-view summary {
  cursor: pointer; font-size: 13px; color: var(--text-muted); padding: 3px 0;
}
.table-view summary:hover { color: var(--text-primary); }
table { border-collapse: collapse; width: 100%; margin-top: 8px; font-size: 13px; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); }
th { color: var(--text-muted); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 0; }
.chip {
  background: var(--surface-2); border: 1px solid var(--border); border-radius: 999px;
  padding: 2px 10px; font-size: 12.5px; color: var(--text-secondary);
}
.small { font-size: 13px; color: var(--text-secondary); }
.muted { color: var(--text-muted); }
.panels { display: flex; flex-wrap: wrap; gap: 20px 28px; }
@media (max-width: 640px) {
  main { padding: 20px 14px 60px; }
  h1 { font-size: 24px; }
  .hero { font-size: 42px; }
}
"""

THEME_JS = """
(function () {
  var stored = null;
  try { stored = localStorage.getItem('kb-theme'); } catch (e) {}
  if (stored) document.documentElement.setAttribute('data-theme', stored);
  window.toggleTheme = function () {
    var root = document.documentElement;
    var current = root.getAttribute('data-theme');
    if (!current) {
      current = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    var next = current === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('kb-theme', next); } catch (e) {}
  };
})();
"""


# --------------------------------------------------------------------------- shell


def page(title: str, active: str, body: str, *, extra_css: str = "", extra_js: str = "") -> str:
    links = "".join(
        f'<a href="{href}"{" aria-current=\"page\"" if href == active else ""}>{escape(name)}</a>'
        for href, name in PAGES
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{PALETTE_CSS}{BASE_CSS}{extra_css}</style>
<script>{THEME_JS}</script>
</head>
<body>
<nav>
  <span class="brand">exam-kb</span>
  {links}
  <span class="spacer"></span>
  <button class="ghost" onclick="toggleTheme()" title="Toggle light and dark">◐ Theme</button>
</nav>
<main>
{body}
</main>
{f"<script>{extra_js}</script>" if extra_js else ""}
</body>
</html>
"""


def card(title: str, note: str, content: str) -> str:
    return (
        f'<div class="card"><h3>{escape(title)}</h3>'
        f'<p class="hint">{escape(note)}</p>{content}</div>'
    )


# --------------------------------------------------------------------------- pages


def build_index(stats: dict) -> str:
    totals = stats["totals"]
    body = [
        "<h1>Exam knowledge base</h1>",
        '<p class="lede">What 549 parsed practice-exam questions say about themselves.</p>',
        '<div class="tiles">',
        charts.stat_tile("Questions", f"{totals['questions']:,}", f"{totals['options']:,} options"),
        charts.stat_tile("Certifications", str(totals["certifications"]), f"{totals['exams']} exams"),
        charts.stat_tile("Domains", str(totals["domains"]), "across both certs"),
        charts.stat_tile(
            "Cited pages",
            str(totals["references"]),
            f"{totals['reference_citations']:,} citations",
        ),
        charts.stat_tile("Corpus", f"{totals['corpus_chars'] / 1_000_000:.1f}M", "characters of text"),
        "</div>",
    ]

    # --- coverage heatmaps ------------------------------------------------------
    body.append("<h2>Coverage</h2>")
    body.append(
        '<p class="hint">How many questions each exam contributes to each domain. '
        "Darker means more.</p>"
    )
    for cert in stats["certifications"]:
        grid = stats["coverage"][cert["slug"]]
        heat = charts.heatmap(
            grid["rows"],
            grid["domains"],
            grid["exams"],
            label=f"{cert['name']} coverage by domain and exam",
            gutter=330,
        )
        table = charts.table(
            ["Domain"] + [e.replace("exam-", "Exam ") for e in grid["exams"]] + ["Total"],
            [
                [domain] + [str(v) for v in row] + [str(sum(row))]
                for domain, row in zip(grid["domains"], grid["rows"])
            ],
            caption="Table view",
        )
        body.append(
            card(
                cert["name"],
                f"{cert['questions']} questions · {cert['exams']} exams · {cert['domains']} domains",
                f'<div class="chart-scroll">{heat}</div>{table}',
            )
        )

    # --- questions per domain ---------------------------------------------------
    domain_items = sorted(
        ((f"{d['certification']} · {d['domain']}", d["questions"]) for d in stats["domains"]),
        key=lambda pair: (-pair[1], pair[0]),
    )
    body.append("<h2>Distribution</h2>")
    body.append(
        card(
            "Questions per domain",
            "One hue: domains are nominal, so bar length alone carries the magnitude.",
            charts.hbar(domain_items, label="Questions per domain", gutter=280)
            + charts.table(
                ["Domain", "Questions"],
                [[name, str(int(value))] for name, value in domain_items],
            ),
        )
    )

    # --- type split -------------------------------------------------------------
    split_items = [
        (
            f"{d['certification']} · {d['domain']}",
            [d["types"]["single_select"], d["types"]["multi_select"]],
        )
        for d in sorted(stats["domains"], key=lambda d: (-d["questions"], d["key"]))
    ]
    types = stats["types"]
    body.append(
        card(
            "Question format by domain",
            f"{types['single_select']} select-one and {types['multi_select']} select-many "
            "questions overall.",
            charts.stacked_bar(
                split_items,
                ["Select one", "Select many"],
                label="Question format by domain",
                gutter=280,
            )
            + charts.table(
                ["Domain", "Select one", "Select many", "Multi %"],
                [
                    [
                        name,
                        str(int(values[0])),
                        str(int(values[1])),
                        f"{values[1] / (values[0] + values[1]) * 100:.0f}%",
                    ]
                    for name, values in split_items
                ],
            ),
        )
    )

    # --- answer position --------------------------------------------------------
    positions = stats["answer_positions"]
    expected = positions[0]["expected"]
    body.append("<h2>Exam-taking signals</h2>")
    body.append(
        card(
            "Where the correct answer sits",
            f"Deviation from an even split across the four positions (expected {expected} each). "
            "Blue is over-represented, red under.",
            charts.diverging_bar(
                [(p["label"], p["deviation_pct"]) for p in positions],
                label="Correct-answer position, deviation from expectation",
            )
            + charts.table(
                ["Position", "Correct", "Expected", "Deviation"],
                [
                    [p["label"], str(p["count"]), f"{p['expected']:.1f}", f"{p['deviation_pct']:+.1f}%"]
                    for p in positions
                ],
            ),
        )
    )

    # --- mode comparison, small multiples ---------------------------------------
    modes = stats["modes"]
    slots = {f"{m['certification']} {m['mode']}": index + 1 for index, m in enumerate(modes)}
    metrics = [
        ("Questions", lambda m: m["questions"], "{:.0f}"),
        ("Multi-select %", lambda m: m["multi_select_pct"], "{:.1f}"),
        ("Prompt chars", lambda m: m["lengths"]["prompt_mean"], "{:.0f}"),
        ("Option chars", lambda m: m["lengths"]["option_mean"], "{:.0f}"),
        ("Explanation chars", lambda m: m["lengths"]["explanation_mean"], "{:.0f}"),
    ]
    panels = "".join(
        charts.small_multiple(
            name,
            [(f"{m['certification']} {m['mode']}", getter(m)) for m in modes],
            slots=slots,
            value_format=fmt,
        )
        for name, getter, fmt in metrics
    )
    legend = " ".join(
        f'<span class="key"><i style="background:var(--series-{slot})"></i>{escape(name)}</span>'
        for name, slot in slots.items()
    )
    body.append(
        card(
            "Exam groups compared",
            "Small multiples on separate axes — never two scales on one plot. Each group keeps "
            "its color across every panel.",
            f'<div class="legend">{legend}</div><div class="panels">{panels}</div>'
            + charts.table(
                ["Group", "Questions", "Multi %", "Prompt", "Option", "Explanation"],
                [
                    [
                        f"{m['certification']} {m['mode']}",
                        str(m["questions"]),
                        f"{m['multi_select_pct']:.1f}%",
                        f"{m['lengths']['prompt_mean']:.0f}",
                        f"{m['lengths']['option_mean']:.0f}",
                        f"{m['lengths']['explanation_mean']:.0f}",
                    ]
                    for m in modes
                ],
            ),
        )
    )

    # --- length distributions ---------------------------------------------------
    body.append("<h2>Text length</h2>")
    for key, title, note in [
        ("prompt", "Question prompts", "Characters per question stem."),
        ("explanation", "Overall explanations", "Characters per overall explanation."),
        ("option", "Answer options", "Characters per individual option."),
    ]:
        bins = stats["lengths"][key]
        body.append(
            card(
                title,
                note,
                charts.histogram(bins, label=f"{title} length distribution")
                + charts.table(
                    ["Range (chars)", "Count"], [[b["label"], str(b["count"])] for b in bins]
                ),
            )
        )

    return page("Overview · exam-kb", "index.html", "\n".join(body))


def build_domains(stats: dict) -> str:
    body = [
        "<h1>Domains</h1>",
        '<p class="lede">Each exam domain profiled: volume, format, vocabulary, and the '
        "documentation its explanations lean on.</p>",
    ]
    by_cert: dict[str, list[dict]] = {}
    for domain in stats["domains"]:
        by_cert.setdefault(domain["certification"], []).append(domain)

    certs = {c["slug"]: c for c in stats["certifications"]}
    for slug in sorted(by_cert):
        body.append(f"<h2>{escape(certs[slug]['name'])}</h2>")
        for domain in sorted(by_cert[slug], key=lambda d: (-d["questions"], d["domain"])):
            types = domain["types"]
            lengths = domain["lengths"]
            chips = "".join(
                f'<span class="chip">{escape(term["term"])}</span>' for term in domain["terms"][:10]
            )
            references = "".join(
                f'<li><a href="{escape(r["url"], quote=True)}" target="_blank" rel="noopener">'
                f'{escape(r["key"])}</a> <span class="muted">· {r["count"]} citations</span></li>'
                for r in domain["top_references"]
            )
            exam_rows = " ".join(
                f'<span class="chip">{escape(exam.replace("exam-", "Exam "))} · {count}</span>'
                for exam, count in domain["exams"].items()
            )
            body.append(
                f"""<div class="card" id="{slugify(domain['domain'])}">
<h3>{escape(domain['domain'])}</h3>
<p class="hint">{domain['questions']} questions ·
{types['single_select']} select-one, {types['multi_select']} select-many ·
mean prompt {lengths['prompt_mean']:.0f} chars, explanation {lengths['explanation_mean']:.0f} chars</p>
<p class="small"><strong>Distinctive vocabulary</strong> — terms that occur here far more
than in the other {stats['totals']['domains'] - 1} domains:</p>
<div class="chips">{chips}</div>
<p class="small" style="margin-top:14px"><strong>Most cited documentation</strong></p>
<ul class="small">{references}</ul>
<p class="small"><strong>Spread across exams</strong></p>
<div class="chips">{exam_rows}</div>
<p style="margin-top:14px"><a href="browse.html#domain={escape(domain['domain'], quote=True)}">
Study these {domain['questions']} questions →</a></p>
</div>"""
            )
    return page("Domains · exam-kb", "domains.html", "\n".join(body))


def build_reading(stats: dict) -> str:
    references = stats["references"]
    sections = stats["reference_sections"]
    body = [
        "<h1>Reading list</h1>",
        f'<p class="lede">{len(references)} documentation pages cited '
        f'{stats["totals"]["reference_citations"]:,} times across the explanations, '
        "most-cited first. Mirror hosts are merged, so each page counts once.</p>",
        '<div class="tiles">',
        charts.stat_tile("Unique pages", str(len(references))),
        charts.stat_tile("Citations", f"{stats['totals']['reference_citations']:,}"),
        charts.stat_tile("Sections", str(len(sections))),
        "</div>",
    ]

    top = [(r["key"], r["count"]) for r in references[:20]]
    body.append("<h2>Most cited</h2>")
    body.append(
        card(
            "Top 20 pages",
            "Read these first — they carry the concepts the explanations return to.",
            charts.hbar(top, label="Most cited documentation pages", gutter=330, width=760)
            + charts.table(
                ["Page", "Citations", "Questions"],
                [[r["key"], str(r["count"]), str(len(r["questions"]))] for r in references[:20]],
            ),
        )
    )

    body.append("<h2>By section</h2>")
    section_items = [(s["section"], s["citations"]) for s in sections]
    body.append(
        card(
            "Citations by documentation area",
            "Where the exam's center of gravity sits in the docs.",
            charts.hbar(section_items, label="Citations by documentation section", gutter=210)
            + charts.table(
                ["Section", "Pages", "Citations"],
                [[s["section"], str(s["pages"]), str(s["citations"])] for s in sections],
            ),
        )
    )

    body.append("<h2>Every page</h2>")
    body.append(
        '<p class="hint">Grouped by section. Each entry lists the domains that cite it, so you '
        "can read for the domain you are weakest in.</p>"
    )
    for section in sections:
        rows = [r for r in references if r["section"] == section["section"]]
        items = []
        for reference in rows:
            domains = "".join(
                f'<span class="chip">{escape(d)}</span>' for d in reference["domains"]
            )
            variants = (
                f'<details class="table-view"><summary>{len(reference["variants"])} mirror URLs'
                f"</summary><ul class='small mono'>"
                + "".join(f"<li>{escape(v)}</li>" for v in reference["variants"])
                + "</ul></details>"
                if len(reference["variants"]) > 1
                else ""
            )
            items.append(
                f'<div class="card"><h3><a href="{escape(reference["url"], quote=True)}" '
                f'target="_blank" rel="noopener">{escape(reference["key"])}</a></h3>'
                f'<p class="hint">{reference["count"]} citations · '
                f'{len(reference["questions"])} questions</p>'
                f'<div class="chips">{domains}</div>{variants}</div>'
            )
        body.append(
            f'<h3 style="margin-top:28px">{escape(section["section"])} '
            f'<span class="muted small">· {section["pages"]} pages, '
            f'{section["citations"]} citations</span></h3>' + "".join(items)
        )

    return page("Reading list · exam-kb", "reading.html", "\n".join(body))


BROWSE_CSS = """
.controls {
  position: sticky; top: 49px; z-index: 9; background: var(--surface-0);
  padding: 14px 0 12px; border-bottom: 1px solid var(--border); margin-bottom: 8px;
}
.controls .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.controls select, .controls input {
  font: inherit; font-size: 14px; padding: 7px 10px; border-radius: 8px;
  border: 1px solid var(--border); background: var(--surface-1); color: var(--text-primary);
}
.controls input[type="search"] { flex: 1; min-width: 200px; }
.controls label.toggle {
  display: inline-flex; align-items: center; gap: 7px; font-size: 14px;
  color: var(--text-secondary); background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 8px; padding: 7px 12px; cursor: pointer; user-select: none;
}
.status { font-size: 13px; color: var(--text-muted); margin-top: 10px; }
.status b { color: var(--text-primary); font-variant-numeric: tabular-nums; }
.q { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
     padding: 18px 20px; margin: 14px 0; box-shadow: var(--shadow); }
.q-meta { font-size: 12px; color: var(--text-muted); display: flex; gap: 8px; flex-wrap: wrap;
          align-items: center; margin-bottom: 8px; }
.q-meta .tag { background: var(--surface-2); border-radius: 999px; padding: 2px 9px; }
.q-meta .id { font-family: ui-monospace, Menlo, monospace; }
.q-prompt p { margin: 0 0 10px; font-size: 15.5px; }
.opts { list-style: none; padding: 0; margin: 12px 0 0; }
.opts li {
  border: 1px solid var(--border); border-radius: 9px; padding: 9px 12px; margin-bottom: 7px;
  background: var(--surface-1); display: flex; gap: 10px; align-items: flex-start;
}
.opts .lab { font-weight: 700; color: var(--text-muted); min-width: 18px; }
.opts li p, .opts li > span > p { margin: 0; }
.opts li > span { flex: 1; }
.revealed .opts li.correct { border-color: var(--good); background: color-mix(in oklab, var(--good) 9%, var(--surface-1)); }
.revealed .opts li.correct .lab { color: var(--good); }
.expl { display: none; margin-top: 12px; border-top: 1px solid var(--border); padding-top: 12px; }
.revealed .expl { display: block; }
.expl h4 { margin: 14px 0 4px; font-size: 13px; text-transform: uppercase;
           letter-spacing: .05em; color: var(--text-muted); }
.expl .opt-expl { margin: 0 0 10px; padding-left: 12px; border-left: 2px solid var(--border); }
.expl .opt-expl.correct { border-left-color: var(--good); }
.expl .opt-expl p { margin: 4px 0 0; }
.expl .opt-expl strong { color: var(--text-secondary); font-size: 13px; }
.expl p { margin: 0 0 8px; }
.actions { display: flex; gap: 7px; flex-wrap: wrap; margin-top: 12px; }
.actions button {
  font: inherit; font-size: 13px; padding: 5px 12px; border-radius: 7px; cursor: pointer;
  border: 1px solid var(--border); background: var(--surface-1); color: var(--text-secondary);
}
.actions button:hover { color: var(--text-primary); }
.actions button.on { background: var(--surface-2); color: var(--text-primary); font-weight: 600; }
.q[data-mark="known"] { border-left: 3px solid var(--series-3); }
.q[data-mark="unsure"] { border-left: 3px solid var(--series-2); }
.q[data-mark="flagged"] { border-left: 3px solid var(--series-1); }
#more { display: block; width: 100%; margin: 20px 0; padding: 12px; }
.empty { text-align: center; color: var(--text-muted); padding: 60px 20px; }
"""

BROWSE_JS = r"""
const DATA = JSON.parse(document.getElementById('kb-data').textContent);
const PAGE_SIZE = 25;
let shown = PAGE_SIZE;
let marks = {};
try { marks = JSON.parse(localStorage.getItem('kb-marks') || '{}'); } catch (e) {}

const el = id => document.getElementById(id);
const listEl = el('list'), statusEl = el('status'), moreEl = el('more');

function saveMarks() {
  try { localStorage.setItem('kb-marks', JSON.stringify(marks)); } catch (e) {}
}

function filtered() {
  const cert = el('f-cert').value, domain = el('f-domain').value;
  const exam = el('f-exam').value, type = el('f-type').value, mark = el('f-mark').value;
  const terms = el('f-search').value.toLowerCase().split(/\s+/).filter(Boolean);
  return DATA.filter(q => {
    if (cert && q.cert !== cert) return false;
    if (domain && q.domain !== domain) return false;
    if (exam && q.exam !== exam) return false;
    if (type && q.type !== type) return false;
    const m = marks[q.id] || '';
    if (mark === 'unmarked' ? m !== '' : mark && m !== mark) return false;
    if (terms.length && !terms.every(t => q.search.includes(t))) return false;
    return true;
  });
}

function syncDomains() {
  const cert = el('f-cert').value;
  const current = el('f-domain').value;
  const domains = [...new Set(DATA.filter(q => !cert || q.cert === cert).map(q => q.domain))].sort();
  el('f-domain').innerHTML = '<option value="">All domains</option>' +
    domains.map(d => `<option${d === current ? ' selected' : ''}>${d}</option>`).join('');
  if (el('f-domain').value !== current) el('f-domain').value = '';
}

function card(q) {
  const quiz = el('quiz').checked;
  const mark = marks[q.id] || '';
  const opts = q.options.map(o =>
    `<li class="${o.correct ? 'correct' : ''}"><span class="lab">${o.label}</span>
     <span>${o.text}</span></li>`).join('');
  const expl = q.options.map(o =>
    `<div class="opt-expl ${o.correct ? 'correct' : ''}">
       <strong>${o.label}. ${o.correct ? 'Correct' : 'Incorrect'}</strong>${o.explanation}
     </div>`).join('');
  return `<article class="q ${quiz ? '' : 'revealed'}" data-id="${q.id}" data-mark="${mark}">
    <div class="q-meta">
      <span class="tag">${q.cert}</span><span class="tag">${q.domain}</span>
      <span class="tag">${q.type === 'multi_select' ? 'Select many' : 'Select one'}</span>
      <span class="id">${q.id}</span>
    </div>
    <div class="q-prompt">${q.prompt}</div>
    <ul class="opts">${opts}</ul>
    <div class="actions">
      ${quiz ? '<button data-act="reveal">Reveal answer</button>' : ''}
      <button data-act="known" class="${mark === 'known' ? 'on' : ''}">Known</button>
      <button data-act="unsure" class="${mark === 'unsure' ? 'on' : ''}">Unsure</button>
      <button data-act="flagged" class="${mark === 'flagged' ? 'on' : ''}">Flag</button>
    </div>
    <div class="expl">
      <h4>Answer: ${q.correct.join(', ')}</h4>
      ${expl}
      <h4>Overall explanation</h4>
      ${q.overall}
      ${q.refs.length ? '<h4>References</h4><ul class="small">' +
        q.refs.map(r => `<li><a href="${r}" target="_blank" rel="noopener">${r}</a></li>`).join('') +
        '</ul>' : ''}
    </div>
  </article>`;
}

function render() {
  const rows = filtered();
  listEl.innerHTML = rows.length
    ? rows.slice(0, shown).map(card).join('')
    : '<p class="empty">No questions match these filters.</p>';
  const counts = { known: 0, unsure: 0, flagged: 0 };
  rows.forEach(q => { const m = marks[q.id]; if (m) counts[m]++; });
  statusEl.innerHTML = `Showing <b>${Math.min(shown, rows.length)}</b> of <b>${rows.length}</b>` +
    ` matching questions (${DATA.length} total) · <b>${counts.known}</b> known ·` +
    ` <b>${counts.unsure}</b> unsure · <b>${counts.flagged}</b> flagged`;
  moreEl.style.display = rows.length > shown ? 'block' : 'none';
  moreEl.textContent = `Load ${Math.min(PAGE_SIZE, rows.length - shown)} more`;
}

function reset() { shown = PAGE_SIZE; render(); }

['f-cert', 'f-domain', 'f-exam', 'f-type', 'f-mark'].forEach(id =>
  el(id).addEventListener('change', () => {
    if (id === 'f-cert') syncDomains();
    reset();
  }));
el('f-search').addEventListener('input', reset);
el('quiz').addEventListener('change', render);
moreEl.addEventListener('click', () => { shown += PAGE_SIZE; render(); });
el('reset').addEventListener('click', () => {
  ['f-cert', 'f-domain', 'f-exam', 'f-type', 'f-mark'].forEach(id => el(id).value = '');
  el('f-search').value = '';
  syncDomains();
  reset();
});

listEl.addEventListener('click', ev => {
  const button = ev.target.closest('button[data-act]');
  if (!button) return;
  const article = button.closest('.q');
  const act = button.dataset.act;
  if (act === 'reveal') { article.classList.add('revealed'); button.remove(); return; }
  const id = article.dataset.id;
  marks[id] = marks[id] === act ? '' : act;
  if (!marks[id]) delete marks[id];
  saveMarks();
  article.dataset.mark = marks[id] || '';
  article.querySelectorAll('.actions button[data-act]').forEach(b => {
    if (b.dataset.act !== 'reveal') b.classList.toggle('on', marks[id] === b.dataset.act);
  });
  const rows = filtered();
  const counts = { known: 0, unsure: 0, flagged: 0 };
  rows.forEach(q => { const m = marks[q.id]; if (m) counts[m]++; });
  statusEl.innerHTML = statusEl.innerHTML.replace(
    /<b>\d+<\/b> known · <b>\d+<\/b> unsure · <b>\d+<\/b> flagged/,
    `<b>${counts.known}</b> known · <b>${counts.unsure}</b> unsure · <b>${counts.flagged}</b> flagged`);
});

// Deep links from the domain profiles: browse.html#domain=Integration
if (location.hash.startsWith('#domain=')) {
  const wanted = decodeURIComponent(location.hash.slice(8));
  const match = DATA.find(q => q.domain === wanted);
  if (match) { el('f-cert').value = match.cert; syncDomains(); el('f-domain').value = wanted; }
}
render();
"""


def build_browse(stats: dict, questions: list[dict]) -> str:
    payload = []
    for question in questions:
        searchable = " ".join(
            [question["prompt_md"], question["domain"], question["id"]]
            + [o["text_md"] for o in question["options"]]
        ).lower()
        payload.append(
            {
                "id": question["id"],
                "cert": question["certification"]["slug"],
                "domain": question["domain"],
                "exam": f"{question['certification']['slug']}/{question['exam']['slug']}",
                "type": question["type"],
                "prompt": md_to_html(question["prompt_md"]),
                "options": [
                    {
                        "label": o["label"],
                        "text": md_to_html(o["text_md"]),
                        "correct": o["correct"],
                        "explanation": md_to_html(o["explanation_md"]),
                    }
                    for o in question["options"]
                ],
                "correct": question["correct_labels"],
                "overall": md_to_html(question["overall_explanation_md"]),
                "refs": question["references"],
                "search": re.sub(r"\s+", " ", searchable),
            }
        )

    certs = "".join(
        f'<option value="{c["slug"]}">{escape(c["name"])}</option>' for c in stats["certifications"]
    )
    exams = "".join(
        f'<option value="{e["key"]}">{escape(e["title"])}</option>' for e in stats["exams"]
    )
    body = f"""<h1>Browse questions</h1>
<p class="lede">All {len(payload)} questions, filterable and searchable. Quiz mode hides the
answers until you ask for them; your marks are saved in this browser.</p>
<div class="controls">
  <div class="row">
    <select id="f-cert"><option value="">All certifications</option>{certs}</select>
    <select id="f-domain"><option value="">All domains</option></select>
    <select id="f-exam"><option value="">All exams</option>{exams}</select>
    <select id="f-type">
      <option value="">Any format</option>
      <option value="single_select">Select one</option>
      <option value="multi_select">Select many</option>
    </select>
    <select id="f-mark">
      <option value="">Any status</option>
      <option value="unmarked">Unmarked</option>
      <option value="known">Known</option>
      <option value="unsure">Unsure</option>
      <option value="flagged">Flagged</option>
    </select>
  </div>
  <div class="row" style="margin-top:8px">
    <input type="search" id="f-search" placeholder="Search prompts and options…">
    <label class="toggle"><input type="checkbox" id="quiz" checked> Quiz mode</label>
    <button class="ghost" id="reset">Reset</button>
  </div>
  <div class="status" id="status"></div>
</div>
<div id="list"></div>
<button class="ghost" id="more">Load more</button>
<script type="application/json" id="kb-data">{
        json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    }</script>
"""
    return page(
        "Browse · exam-kb", "browse.html", body, extra_css=BROWSE_CSS, extra_js=BROWSE_JS
    )


# --------------------------------------------------------------------------- main


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Render the KB into HTML reports.")
    parser.add_argument("--kb", default="kb", help="knowledge-base directory")
    args = parser.parse_args()

    kb = (repo_root / args.kb).resolve()
    stats_path = kb / "stats.json"
    if not stats_path.exists():
        print(f"missing {stats_path}; run tools/kb_stats.py first", file=sys.stderr)
        return 1

    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    try:
        questions = load_questions(kb)
    except KBNotFound as missing:
        print(f"missing {missing.path}; run tools/parse_udemy.py first", file=sys.stderr)
        return 1

    reports = kb / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    pages = {
        "index.html": build_index(stats),
        "domains.html": build_domains(stats),
        "browse.html": build_browse(stats, questions),
        "reading.html": build_reading(stats),
    }
    for name, html in pages.items():
        (reports / name).write_text(html, encoding="utf-8")
        size = len(html.encode("utf-8"))
        print(f"  {name:14s} {size / 1024:8.0f} KB")

    shown = display_path(reports, repo_root)
    print(f"\nwrote {len(pages)} reports to {shown}")
    print(f"open {shown}/index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
