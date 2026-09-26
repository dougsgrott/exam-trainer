"""Full-text search over the corpus, and the index it runs on.

Two halves that have to agree, so they live in one file:

**The index.** One FTS5 table, `question_fts`, holding three searchable columns
per question -- the prompt, every option's text, and every explanation -- keyed by
an `UNINDEXED` question id. Its DDL belongs to `alembic/versions/0002`, and this
module never writes a second copy of it: a rebuild reads the statement back out of
`sqlite_master` and re-executes it. Ingest (006) calls in here after its upserts,
inside the same transaction, so a crash leaves the index and the projection
agreeing with each other rather than one of them half-updated.

**The search.** FTS5's query language is not a thing to hand user input to. Four of
the five strings the acceptance criteria name -- `"unclosed`, `AND`, `*`, `-foo` --
raise `OperationalError` against a raw `MATCH`, and stripping the dangerous
characters is a game that ends with the one you forgot. So every term the person
typed is re-emitted as an FTS5 **string literal**, which turns `AND` into the word
"and" and `*` into nothing at all. The only operator that survives is a trailing
`*`, because prefix search is worth keeping and is unambiguous.

Snippets come back as **text segments**, never HTML. `[(text, matched), ...]` lets
the template decide what a hit looks like, and means the corpus's own
`<instructions>` renders literally without anyone remembering to escape it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace

import sqlalchemy as sa
from sqlalchemy.orm import Session

from examkb.services.marks import CLEARED

FTS_TABLE = "question_fts"

# FTS5 creates five shadow tables beside the virtual one. They are the index's
# private business: `alembic/env.py` skips them so autogenerate does not propose
# dropping search on every run, and `tests/test_schema.py` counts them separately.
SHADOW_SUFFIXES: tuple[str, ...] = ("_config", "_content", "_data", "_docsize", "_idx")
FTS_TABLES: tuple[str, ...] = (FTS_TABLE, *(FTS_TABLE + suffix for suffix in SHADOW_SUFFIXES))

# One weight per column, in the table's column order, for `bm25()`. A hit in the
# prompt is what the person is looking for; a hit in an explanation is usually the
# same words restated, and ranking those first buries the question that was asked.
COLUMN_WEIGHTS: tuple[float, ...] = (0.0, 10.0, 3.0, 1.0)  # id, prompt, options, explanation

# Snippet delimiters. Control characters on purpose: they cannot occur in the
# corpus (a test asserts it), so splitting on them cannot cut a snippet in the
# wrong place, and nothing downstream has to escape them.
MARK_OPEN = "\x02"
MARK_CLOSE = "\x03"
ELLIPSIS = "…"
SNIPPET_TOKENS = 14

# SQLite's default limit is 999 bound parameters; reindexing a large change set
# would sail past it with one `IN (...)`.
CHUNK = 400

DEFAULT_LIMIT = 20


class SearchError(Exception):
    """Raised when the index is missing. Everything else is handled, not raised."""


# --------------------------------------------------------------------------- the query
#
# `parse` turns whatever was typed into an FTS5 expression, or into None when
# there is nothing left to search for. None is not an error: a blank box plus a
# domain filter should list the domain, so an empty query means "no text filter".

_QUOTED = re.compile(r'"([^"]*)"?')
_HAS_WORD = re.compile(r"\w", re.UNICODE)


@dataclass(frozen=True)
class Query:
    """What the person typed, and the expression it safely became."""

    raw: str
    terms: tuple[str, ...] = ()
    """Each term as it will be matched, without the quoting. For highlighting."""

    expression: str | None = None
    """The FTS5 `MATCH` expression, or None when there is nothing to match on."""

    @property
    def is_empty(self) -> bool:
        return self.expression is None


def _literal(term: str, *, prefix: bool = False) -> str:
    """One FTS5 string literal. Doubling the quote is the whole escape."""
    return '"' + term.replace('"', '""') + '"' + ("*" if prefix else "")


def parse(raw: str) -> Query:
    """Whatever was typed -> an expression FTS5 cannot choke on.

    Quoted runs stay phrases, including an unterminated one at the end (which is
    what a person who is still typing has). Everything else is split on whitespace
    and each word becomes its own literal, ANDed together.
    """
    raw = raw or ""
    terms: list[str] = []
    pieces: list[str] = []

    position = 0
    for match in _QUOTED.finditer(raw):
        for word in raw[position : match.start()].split():
            term, prefix = _bare(word)
            if term:
                terms.append(term)
                pieces.append(_literal(term, prefix=prefix))
        phrase = match.group(1).strip()
        if _HAS_WORD.search(phrase):
            terms.append(phrase)
            pieces.append(_literal(phrase))
        position = match.end()

    for word in raw[position:].split():
        term, prefix = _bare(word)
        if term:
            terms.append(term)
            pieces.append(_literal(term, prefix=prefix))

    if not pieces:
        return Query(raw=raw)
    return Query(raw=raw, terms=tuple(terms), expression=" AND ".join(pieces))


def _bare(word: str) -> tuple[str, bool]:
    """One unquoted word -> (term, is_prefix_search).

    A trailing `*` is the one piece of syntax that survives being typed by a
    person, because it means what they think it means. A word with no word
    character left in it -- `*`, `-`, `:` -- is dropped rather than turned into an
    empty literal that matches nothing.
    """
    prefix = word.endswith("*")
    term = word.rstrip("*").strip()
    if not _HAS_WORD.search(term):
        return "", False
    return term, prefix


# --------------------------------------------------------------------- the documents
#
# One row per question, three searchable columns. The same three strings the
# migration's back-fill builds in SQL -- `test_search.py` asserts the two agree,
# because "the migration filled it in" and "a rebuild filled it in" have to mean
# the same thing.


@dataclass(frozen=True)
class Document:
    question_id: str
    prompt: str
    options: str
    explanation: str

    def row(self) -> dict:
        return {
            "question_id": self.question_id,
            "prompt": self.prompt,
            "options": self.options,
            "explanation": self.explanation,
        }


_DOCUMENT_SQL = """
SELECT question.id            AS question_id,
       question.prompt_md     AS prompt_md,
       question.overall_explanation_md AS overall_md,
       question_option.position   AS position,
       question_option.text_md    AS text_md,
       question_option.explanation_md AS option_explanation_md
  FROM question
  LEFT JOIN question_option ON question_option.question_id = question.id
 {where}
 ORDER BY question.id, question_option.position
