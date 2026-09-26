# exam-kb

A knowledge base for studying professional tech certifications, built from raw source
material (saved course pages, exports) that is parsed into a canonical, queryable form.

The first two sources are Udemy practice-exam packs for the Anthropic certifications:
**549 questions**, each with four options, a per-option explanation, a long-form overall
explanation, a domain tag, and its cited documentation links.

| Certification | Level | Exams | Questions |
| --- | --- | ---: | ---: |
| Claude Certified Associate - Foundations (`ccao-f`) | associate | 6 | 360 |
| Claude Certified Architect - Professional (`ccar-p`) | professional | 3 | 189 |

## Layout

```
data/          raw source material — immutable, never edited
  udemy_courses/<course>/frontmatter.md      course URL
  udemy_courses/<course>/raw_tests/*.html    saved result pages

kb/            generated — safe to delete and rebuild
  exams/<cert>/<exam>.json    canonical, one file per exam
  questions.jsonl             the Udemy shard: 549 questions, one JSON object per line
  questions/<name>.jsonl      every other shard (generated, seeded) — no parser touches these
  shards.json                 the index of shards: path, provider, line count, sha256
  references.json             every cited link, with citation counts
  manifest.json               totals, per-domain counts, warnings, validation results
  stats.json                  every aggregate the reports display
  study/                      readable Markdown (see below)
  reports/                    self-contained HTML dashboards (see below)

plans/         the implementation plans this repo was built from
tools/         the parser, renderers, and report builders
```

`data/` is the source of truth. Everything under `kb/` is regenerated from it and should
never be hand-edited.

Questions live in **shards**, indexed by `kb/shards.json`. Consumers never open a question
file by name — they call `load_questions(kb)` in `tools/_shared.py`, which reads the index
and raises if a shard on disk is missing from it rather than skipping it quietly.

## Regenerating

Every script carries PEP 723 inline metadata, so `uv` handles dependencies:

```bash
uv run tools/parse_udemy.py --check    # data/  -> kb/ canonical JSON + JSONL
uv run tools/build_shards.py           # kb/    -> kb/shards.json (index the shards)
uv run tools/build_kb.py               # kb/    -> kb/study/**.md
uv run tools/verify_lossless.py        # kb/ vs data/, proves nothing was dropped

uv run tools/kb_stats.py               # kb/   -> kb/stats.json + terminal summary
uv run tools/build_report.py           # stats -> kb/reports/*.html
```

They also run under a plain `python3` if `beautifulsoup4` and `lxml` are installed
(only the parser needs them; the stats and report tools are pure standard library).
Output is deterministic: re-running produces byte-identical files.

| Script | Does |
| --- | --- |
| `tools/parse_udemy.py` | Extracts questions from the saved HTML, converts rich text to Markdown, writes the canonical KB, and validates it (`--check` exits non-zero on any problem). Owns `questions.jsonl` and rewrites it wholesale on every run. |
| `tools/build_shards.py` | Writes `kb/shards.json`, the index of question shards. The **only** writer of it — a parser that owned the index would drop every other producer's questions just by running. `--check` verifies the index is current without writing. |
| `tools/build_kb.py` | Renders the canonical KB into `kb/study/` Markdown. Never touches the HTML. |
| `tools/verify_lossless.py` | Re-parses the source, reverses the Markdown conversion of all 5490 text fields, and asserts they match the original exactly. Checks `provider == "udemy"` questions only — the rest have no upstream page — and asserts the field count so a vanished shard fails rather than passing quietly. |
| `tools/kb_stats.py` | Computes every aggregate — coverage, formats, answer-position skew, tf-idf domain fingerprints, merged reference index — into `kb/stats.json`, and prints a terminal summary. |
| `tools/charts.py` | Inline-SVG chart primitives (no dependencies) used by the report builder. |
| `tools/build_report.py` | Renders `kb/stats.json` + the questions into the four HTML reports. Never touches the HTML source. |

## Studying

Start at [`kb/study/index.md`](kb/study/index.md). Questions are grouped by domain, one
file per domain, with the answer and all explanations inside a collapsed `<details>`
block — so a file reads top-to-bottom as a self-quiz. Every cited link is collected in
[`kb/study/references.md`](kb/study/references.md).

