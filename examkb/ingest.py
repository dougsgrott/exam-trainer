"""`kb/` → SQLite. The one function that makes the database a cache and not a
second source of truth.

Three properties hold, and each one is a test in `tests/test_ingest.py`:

1. **Every key is derived from `kb/` bytes.** `ccao-f/exam-01/q001`,
   `ccao-f/exam-01/q001#A`, `docs/build-with-claude/prompt-caching`. So a rebuild
   from scratch reproduces the projection *including its primary keys*, and a
   journal row that recorded a question id years ago still names the same question.
2. **One transaction.** A crash mid-ingest leaves the previous projection intact
   and queryable -- never a half-written corpus that reads as if it were whole.
3. **Ingest never writes a JOURNAL row.** `INGESTED_TABLES` below is the complete
   write set, and a test fails if a journal table ever appears in it.

The run id **is** the `kb` fingerprint -- sha256 over the shard hashes. That makes
`ingest_run` a record of the corpus states this projection has been built from
rather than a log of every time you typed the command, and it gives 008's stale
banner and 047's doctor a single comparison to make: does the projection's
fingerprint match what is on disk right now? It also means re-ingesting an
unchanged corpus is a genuine no-op -- nothing is written, not even a timestamp.

Default is an **incremental sync**: insert what is new, update what changed, delete
what left `kb/`, and re-attribute the rest. `--rebuild` empties the projection
first, which is the path to take when a *key formula* changes rather than the
content -- a new key can collide with the old row's unique constraint, and the
error that produces is the honest signal to rebuild.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import Table
from sqlalchemy.orm import Session

from examkb import __version__
from examkb.compat import (
    blueprint_files,
    discover_shards,
    load_blueprints,
    load_questions,
    norm,
    normalize_reference,
    read_shard_index,
    slugify,
)
from examkb.models import corpus, metadata
from examkb.models.base import utcnow
from examkb.services import search

# The complete write set, in dependency order: parents before children. Deletes
# walk it backwards. Nothing outside this tuple is touched by an ingest, and a
# test asserts every name in it is a PROJECTION table.
INGESTED_TABLES: tuple[str, ...] = (
    "ingest_run",
    "shard",
    "certification",
    "exam",
    "domain",
    "question",
    "question_option",
    "reference",
    "question_reference",
    # 017. Parents first: a node's `parent_id` points inside its own table, and the
    # rows are generated in document order so a parent is always inserted first.
    "blueprint",
    "blueprint_source",
    "blueprint_node",
    # 018. Derived from the two above it, so it is written by ingest rather than
    # recomputed per request: `ingest --rebuild` has to reproduce the join, and a
    # join that lived in a query would be a second definition of it.
    "blueprint_domain_map",
)

# `kb/` records carry a provider, not an origin: origin is the platform's word for
# where a question came from and it is what 038 filters on. Unknown providers are a
# loud failure -- guessing `vendor_dump` for a future parser's output would quietly
# mislabel generated questions as a vendor's.
ORIGIN_FOR_PROVIDER = {
    "udemy": "vendor_dump",
    "official_sample": "official_sample",
    "local_generation": "local_generation",
    "hand_written": "hand_written",
}


class IngestError(RuntimeError):
    """Ingest refused to write. Carries a message meant for a terminal."""


@dataclass(frozen=True)
class TableChange:
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0

    @property
    def touched(self) -> int:
        return self.inserted + self.updated + self.deleted

    def __str__(self) -> str:
        parts = []
        if self.inserted:
            parts.append(f"+{self.inserted}")
        if self.updated:
            parts.append(f"~{self.updated}")
        if self.deleted:
            parts.append(f"-{self.deleted}")
        return " ".join(parts) or "="


@dataclass(frozen=True)
class IngestResult:
    run_id: str
    fingerprint: str
    mode: str
    questions: int
    shards: int
    elapsed_ms: int
    changes: dict[str, TableChange] = field(default_factory=dict)
    indexed: int = 0
    """Questions written to the FTS5 index (009). Zero when nothing needed it."""

    reindexed_all: bool = False

    @property
    def changed(self) -> bool:
        return any(change.touched for change in self.changes.values())

    def summary(self) -> str:
        head = (
            f"ingest: {self.questions} questions from {self.shards} shard"
            f"{'' if self.shards == 1 else 's'}"
            f" -- {self.mode}, fingerprint {self.fingerprint[:12]}, {self.elapsed_ms} ms"
        )
        if not self.changed and not self.indexed:
            return f"{head}\n  projection already matches kb/; nothing written"
        lines = [
            f"  {name:<20} {change}"
            for name, change in self.changes.items()
            if change.touched
        ]
        if self.indexed:
            what = "rebuilt" if self.reindexed_all else "updated"
            lines.append(f"  {'search index':<20} {what}, {self.indexed} questions")
        return "\n".join([head, *lines])


@dataclass(frozen=True)
class VerifyResult:
    fields_checked: int
    questions_checked: int
    mismatches: list[str]
    missing: list[str]
    unexpected: list[str]

    @property
    def ok(self) -> bool:
        return not (self.mismatches or self.missing or self.unexpected)

    def summary(self) -> str:
        head = (
            f"verify: checked {self.fields_checked} text fields across "
            f"{self.questions_checked} questions"
        )
        if self.ok:
            return f"{head} -- all round-trip"
        problems = [
            *(f"  missing from the projection: {qid}" for qid in self.missing[:10]),
            *(f"  in the projection but not in kb/: {qid}" for qid in self.unexpected[:10]),
            *(f"  {problem}" for problem in self.mismatches[:10]),
        ]
        return "\n".join([head, f"  {len(self.mismatches)} field mismatches", *problems])


# ------------------------------------------------------------------------- fingerprint


def shard_rows(kb: Path) -> list[dict]:
    """Every shard with its provider, line count and sha256, in index order.

    Prefers `kb/shards.json` (003's index, written by `build_shards.py` and by
    nothing else) and falls back to hashing what is on disk, so a corpus that has
    not had `build_shards` run over it still ingests -- with the same fingerprint
    the index would have produced.
    """
    kb = Path(kb)
    index = read_shard_index(kb)
    if index is not None:
        return [
            {
                "path": entry["path"],
                "provider": entry["provider"],
                "line_count": int(entry["line_count"]),
                "sha256": entry["sha256"],
            }
            for entry in index
        ]

    rows = []
    for path in discover_shards(kb):
        raw = path.read_bytes()
        lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()]
        providers = {json.loads(line)["source"]["provider"] for line in lines}
        rows.append(
            {
                "path": str(path.relative_to(kb)),
                "provider": providers.pop() if len(providers) == 1 else "mixed",
                "line_count": len(lines),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return rows


def fingerprint(shards: Iterable[dict]) -> str:
    """sha256 over the shard hashes -- the identity of a corpus state.

    Over the *hashes*, not the bytes: the shards have already been hashed by the
    thing that wrote the index, and a fingerprint that needs to re-read 3 MB of
    JSONL is a fingerprint nobody puts on a page load (008's stale banner does).
    """
    digest = hashlib.sha256()
    for shard in shards:
        digest.update(f"{shard['path']}:{shard['sha256']}\n".encode())
    return digest.hexdigest()


def blueprint_rows(kb: Path) -> list[dict]:
    """Every blueprint file with its sha256, in path order.

    The same shape as `shard_rows`, so both go through one `fingerprint()`. They
    have to: 006 hashed question shards only, and a blueprint edited afterwards
    would have left the projection serving a stale outline while reporting itself
    current -- which is the exact failure the fingerprint exists to prevent.
    """
    kb = Path(kb)
    rows = []
    for path in blueprint_files(kb):
        raw = path.read_bytes()
        rows.append(
            {
                "path": str(path.relative_to(kb)),
                "provider": "blueprint",
                "line_count": len(raw.splitlines()),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return rows


def corpus_rows(kb: Path) -> list[dict]:
    """Everything the fingerprint covers: question shards, then blueprints."""
    return [*shard_rows(kb), *blueprint_rows(kb)]


def kb_fingerprint(kb: Path) -> str:
    return fingerprint(corpus_rows(kb))


# ----------------------------------------------------------------------------- rows
#
# One function per table, each a pure function of the corpus. They are pure so the
# "reproduces every primary key" invariant is something you can read rather than
# something you have to run the ingest twice to believe.


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def question_hashes(question: dict) -> dict[str, str]:
    """The cheap dedup layer (033): exact, normalised, and whole-item.

    033 owns the threshold and the expensive layer (FTS5 BM25 top-20, then Jaccard
    against those twenty); these three columns are what lets it skip that work
    entirely for an exact or near-exact hit. Normalisation is deliberately blunt --
    case and whitespace -- because anything cleverer is a judgement call that
    belongs with the gate, not with the projection.
    """
    prompt = question["prompt_md"]
    normalised = norm(prompt).casefold()
    options = "␟".join(option["text_md"] for option in question["options"])
    return {
        "prompt_sha256": _sha256(prompt),
        "normalized_sha256": _sha256(normalised),
        "item_sha256": _sha256(f"{normalised}␞{norm(options).casefold()}"),
    }


def origin_of(question: dict) -> str:
    if question.get("origin"):
        return question["origin"]
    provider = question["source"]["provider"]
    try:
        return ORIGIN_FOR_PROVIDER[provider]
    except KeyError:
        raise IngestError(
            f"{question['id']}: unknown source provider {provider!r}; "
            "add it to ORIGIN_FOR_PROVIDER with the origin it means"
        ) from None


def certification_id(question: dict) -> str:
    return question["certification"]["slug"]


def exam_id(question: dict) -> str | None:
    exam = question.get("exam") or {}
    if not exam.get("slug"):
        return None
    return f"{certification_id(question)}/{exam['slug']}"


def domain_id(question: dict) -> str | None:
    label = question.get("domain")
    if not label:
        return None
    return f"{certification_id(question)}/{slugify(label)}"


def option_id(question_id: str, label: str) -> str:
    return f"{question_id}#{label}"


def build_rows(
    questions: list[dict],
    shards: list[dict],
    run_id: str,
    *,
    blueprints: list[dict] | None = None,
) -> dict[str, list[dict]]:
    """The whole projection, as plain dicts, keyed by table name."""
    shard_for_provider: dict[str, str] = {}
    for shard in shards:
        shard_for_provider.setdefault(shard["provider"], shard["path"])

    certifications: dict[str, dict] = {}
    exams: dict[str, dict] = {}
    domains: dict[str, dict] = {}
    references: dict[str, dict] = {}
    reference_urls: dict[str, set[str]] = {}
    question_rows: list[dict] = []
    option_rows: list[dict] = []
    citation_rows: list[dict] = []

    for question in questions:
        cert = question["certification"]
        cert_id = cert["slug"]
        certification = certifications.setdefault(
            cert_id,
            {
                "id": cert_id,
                "name": cert["name"],
                "vendor": cert["vendor"],
                "level": cert.get("level"),
                "question_count": 0,
                "ingest_run_id": run_id,
            },
        )
        certification["question_count"] += 1

        exam = question.get("exam") or {}
        this_exam_id = exam_id(question)
        if this_exam_id is not None:
            row = exams.setdefault(
                this_exam_id,
                {
                    "id": this_exam_id,
                    "certification_id": cert_id,
                    "slug": exam["slug"],
                    "title": exam.get("title") or exam["slug"],
                    "number": exam.get("number"),
                    "mode": exam.get("mode"),
                    "question_count": 0,
                    "ingest_run_id": run_id,
                },
            )
            row["question_count"] += 1

        this_domain_id = domain_id(question)
        if this_domain_id is not None:
            row = domains.setdefault(
                this_domain_id,
                {
                    "id": this_domain_id,
                    "certification_id": cert_id,
                    # The vendor's string, byte-identical. `slug` is for URLs and is
                    # never what anything joins on (018 joins on `label`, exactly).
                    "label": question["domain"],
                    "slug": slugify(question["domain"]),
                    "question_count": 0,
                    "ingest_run_id": run_id,
                },
            )
            row["question_count"] += 1

        question_rows.append(
            {
                "id": question["id"],
                "content_hash": question["content_hash"],
                "certification_id": cert_id,
                "exam_id": this_exam_id,
                "domain_id": this_domain_id,
                "domain_label": question.get("domain"),
                "question_number": exam.get("question_number"),
                "type": question["type"],
                "select_count": question.get("select_count") or 1,
                "prompt_md": question["prompt_md"],
                "overall_explanation_md": question.get("overall_explanation_md"),
                "correct_labels": list(question["correct_labels"]),
                "origin": origin_of(question),
                "source_provider": question["source"]["provider"],
                "source_file": question["source"].get("file"),
                "source_course_url": question["source"].get("course_url"),
                "shard_id": shard_for_provider.get(question["source"]["provider"]),
                **question_hashes(question),
                "ingest_run_id": run_id,
            }
        )

        for position, option in enumerate(question["options"]):
            option_rows.append(
                {
                    "id": option_id(question["id"], option["label"]),
                    "question_id": question["id"],
                    "label": option["label"],
                    "position": position,
                    "text_md": option["text_md"],
                    "is_correct": bool(option["correct"]),
                    "explanation_md": option.get("explanation_md"),
                }
            )

        seen: set[str] = set()
        for position, url in enumerate(question.get("references") or []):
            reference_id = normalize_reference(url)
            reference_urls.setdefault(reference_id, set()).add(url)
            row = references.setdefault(
                reference_id,
                {
                    "id": reference_id,
                    "display_url": url,
                    "host": _host(url),
                    "question_count": 0,
                    "ingest_run_id": run_id,
                },
            )
            if reference_id not in seen:
                # A question citing the same page under two mirror hosts cites it
                # once: the reading list (023) must not recommend one page twice.
                seen.add(reference_id)
                row["question_count"] += 1
                citation_rows.append(
                    {
                        "question_id": question["id"],
                        "reference_id": reference_id,
                        "position": position,
                        "raw_url": url,
                    }
                )

    for reference_id, urls in reference_urls.items():
        # Deterministic, so two rebuilds agree on which mirror is displayed.
        chosen = sorted(urls)[0]
        references[reference_id]["display_url"] = chosen
        references[reference_id]["host"] = _host(chosen)

    blueprint_rows_out, source_rows, node_rows = blueprint_tables(blueprints or [], run_id)
    # A blueprint may be the *only* thing a certification has -- phase 8's whole
    # point is a certification that arrives as an outline and nothing else -- so it
    # contributes a `certification` row when the corpus has none. Questions win
    # where both exist: the corpus owns its own names and its own count.
    for row in blueprint_rows_out:
        name = row.pop("_certification_name", None)
        certifications.setdefault(
            row["certification_id"],
            {
                "id": row["certification_id"],
                "name": name or row["certification_id"],
                "vendor": row["vendor"],
                "level": None,
                "question_count": 0,
                "ingest_run_id": run_id,
            },
        )

    return {
        "shard": [{**shard, "id": shard["path"], "ingest_run_id": run_id} for shard in shards],
        "certification": sorted(certifications.values(), key=lambda row: row["id"]),
        "exam": sorted(exams.values(), key=lambda row: row["id"]),
        "domain": sorted(domains.values(), key=lambda row: row["id"]),
        "question": question_rows,
        "question_option": option_rows,
        "reference": sorted(references.values(), key=lambda row: row["id"]),
        "question_reference": citation_rows,
        "blueprint": blueprint_rows_out,
        "blueprint_source": source_rows,
        "blueprint_node": node_rows,
        "blueprint_domain_map": domain_map_rows(
            sorted(domains.values(), key=lambda row: row["id"]),
            blueprint_rows_out,
            node_rows,
        ),
    }


def domain_map_rows(
    domain_rows: list[dict], blueprint_rows: list[dict], node_rows: list[dict]
) -> list[dict]:
    """Corpus domain -> top-level blueprint node, by exact string equality.

    `=` and nothing else. The vendor's own punctuation is inconsistent across its
    two certifications -- `Governance, Risk, and Responsible Use` against
    `Governance, Safety & Risk Management` -- and every scheme for papering over
    that also quietly joins two domains that genuinely differ. So the label is
    compared byte for byte, and a domain that finds no node simply does not appear
    here. 018's test is what notices; `join_method` is constrained to `exact` in
    the schema so no later change can sneak a fallback in.

    Scoped by certification, because two vendors may well both call a domain
    `Integration`.
    """
    certification_of = {row["id"]: row["certification_id"] for row in blueprint_rows}
    by_label: dict[tuple[str, str], str] = {}
    for node in node_rows:
        if node["depth"] != 1:
            continue
        key = (certification_of[node["blueprint_id"]], node["label"])
        # First blueprint wins if a certification somehow has two current ones;
        # `is_current` is the lever for that and nothing needs it yet (017).
        by_label.setdefault(key, node["id"])

    rows = []
    for domain in domain_rows:
        node_id = by_label.get((domain["certification_id"], domain["label"]))
        if node_id is not None:
            rows.append(
                {"domain_id": domain["id"], "node_id": node_id, "join_method": "exact"}
            )
    return rows


def blueprint_tables(
    blueprints: list[dict], run_id: str
) -> tuple[list[dict], list[dict], list[dict]]:
    """One parsed blueprint file -> its three tables.

    Keys are content-derived like every other PROJECTION key (005): a blueprint's
    is `vendor/certification/version`, a node's is `<blueprint>#<path>`, a source's
    is `<blueprint>#<its own path>`. So `ingest --rebuild` reproduces every one of
    them, and 018's domain map can point at a node id that still means the same
    node next year.

    Nothing here judges the weights. `weights_sum` and `weights_sum_to_100` are
    copied through from the parser, which recorded the arithmetic without deciding
    whether 80/105 is allowed -- it is.
    """
    blueprint_rows: list[dict] = []
    source_rows: list[dict] = []
    node_rows: list[dict] = []

    for blueprint in blueprints:
        blueprint_id = blueprint["id"]
        blueprint_rows.append(
            {
                "id": blueprint_id,
                "certification_id": blueprint["certification"],
                "vendor": blueprint["vendor"],
                "version_label": blueprint.get("version_label"),
                "effective_from": blueprint.get("effective_from"),
                "weight_regime": blueprint["weight_regime"],
                "weights_sum": blueprint.get("weights_sum"),
                "weights_sum_to_100": blueprint.get("weights_sum_to_100"),
                "max_depth": blueprint["max_depth"],
                "is_current": True,
                "ingest_run_id": run_id,
                "_certification_name": blueprint.get("name"),
            }
        )
        for source in blueprint.get("sources") or []:
            source_rows.append(
                {
                    "id": f"{blueprint_id}#{source['path']}",
                    "blueprint_id": blueprint_id,
                    "path": source["path"],
                    "kind": source.get("kind"),
                    "sha256": source["sha256"],
                    "retrieved_on": source.get("retrieved_on"),
                    "note": source.get("note"),
                }
            )
        for node in blueprint.get("nodes") or []:
            path = node["path"]
            parent = path.rsplit(".", 1)[0] if "." in path else None
            node_rows.append(
                {
                    "id": f"{blueprint_id}#{path}",
                    "blueprint_id": blueprint_id,
                    "parent_id": f"{blueprint_id}#{parent}" if parent else None,
                    "path": path,
                    "depth": node["depth"],
                    "ordinal": node["ordinal"],
                    "kind": node.get("kind"),
                    # Byte-identical. The vendor's punctuation is what 018 joins on,
                    # so nothing here may tidy it.
                    "label": node["label"],
                    "weight_pct": node.get("weight_pct"),
                    "weight_min": node.get("weight_min"),
                    "weight_max": node.get("weight_max"),
                }
            )

    return blueprint_rows, source_rows, node_rows


def _host(url: str) -> str | None:
    trimmed = url.split("://", 1)[-1]
    host = trimmed.split("/", 1)[0]
    return host or None


# ------------------------------------------------------------------------------ sync


def _table(name: str) -> Table:
    return metadata.tables[name]


def _key(table: Table, row: dict) -> tuple:
    return tuple(row[column.name] for column in table.primary_key.columns)


def _comparable(table: Table, row: dict) -> dict:
    """Everything that decides "has this row changed", minus the attribution.

    `ingest_run_id` is excluded on purpose: it is the fingerprint of the corpus, so
    including it would mark all 3 300 rows as changed the moment one of them was,
    and the counts a person reads would stop meaning anything.
    """
    return {
        name: row.get(name)
        for name in table.columns.keys()
        if name != "ingest_run_id" and name in row
    }


@dataclass(frozen=True)
class TablePlan:
    """What one table needs, decided before anything is written."""

    name: str
    table: Table
    to_insert: list[dict]
    to_update: list[dict]
    to_delete: list[tuple]
    unchanged: int

    def change(self) -> TableChange:
        return TableChange(
            inserted=len(self.to_insert),
            updated=len(self.to_update),
            deleted=len(self.to_delete),
            unchanged=self.unchanged,
        )


def plan_table(session: Session, name: str, rows: list[dict]) -> TablePlan:
    table = _table(name)
    desired = {_key(table, row): row for row in rows}
    if len(desired) != len(rows):
        raise IngestError(f"{name}: duplicate primary keys in the corpus; refusing to write")

    existing = {
        _key(table, dict(row)): dict(row)
        for row in session.execute(sa.select(table)).mappings()
    }
    to_insert = [row for key, row in desired.items() if key not in existing]
    to_update = [
        row
        for key, row in desired.items()
        if key in existing and _comparable(table, existing[key]) != _comparable(table, row)
    ]
    return TablePlan(
        name=name,
        table=table,
        to_insert=to_insert,
        to_update=to_update,
        to_delete=[key for key in existing if key not in desired],
        unchanged=len(desired) - len(to_insert) - len(to_update),
    )


def apply_deletes(session: Session, plan: TablePlan) -> None:
    """Deletes run children-first, across every table, before any insert.

    Order is the whole difference between this working and a foreign key error: a
    question has to be gone before the exam it sat in can be. The one case this
    does not cover is a surviving row whose *key formula* changed -- the old row is
    still there when the new one is inserted, and a unique constraint says so.
    `--rebuild` is the answer to that, and an error beats a silent half-update.
    """
    for key in plan.to_delete:
        session.execute(
            sa.delete(plan.table).where(
                *[
                    column == value
                    for column, value in zip(plan.table.primary_key.columns, key)
                ]
            )
        )


def apply_upserts(session: Session, plan: TablePlan, run_id: str) -> None:
    if plan.to_insert:
        session.execute(sa.insert(plan.table), plan.to_insert)
    for row in plan.to_update:
        session.execute(_update_by_key(plan.table, row))

    if "ingest_run_id" in plan.table.columns:
        # The rows that did not change still belong to this corpus state. One
        # statement, so re-attributing 549 questions is not 549 statements.
        session.execute(
            sa.update(plan.table)
            .where(plan.table.c.ingest_run_id != run_id)
            .values(ingest_run_id=run_id)
        )


def _update_by_key(table: Table, row: dict):
    statement = sa.update(table)
    for column in table.primary_key.columns:
        statement = statement.where(column == row[column.name])
    return statement.values({
        name: row[name] for name in table.columns.keys() if name in row
    })


def empty_projection(session: Session) -> None:
    """Delete every ingested row, children first. `--rebuild`'s first half."""
    for name in reversed(INGESTED_TABLES):
        if name == "ingest_run":
            continue
        session.execute(sa.delete(_table(name)))


# ---------------------------------------------------------------------------- ingest


def ingest(
    session: Session,
    kb: Path,
    *,
    rebuild: bool = False,
    note: str | None = None,
) -> IngestResult:
    """Project `kb/` into the database. One transaction, committed by the caller.

    The caller owns the transaction on purpose: `examkb ingest` commits, and a test
    that wants to prove a crash changes nothing simply does not.
    """
    started = time.monotonic()
    shards = shard_rows(kb)
    questions = load_questions(kb)
    blueprints = load_blueprints(kb)
    run_id = fingerprint([*shards, *blueprint_rows(kb)])
    mode = "rebuild" if rebuild else "incremental"  # the schema's vocabulary (005)

    rows = build_rows(questions, shards, run_id, blueprints=blueprints)

    run = session.get(corpus.IngestRun, run_id)
    if run is None:
        now = utcnow()
        session.add(
            corpus.IngestRun(
                id=run_id,
                started_at=now,
                finished_at=now,
                mode=mode,
                status="ok",
                kb_fingerprint=run_id,
                shard_count=len(shards),
                question_count=len(questions),
                examkb_version=__version__,
                note=note,
            )
        )
        session.flush()

    # Foreign keys are checked at COMMIT rather than per row, for the duration of
    # this transaction only. 006's two-phase apply already orders deletes
    # children-first *across* tables, but `blueprint_node.parent_id` points inside
    # its own table (017): a parent and its children are deleted in one pass and no
    # row order fixes that in general. Deferring is SQLite's own answer, and it is
    # not a loosening -- every constraint is still enforced, just once, at the end.
    session.execute(sa.text("PRAGMA defer_foreign_keys = ON"))

    if rebuild:
        empty_projection(session)

    # Everything is planned against the current projection before a single row is
    # written, so the two phases below cannot see each other's half-done work.
    plans = [plan_table(session, name, rows[name]) for name in INGESTED_TABLES[1:]]
    for plan in reversed(plans):
        apply_deletes(session, plan)
    for plan in plans:
        apply_upserts(session, plan, run_id)
    changes = {plan.name: plan.change() for plan in plans}

    indexed, rebuilt_index = update_search_index(session, plans, rebuild=rebuild)

    return IngestResult(
        run_id=run_id,
        fingerprint=run_id,
        mode=mode,
        questions=len(questions),
        shards=len(shards),
        elapsed_ms=int((time.monotonic() - started) * 1000),
        changes=changes,
        indexed=indexed,
        reindexed_all=rebuilt_index,
    )


def affected_questions(plans: list[TablePlan]) -> set[str]:
    """Every question whose indexable text could have moved, from the plans.

    Both tables count: a question whose own row is untouched but one of whose
    options changed its text has a different document, and an index that only
    watched `question` would keep serving the old one.
    """
    touched: set[str] = set()
    for plan in plans:
        if plan.name == "question":
            touched.update(row["id"] for row in plan.to_insert)
            touched.update(row["id"] for row in plan.to_update)
            touched.update(key[0] for key in plan.to_delete)
        elif plan.name == "question_option":
            touched.update(row["question_id"] for row in plan.to_insert)
            touched.update(row["question_id"] for row in plan.to_update)
            # The option key is `<question id>#<label>`; no question id contains a
            # `#`, which is what makes this split safe rather than clever.
            touched.update(key[0].rsplit("#", 1)[0] for key in plan.to_delete)
    return touched


def update_search_index(
    session: Session, plans: list[TablePlan], *, rebuild: bool
) -> tuple[int, bool]:
    """Bring the FTS5 index (009) in line, in this same transaction.

    Inside the transaction on purpose: the index is part of the projection, and a
    crash between writing the rows and indexing them would leave a database that
    answers `SELECT` correctly and `MATCH` wrongly -- the worst of the two, because
    nothing looks broken.

    The consistency check afterwards is the self-heal: a database migrated to 0002
    but never re-ingested, or one where something went wrong, gets a full rebuild
    rather than a search that silently returns less than it should.
    """
    if not search.index_exists(session):
        raise IngestError(
            f"{search.FTS_TABLE} is missing -- this database is behind its migrations; "
            "run `examkb db upgrade`"
        )

    if rebuild:
        return search.rebuild(session), True

    indexed = search.reindex(session, affected_questions(plans))
    if not search.is_consistent(session):
        return search.rebuild(session), True
    return indexed, False


# ---------------------------------------------------------------------------- verify


def verify(session: Session, kb: Path) -> VerifyResult:
    """Round-trip every text field in `kb/` through the projection and back.

    The mirror of `tools/verify_lossless.py`, one step further down the pipeline:
    that one proves `kb/` lost nothing from the source HTML, this one proves the
    projection lost nothing from `kb/`. Two proofs end to end, no gap between them.
    """
    questions = load_questions(kb)
    stored_questions = {
        row["id"]: row
        for row in session.execute(
            sa.select(
                corpus.Question.id,
                corpus.Question.prompt_md,
                corpus.Question.overall_explanation_md,
            )
        ).mappings()
    }
    stored_options: dict[str, dict] = {}
    for row in session.execute(
        sa.select(
            corpus.QuestionOption.id,
            corpus.QuestionOption.text_md,
            corpus.QuestionOption.explanation_md,
        )
    ).mappings():
        stored_options[row["id"]] = dict(row)

    checked = 0
    mismatches: list[str] = []
    missing: list[str] = []

    for question in questions:
        stored = stored_questions.get(question["id"])
        if stored is None:
            missing.append(question["id"])
            continue
        checked += 2
        if stored["prompt_md"] != question["prompt_md"]:
            mismatches.append(f"{question['id']}: prompt_md")
        if stored["overall_explanation_md"] != question.get("overall_explanation_md"):
            mismatches.append(f"{question['id']}: overall_explanation_md")

        for option in question["options"]:
            key = option_id(question["id"], option["label"])
            stored_option = stored_options.get(key)
            checked += 2
            if stored_option is None:
                mismatches.append(f"{key}: missing option")
                continue
            if stored_option["text_md"] != option["text_md"]:
                mismatches.append(f"{key}: text_md")
            if stored_option["explanation_md"] != option.get("explanation_md"):
                mismatches.append(f"{key}: explanation_md")

    unexpected = sorted(set(stored_questions) - {question["id"] for question in questions})
    return VerifyResult(
        fields_checked=checked,
        questions_checked=len(questions) - len(missing),
        mismatches=mismatches,
        missing=missing,
        unexpected=unexpected,
    )


# ------------------------------------------------------------------------- staleness


def projection_fingerprint(session: Session) -> str | None:
    """The corpus state this projection was built from, or None if never ingested.

    There is exactly one: every projection row carries the same `ingest_run_id`
    after a successful ingest, so "is this stale?" is one comparison against
    `kb_fingerprint(kb)` -- which is what 008's banner and 047's doctor ask.
    """
    return session.scalar(sa.select(corpus.Question.ingest_run_id).limit(1))


def is_stale(session: Session, kb: Path) -> bool:
    current = projection_fingerprint(session)
    return current is None or current != kb_fingerprint(kb)