"""


def documents(session: Session, question_ids: Sequence[str] | None = None) -> list[Document]:
    """Build the indexable text for every question, or for the ones named."""
    if question_ids is None:
        rows = session.execute(sa.text(_DOCUMENT_SQL.format(where=""))).mappings().all()
        return list(_group(rows))

    found: list[Document] = []
    for chunk in _chunks(list(question_ids)):
        statement = sa.text(_DOCUMENT_SQL.format(where="WHERE question.id IN :ids")).bindparams(
            sa.bindparam("ids", expanding=True)
        )
        rows = session.execute(statement, {"ids": chunk}).mappings().all()
        found.extend(_group(rows))
    return found


def _group(rows: Iterable[sa.RowMapping]) -> Iterator[Document]:
    current: str | None = None
    prompt = overall = ""
    texts: list[str] = []
    explanations: list[str] = []

    def finish() -> Document:
        body = (overall or "") + "\n" + "\n".join(explanations)
        return Document(
            question_id=current,  # type: ignore[arg-type]
            prompt=prompt,
            options="\n".join(texts),
            # `trim(..., char(10) || ' ')` in the migration; the same thing here.
            explanation=body.strip("\n "),
        )

    for row in rows:
        if row["question_id"] != current:
            if current is not None:
                yield finish()
            current = row["question_id"]
            prompt = row["prompt_md"] or ""
            overall = row["overall_md"] or ""
            texts, explanations = [], []
        if row["position"] is not None:
            texts.append(row["text_md"] or "")
            if row["option_explanation_md"] is not None:
                explanations.append(row["option_explanation_md"])
    if current is not None:
        yield finish()


def _chunks(values: list[str], size: int = CHUNK) -> Iterator[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


# ----------------------------------------------------------------- index maintenance


def index_exists(session: Session) -> bool:
    return bool(
        session.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
            {"name": FTS_TABLE},
        ).first()
    )


def index_definition(session: Session) -> str:
    """The `CREATE VIRTUAL TABLE` statement, read from the database that has it.

    This is why there is no copy of the DDL in this module. The migration owns the
    definition; a rebuild re-executes whatever the database was actually built
    with, so the two cannot drift and a future migration that changes the tokenizer
    is picked up by the next rebuild without touching this file.
    """
    sql = session.scalar(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": FTS_TABLE},
    )
    if not sql:
        raise SearchError(
            f"{FTS_TABLE} does not exist -- the database is behind its migrations; "
            "run `examkb db upgrade`"
        )
    return sql


def row_count(session: Session) -> int:
    return int(session.scalar(sa.text(f"SELECT count(*) FROM {FTS_TABLE}")) or 0)


def orphan_count(session: Session) -> int:
    """Index rows whose question is gone. The number that must always be zero."""
    return int(
        session.scalar(
            sa.text(
                f"SELECT count(*) FROM {FTS_TABLE} "
                "WHERE question_id NOT IN (SELECT id FROM question)"
            )
        )
        or 0
    )


def is_consistent(session: Session) -> bool:
    """One row per question, and no row without one."""
    questions = int(session.scalar(sa.text("SELECT count(*) FROM question")) or 0)
    return row_count(session) == questions and orphan_count(session) == 0


def rebuild(session: Session) -> int:
    """Drop the index, recreate it from its own DDL, and fill it. Returns the rows.

    Dropped and recreated rather than emptied, and that is not a style choice:
    FTS5 keeps the deletions in its segment structure, so `DELETE FROM` + re-insert
    leaves a *different* database from a fresh build with the same content. 006's
    invariant is that two rebuilds are byte-identical, and this is what keeps it
    true now that there is an index in the file.
    """
    definition = index_definition(session)
    session.execute(sa.text(f"DROP TABLE {FTS_TABLE}"))
    session.execute(sa.text(definition))
    rows = [document.row() for document in documents(session)]
    _insert(session, rows)
    return len(rows)


def reindex(session: Session, question_ids: Iterable[str]) -> int:
    """Re-index exactly these questions, dropping any whose row has gone.

    Delete first, then insert what still exists: a question that left `kb/` is
    deleted and never re-inserted, which is the whole of "no orphaned FTS rows".
    """
    ids = sorted(set(question_ids))
    if not ids:
        return 0
    for chunk in _chunks(ids):
        statement = sa.text(
            f"DELETE FROM {FTS_TABLE} WHERE question_id IN :ids"
        ).bindparams(sa.bindparam("ids", expanding=True))
        session.execute(statement, {"ids": chunk})

    rows = [document.row() for document in documents(session, ids)]
    _insert(session, rows)
    return len(rows)


def _insert(session: Session, rows: list[dict]) -> None:
    if not rows:
        return
    session.execute(
        sa.text(
            f"INSERT INTO {FTS_TABLE} (question_id, prompt, options, explanation) "
            "VALUES (:question_id, :prompt, :options, :explanation)"
        ),
        rows,
    )


# ------------------------------------------------------------------------ the search


# The value a facet uses for "the vendor did not say". All 189 ccar-p questions sit
# in exams with no `mode`, so a mode facet that drops NULL sums to 360 rather than
# 549 -- and "the counts add up" is the whole point of showing them. `-` because no
# vendor word is `-`; `test_browse.py` asserts no real value collides with it.
UNSPECIFIED = "-"

# The current mark, as the UI thinks of it. `current_mark` (005's view) returns the
# latest row whatever it says, and the latest row can be `cleared` -- which means
# "not marked", not "marked cleared". Folding that to NULL here is what makes the
# `-` bucket mean the same thing in the filter, in the facet and on the page.
MARK_EXPRESSION = (
    f"CASE WHEN current_mark.value IS NULL OR current_mark.value = '{CLEARED}' "
    "THEN NULL ELSE current_mark.value END"
)


@dataclass(frozen=True)
class SearchFilters:
    """The projection's own columns, plus the one join a person filters by.

    `exam_mode` lives on `exam`, not on `question`, which is why the query below
    always joins it. Marks (011) and tags (028) are joins this does not know about
    yet; each adds itself here when it exists rather than being anticipated now.
    """

    certification_id: str | None = None
    exam_id: str | None = None
    domain_id: str | None = None
    domain_label: str | None = None
    type: str | None = None
    origin: str | None = None
    exam_mode: str | None = None
    mark: str | None = None

    COLUMNS = {
        "certification_id": "question.certification_id",
        "exam_id": "question.exam_id",
        "domain_id": "question.domain_id",
        "domain_label": "question.domain_label",
        "type": "question.type",
        "origin": "question.origin",
        "exam_mode": "exam.mode",
        "mark": MARK_EXPRESSION,
    }

    def clauses(self) -> tuple[list[str], dict[str, object]]:
        """SQL fragments and their bound values. Column names never come from input."""
        fragments: list[str] = []
        values: dict[str, object] = {}
        for name, column in self.COLUMNS.items():
            value = getattr(self, name)
            if value is None:
                continue
            if value == UNSPECIFIED:
                # Filtering *for* the absence of a value. `= NULL` is never true, so
                # this has to be spelled out rather than bound.
                fragments.append(f"{column} IS NULL")
                continue
            fragments.append(f"{column} = :f_{name}")
            values[f"f_{name}"] = value
        return fragments, values

    @property
    def any(self) -> bool:
        return bool(self.clauses()[0])

    def without(self, name: str) -> SearchFilters:
        """The same filters with one field cleared. For "show me the alternatives"."""
        return replace(self, **{name: None})


# One FROM for every question this module counts, ranks or lists, so a facet count
# and a result count cannot be computed over different sets. `exam` and
# `certification` are LEFT-joined because a question is allowed to have neither and
# an INNER join would quietly drop it from its own totals.
_JOINS = (
    " LEFT JOIN exam ON exam.id = question.exam_id"
    " LEFT JOIN certification ON certification.id = question.certification_id"
    " LEFT JOIN current_mark ON current_mark.question_id = question.id"
)
_FROM_ALL = f"FROM question{_JOINS}"
_FROM_MATCH = (
    f"FROM {FTS_TABLE}"
    f" JOIN question ON question.id = {FTS_TABLE}.question_id"
    f"{_JOINS}"
)


def _scope(query: Query, filters: SearchFilters) -> tuple[str, dict[str, object]]:
    """`FROM ... WHERE ...` plus its bound values: the set under discussion."""
    fragments, values = filters.clauses()
    if query.is_empty:
        where = (" WHERE " + " AND ".join(fragments)) if fragments else ""
        return _FROM_ALL + where, values
    clauses = [f"{FTS_TABLE} MATCH :match", *fragments]
    values = {**values, "match": query.expression}
    return _FROM_MATCH + " WHERE " + " AND ".join(clauses), values


@dataclass(frozen=True)
class SnippetPart:
    text: str
    matched: bool = False


@dataclass(frozen=True)
class Snippet:
    """A snippet as segments, never as HTML.

    The template renders `<mark>` around the matched parts; nothing here produces
    markup, so corpus text containing `<instructions>` survives to the page as
    those exact characters and Jinja escapes it like any other string.
    """

    parts: tuple[SnippetPart, ...] = ()

    @property
    def text(self) -> str:
        return "".join(part.text for part in self.parts)

    @property
    def has_match(self) -> bool:
        return any(part.matched for part in self.parts)

    def __bool__(self) -> bool:
        return bool(self.parts)


def snippet_from(raw: str | None) -> Snippet:
    """Split FTS5's marked-up snippet on the sentinels into typed segments."""
    if not raw:
        return Snippet()
    parts: list[SnippetPart] = []
    for block in raw.split(MARK_OPEN):
        if MARK_CLOSE in block:
            matched, rest = block.split(MARK_CLOSE, 1)
            if matched:
                parts.append(SnippetPart(matched, True))
            if rest:
                parts.append(SnippetPart(rest, False))
        elif block:
            parts.append(SnippetPart(block, False))
    return Snippet(tuple(parts))