## Visualizing

`kb/reports/` holds four standalone pages — all CSS and JS inline, no CDN, no network
needed. Open [`kb/reports/index.html`](kb/reports/index.html) and the nav links the rest.

| Page | What it answers |
| --- | --- |
| `index.html` | How the corpus is shaped: coverage heatmaps, questions per domain, format split, correct-answer position skew, exam-group comparison, text-length distributions. |
| `domains.html` | What each domain is *about* — its distinctive vocabulary, most-cited docs, and spread across exams. |
| `browse.html` | All 549 questions, filterable by certification, domain, exam, format, and full-text search. Quiz mode hides the answers; known / unsure / flagged marks persist in the browser. |
| `reading.html` | The documentation reading list, deduplicated across mirror hosts and ranked by how often the explanations cite each page. |

Terminal statistics without the browser:

```bash
uv run tools/kb_stats.py --top 25      # tables and block-bar charts on stdout
jq '.answer_positions' kb/stats.json   # or query the aggregates directly
```

Notes on the charts: the palette is the `dataviz` reference instance, validated with its
`validate_palette.js` in both light and dark. Domains are nominal, so every bar in a
domain chart carries one hue — bar length alone encodes magnitude. The heatmaps use a
single-hue sequential ramp, the answer-position chart is diverging around its expected
value, and the exam-group comparison is small multiples rather than a dual-axis plot.
Every chart ships direct value labels and a companion table view.

## Question schema

`kb/questions.jsonl` holds one of these per line; `kb/exams/**.json` nests the same
records under an exam.

```jsonc
{
  "id": "ccao-f/exam-01/q001",       // cert / exam / question number
  "content_hash": "fd1c8863d839",    // sha256 of prompt + sorted option texts
  "certification": { "slug": "ccao-f", "name": "…", "vendor": "anthropic", "level": "associate" },
  "exam": { "slug": "exam-01", "title": "…", "number": 1, "mode": "realistic",
            "question_number": 1 },
  "domain": "Prompting and Task Execution",
  "type": "single_select",           // or "multi_select"
  "select_count": 2,                 // how many options are correct
  "prompt_md": "…",
  "options": [
    { "label": "A", "text_md": "…", "correct": false, "explanation_md": "…" }
  ],
  "correct_labels": ["B", "C"],
  "overall_explanation_md": "…",
  "references": ["https://…"],
  "source": { "provider": "udemy", "course_url": "https://…", "file": "data/…html" }
}
```

Every `*_md` field is valid Markdown. The source uses only `<p> <b> <i> <code> <a>`, and
literal text is escaped where Markdown would otherwise interpret it (`Bash(scp \*)`,
`\<thinking\>`); identifiers such as `output_config` are left alone because Markdown does
not emphasise underscores inside a word.

Example queries:

```bash
# every multi-select question in one domain
jq -c 'select(.type=="multi_select" and .domain=="Integration") | .id' kb/questions.jsonl

# question counts per domain
jq -r '.certification.slug + " / " + .domain' kb/questions.jsonl | sort | uniq -c

# the most-cited documentation links
jq -r '.references[:5][] | "\(.count)\t\(.url)"' kb/references.json
```

## Adding another exam source

`tools/parse_udemy.py` targets the Udemy saved-page format specifically. To add more
exams from the same format:

1. Save the results page ("Save page as → complete") into
   `data/udemy_courses/<course>/raw_tests/`, and put the course URL in the course's
   `frontmatter.md`.
2. If it is a new certification, add its exam-code prefix to `CERTIFICATIONS` in
   `tools/parse_udemy.py`.
3. Re-run the commands above, in order.

The parser is deliberately strict: it raises on an unknown rich-text tag, a missing
node, or a question pane it cannot read, rather than silently writing partial data. A
different vendor's export needs its own parser writing the same schema.

On WSL, saved pages come with `:Zone.Identifier` companion files;
`./clean-zone-identifiers.sh` removes them (`-n` to preview).