@dataclass(frozen=True)
class SearchHit:
    question_id: str
    score: float
    snippet: Snippet
    certification_id: str | None = None
    certification_name: str | None = None
    exam_id: str | None = None
    exam_title: str | None = None
    exam_mode: str | None = None
    domain_label: str | None = None
    type: str | None = None
    question_number: int | None = None
    select_count: int = 1
    mark: str | None = None
    prompt_md: str = ""

    @property
    def mode(self) -> str:
        """What the vendor called this exam, or the word for not having said."""
        return self.exam_mode or "unspecified"


@dataclass(frozen=True)
class SearchResults:
    query: Query
    total: int
    hits: list[SearchHit] = field(default_factory=list)
    limit: int = DEFAULT_LIMIT
    offset: int = 0

    @property
    def searched(self) -> bool:
        return not self.query.is_empty

    def __len__(self) -> int:
        return len(self.hits)


_SELECT_COLUMNS = """
       question.id            AS question_id,
       question.certification_id AS certification_id,
       certification.name     AS certification_name,
       question.exam_id       AS exam_id,
       exam.title             AS exam_title,
       exam.mode              AS exam_mode,
       question.domain_label  AS domain_label,
       question.type          AS type,
       question.question_number AS question_number,
       question.select_count  AS select_count,
       CASE WHEN current_mark.value IS NULL OR current_mark.value = 'cleared'
            THEN NULL ELSE current_mark.value END AS mark,
       question.prompt_md     AS prompt_md
"""


def search_questions(
    session: Session,
    q: str = "",
    *,
    filters: SearchFilters | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> SearchResults:
    """Search, filter, rank. Composes with the filters rather than replacing them.

    An unparseable or empty `q` is not an error: it means "no text filter", so the
    filters alone decide the result and the order is the question id. That is what
    a blank search box next to a domain dropdown should do.

    Two statements, always -- the count and the page -- whatever the page size is.
    Everything the list renders comes back in the second one, because a template
    that reaches back for an exam title is an N+1 waiting for a bigger page.
    """
    filters = filters or SearchFilters()
    query = parse(q)
    limit = max(0, int(limit))
    offset = max(0, int(offset))

    if not query.is_empty and not index_exists(session):
        raise SearchError(
            f"{FTS_TABLE} does not exist -- run `examkb db upgrade` and `examkb ingest`"
        )

    scope, values = _scope(query, filters)
    total = int(session.scalar(sa.text(f"SELECT count(*) {scope}"), values) or 0)

    if query.is_empty:
        rows = session.execute(
            sa.text(
                f"SELECT {_SELECT_COLUMNS}, 0.0 AS score, NULL AS snip {scope} "
                "ORDER BY question.id LIMIT :limit OFFSET :offset"
            ),
            {**values, "limit": limit, "offset": offset},
        ).mappings().all()
    else:
        weights = ", ".join(str(weight) for weight in COLUMN_WEIGHTS)
        rows = session.execute(
            sa.text(
                f"SELECT {_SELECT_COLUMNS},"
                f" bm25({FTS_TABLE}, {weights}) AS score,"
                f" snippet({FTS_TABLE}, -1, :mark_open, :mark_close, :ellipsis, :tokens) AS snip "
                f"{scope} "
                "ORDER BY score, question.id LIMIT :limit OFFSET :offset"
            ),
            {
                **values,
                "mark_open": MARK_OPEN,
                "mark_close": MARK_CLOSE,
                "ellipsis": ELLIPSIS,
                "tokens": SNIPPET_TOKENS,
                "limit": limit,
                "offset": offset,
            },
        ).mappings().all()

    hits = [_hit(row, snippet_from(row["snip"])) for row in rows]
    return SearchResults(query=query, total=total, hits=hits, limit=limit, offset=offset)


def _hit(row: sa.RowMapping, snippet: Snippet) -> SearchHit:
    return SearchHit(
        question_id=row["question_id"],
        score=float(row["score"] or 0.0),
        snippet=snippet,
        certification_id=row["certification_id"],
        certification_name=row["certification_name"],
        exam_id=row["exam_id"],
        exam_title=row["exam_title"],
        exam_mode=row["exam_mode"],
        domain_label=row["domain_label"],
        type=row["type"],
        question_number=row["question_number"],
        select_count=int(row["select_count"] or 1),
        mark=row["mark"],
        prompt_md=row["prompt_md"] or "",
    )


# ---------------------------------------------------------------------------- facets
#
# Every facet is counted over **the same scope as the results**, this filter
# included. That is what makes each dimension sum to the filtered total, and it is
# the criterion 010 states. The consequence is that choosing a domain leaves that
# domain as the only value in its own facet -- so the page renders a selected value
# as a "clear" link, because otherwise there is no way back.


@dataclass(frozen=True)
class FacetSpec:
    name: str
    title: str
    param: str
    """The URL query parameter this facet reads and writes."""

    column: str
    label_column: str
    attribute: str
    """The `SearchFilters` field it sets."""


FACETS: tuple[FacetSpec, ...] = (
    FacetSpec("certification", "Certification", "cert",
              "question.certification_id", "certification.name", "certification_id"),
    FacetSpec("exam", "Exam", "exam", "question.exam_id", "exam.title", "exam_id"),
    FacetSpec("mode", "Mode", "mode", "exam.mode", "exam.mode", "exam_mode"),
    FacetSpec("domain", "Domain", "domain",
              "question.domain_label", "question.domain_label", "domain_label"),
    FacetSpec("type", "Type", "type", "question.type", "question.type", "type"),
    FacetSpec("mark", "Mark", "mark", MARK_EXPRESSION, MARK_EXPRESSION, "mark"),
)


@dataclass(frozen=True)
class FacetValue:
    value: str
    label: str
    count: int
    selected: bool = False


@dataclass(frozen=True)
class Facet:
    name: str
    title: str
    param: str
    values: tuple[FacetValue, ...] = ()
    selected: str | None = None

    @property
    def total(self) -> int:
        return sum(value.count for value in self.values)

    def __bool__(self) -> bool:
        return bool(self.values)


_TYPE_LABELS = {"single_select": "Single select", "multi_select": "Multi select"}


# What "not set" is called, per dimension. A mode with no value is unspecified; a
# question with no mark is unmarked, and calling that "unspecified" would read as
# though somebody had failed to fill something in.
_MISSING_LABELS = {"mark": "unmarked"}


def _facet_label(spec: FacetSpec, value: str | None, label: str | None) -> str:
    if value is None:
        return _MISSING_LABELS.get(spec.name, "unspecified")
    if spec.name == "type":
        return _TYPE_LABELS.get(value, value)
    return label or value


def facets(
    session: Session, q: str = "", *, filters: SearchFilters | None = None
) -> list[Facet]:
    """One `GROUP BY` per dimension over the search's own scope. Five statements."""
    filters = filters or SearchFilters()
    query = parse(q)
    scope, values = _scope(query, filters)

    found: list[Facet] = []
    for spec in FACETS:
        rows = session.execute(
            sa.text(
                f"SELECT {spec.column} AS value, {spec.label_column} AS label, count(*) AS n "
                f"{scope} GROUP BY {spec.column} ORDER BY n DESC, label"
            ),
            values,
        ).mappings().all()
        selected = getattr(filters, spec.attribute)
        found.append(
            Facet(
                name=spec.name,
                title=spec.title,
                param=spec.param,
                values=tuple(
                    FacetValue(
                        # A missing value is still a bucket; see UNSPECIFIED.
                        value=row["value"] if row["value"] is not None else UNSPECIFIED,
                        label=_facet_label(spec, row["value"], row["label"]),
                        count=int(row["n"]),
                        selected=(row["value"] or UNSPECIFIED) == selected,
                    )
                    for row in rows
                ),
                selected=selected,
            )
        )
    return found
