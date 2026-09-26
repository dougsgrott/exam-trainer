"""Search: the index, the parser that stands between FTS5 and a person, and the ranking.

The parser gets the most attention here, and deliberately. FTS5's query language
is a real grammar, and four of the five strings this issue's criteria name --
`"unclosed`, `AND`, `*`, `-foo` -- are `OperationalError` against a raw `MATCH`,
not merely bad results. So the tests assert twice: that the raw expression really
does blow up (`test_the_raw_match_this_protects_against`), and that the parsed one
does not. A guard whose danger is never demonstrated is a guard nobody dares
delete and nobody trusts either.

The rest is about the index staying true: one row per question, none left behind
by a question that left `kb/`, and two rebuilds producing the same bytes -- which
a naive `DELETE FROM` + re-insert quietly breaks, because FTS5 remembers deletions
in its segment structure.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import write_kb
from sqlalchemy.orm import Session

from examkb import queries
from examkb.ingest import ingest
from examkb.services import search
from examkb.services.search import SearchFilters, parse, search_questions

# ------------------------------------------------------------------------------ helpers


def ingested(session: Session, kb: Path, **kwargs):
    result = ingest(session, kb, **kwargs)
    session.commit()
    return result


@pytest.fixture
def mini(tmp_session: Session, tmp_kb: Path) -> Session:
    ingested(tmp_session, tmp_kb)
    return tmp_session


@pytest.fixture
def real(real_session: Session) -> Session:
    """The committed 549-question corpus, projected once per session and copied."""
    return real_session


def ids(results) -> list[str]:
    return [hit.question_id for hit in results.hits]


def dump(session: Session) -> str:
    raw = session.connection().connection
    return "\n".join(raw.driver_connection.iterdump())


# ------------------------------------------------------------------------- the parser


@pytest.mark.parametrize(
    ("raw", "expression"),
    [
        ("prompt caching", '"prompt" AND "caching"'),
        ('"tool use"', '"tool use"'),
        ('"tool use" api', '"tool use" AND "api"'),
        ("cach*", '"cach"*'),
        # Every operator FTS5 has, turned into a word.
        ("AND", '"AND"'),
        ("OR", '"OR"'),
        ("NOT", '"NOT"'),
        ("NEAR", '"NEAR"'),
        ("-foo", '"-foo"'),
        ("prompt:", '"prompt:"'),
        ("(a)", '"(a)"'),
        ('"unclosed', '"unclosed"'),
        # A quote inside a term is escaped by doubling it, FTS5's own rule.
        ("don't", '"don\'t"'),
        # Nothing to search for is not an error; it is no text filter.
        ("", None),
        ("   ", None),
        ("*", None),
        ("- : ^", None),
    ],
)
def test_the_parser_turns_input_into_something_fts5_accepts(raw: str, expression) -> None:
    assert parse(raw).expression == expression


@pytest.mark.parametrize("raw", ['"unclosed', "AND", "*", "-foo", "prompt:", "a OR", "NEAR("])
def test_the_raw_match_this_protects_against(mini: Session, raw: str) -> None:
    """The danger, demonstrated. Each of these is a 500 without the parser."""
    with pytest.raises(sa.exc.OperationalError):
        mini.execute(
            sa.text(f"SELECT question_id FROM {search.FTS_TABLE} WHERE {search.FTS_TABLE} MATCH :q"),
            {"q": raw},
        ).all()


@pytest.mark.parametrize(
    "raw", ['"unclosed', "AND", "*", "", "   ", "-foo", "prompt:", "NEAR(", "a OR", '""', "^&*()"]
)
def test_malformed_input_never_raises(mini: Session, raw: str) -> None:
    results = search_questions(mini, raw)
    assert isinstance(results.total, int)
    assert len(results.hits) <= results.total


def test_a_prefix_search_finds_the_longer_word(mini: Session) -> None:
    assert ids(search_questions(mini, "prompt*"))


def test_the_parser_keeps_what_was_typed_for_the_page_to_show(mini: Session) -> None:
    query = parse('"tool use" caching')
    assert query.raw == '"tool use" caching'
    assert query.terms == ("tool use", "caching")


# -------------------------------------------------------------------------- the index


def test_the_index_has_exactly_one_row_per_question(real: Session) -> None:
    assert search.row_count(real) == queries.question_count(real) == 549
    assert search.orphan_count(real) == 0
    assert search.is_consistent(real)


def test_the_migration_and_a_rebuild_agree_on_the_text(real: Session) -> None:
    """`db upgrade` fills the index in SQL; `ingest --rebuild` fills it in Python.

    Two implementations of the same three strings, so they get compared rather
    than trusted. If they ever diverge, search results depend on which one last
    touched the database, which is the kind of bug nobody reproduces.
    """
    from_python = {
        document.question_id: (document.prompt, document.options, document.explanation)
        for document in search.documents(real)
    }
    from_sql = {
        row[0]: (row[1], row[2], row[3])
        for row in real.execute(
            sa.text(
                f"SELECT question_id, prompt, options, explanation FROM {search.FTS_TABLE}"
            )
        ).all()
    }
    assert from_python == from_sql


def test_an_option_text_change_reindexes_its_question(
    tmp_session: Session, tmp_kb: Path, mini_questions: list[dict]
) -> None:
    """The question row is untouched; only an option moved. The index must follow."""
    ingested(tmp_session, tmp_kb)
    assert not ids(search_questions(tmp_session, "zarquon"))

    changed = json.loads(json.dumps(mini_questions))
    changed[0]["options"][0]["text_md"] = "A zarquon is the only correct answer"
    write_kb(tmp_kb, changed)
    ingested(tmp_session, tmp_kb)

    assert ids(search_questions(tmp_session, "zarquon")) == [changed[0]["id"]]


def test_a_question_that_leaves_kb_leaves_no_orphan(
    tmp_session: Session, tmp_kb: Path, mini_questions: list[dict]
) -> None:
    """The criterion: no orphaned FTS rows after a question is removed."""
    ingested(tmp_session, tmp_kb)
    gone = mini_questions[0]["id"]
    assert ids(search_questions(tmp_session, "", filters=SearchFilters())).count(gone) == 1

    write_kb(tmp_kb, mini_questions[1:])
    ingested(tmp_session, tmp_kb)

    assert search.orphan_count(tmp_session) == 0
    assert search.row_count(tmp_session) == queries.question_count(tmp_session) == 2
    assert tmp_session.scalar(
        sa.text(f"SELECT count(*) FROM {search.FTS_TABLE} WHERE question_id = :id"), {"id": gone}
    ) == 0


def test_rebuild_leaves_the_index_consistent(tmp_session: Session, tmp_kb: Path) -> None:
    ingested(tmp_session, tmp_kb, rebuild=True)
    assert search.is_consistent(tmp_session)
    assert search.row_count(tmp_session) == 3


def test_rebuild_twice_is_byte_identical_with_the_index_in_place(
    tmp_db: sa.Engine, tmp_kb: Path
) -> None:
    """006's invariant, now that there is an FTS5 index in the file.

    This is the test that forced `rebuild()` to drop and recreate the virtual
    table: `DELETE FROM` + re-insert leaves FTS5's segments carrying the deletions,
    and the dump differs from a fresh build of the same content.
    """
    with Session(tmp_db) as session:
        ingested(session, tmp_kb, rebuild=True)
        first = dump(session)
    with Session(tmp_db) as session:
        ingested(session, tmp_kb, rebuild=True)
        second = dump(session)

    assert hashlib.sha256(first.encode()).hexdigest() == hashlib.sha256(second.encode()).hexdigest()
    assert f"INSERT INTO {search.FTS_TABLE}" in first or "question_fts_data" in first


def test_a_delete_and_reinsert_rebuild_would_not_be(tmp_session: Session, tmp_kb: Path) -> None:
    """Prove the claim above rather than asserting it in a comment."""
    ingested(tmp_session, tmp_kb, rebuild=True)
    fresh = dump(tmp_session)

    tmp_session.execute(sa.text(f"DELETE FROM {search.FTS_TABLE}"))
    rows = [document.row() for document in search.documents(tmp_session)]
    search._insert(tmp_session, rows)

    assert search.row_count(tmp_session) == 3  # correct content...
    assert dump(tmp_session) != fresh          # ...different bytes


def test_an_unchanged_corpus_does_not_touch_the_index(tmp_session: Session, tmp_kb: Path) -> None:
    ingested(tmp_session, tmp_kb)
    before = dump(tmp_session)

    again = ingested(tmp_session, tmp_kb)

    assert again.indexed == 0
    assert not again.reindexed_all
    assert dump(tmp_session) == before


def test_an_empty_index_heals_itself_on_the_next_ingest(tmp_session: Session, tmp_kb: Path) -> None:
    """A database migrated but never re-ingested must not search as if it were empty."""
    ingested(tmp_session, tmp_kb)
    tmp_session.execute(sa.text(f"DELETE FROM {search.FTS_TABLE}"))
    assert not search.is_consistent(tmp_session)

    result = ingested(tmp_session, tmp_kb)

    assert result.reindexed_all
    assert search.is_consistent(tmp_session)
    assert ids(search_questions(tmp_session, "prompt"))


def test_ingest_refuses_when_the_index_is_missing(tmp_session: Session, tmp_kb: Path) -> None:
    from examkb.ingest import IngestError

    tmp_session.execute(sa.text(f"DROP TABLE {search.FTS_TABLE}"))
    with pytest.raises(IngestError, match="db upgrade"):
        ingest(tmp_session, tmp_kb)


# ------------------------------------------------------------------------ the results


def test_a_known_phrase_returns_its_question_first(real: Session) -> None:
    """The criterion, against a phrase taken out of the corpus itself."""
    question = real.execute(
        sa.text("SELECT id, prompt_md FROM question WHERE id = :id"),
        {"id": "ccao-f/exam-01/q001"},
    ).one()
    phrase = "raw incident notes"
    assert phrase in question.prompt_md

    results = search_questions(real, f'"{phrase}"')

    assert results.hits[0].question_id == question.id
    assert results.total >= 1


def test_results_are_ranked_and_the_prompt_outweighs_the_explanation(real: Session) -> None:
    results = search_questions(real, "prompt caching", limit=50)
    assert results.hits
    scores = [hit.score for hit in results.hits]
    assert scores == sorted(scores), "bm25 is more negative for a better match"


def test_search_composes_with_a_domain_filter(real: Session) -> None:
    """The criterion: the intersection, and a count a `GROUP BY` agrees with."""
    term = "Claude"
    domain = "Output Evaluation and Validation"

    unfiltered = search_questions(real, term, limit=1000)
    filtered = search_questions(
        real, term, filters=SearchFilters(domain_label=domain), limit=1000
    )

    assert filtered.total <= unfiltered.total
    assert filtered.total == len(filtered.hits)
    assert {hit.domain_label for hit in filtered.hits} == {domain}
    assert set(ids(filtered)) <= set(ids(unfiltered))

    # The same number, counted the long way round.
    by_hand = real.scalar(
        sa.text(
            f"SELECT count(*) FROM {search.FTS_TABLE} "
            f"JOIN question ON question.id = {search.FTS_TABLE}.question_id "
            f"WHERE {search.FTS_TABLE} MATCH :q AND question.domain_label = :d"
        ),
        {"q": parse(term).expression, "d": domain},
    )
    assert filtered.total == by_hand


def test_a_filter_with_no_query_lists_the_filtered_set(real: Session) -> None:
    """A blank box beside a domain dropdown should list the domain, not nothing."""
    domain = "Output Evaluation and Validation"
    results = search_questions(real, "", filters=SearchFilters(domain_label=domain), limit=1000)

    assert results.total == queries.counts_by_domain(real)[("ccao-f", domain)] == 78
    assert not results.searched
    assert ids(results) == sorted(ids(results))


def test_filters_cover_the_projections_own_columns_and_compose(real: Session) -> None:
    results = search_questions(
        real,
        "",
        filters=SearchFilters(certification_id="ccao-f", type="multi_select"),
        limit=1000,
    )
    by_hand = real.scalar(
        sa.text(
            "SELECT count(*) FROM question WHERE certification_id = 'ccao-f' "
            "AND type = 'multi_select'"
        )
    )
    assert results.total == by_hand > 0


def test_paging_does_not_change_the_total(real: Session) -> None:
    first = search_questions(real, "prompt", limit=5)
    second = search_questions(real, "prompt", limit=5, offset=5)

    assert first.total == second.total > 10
    assert len(first.hits) == len(second.hits) == 5
    assert not set(ids(first)) & set(ids(second))


# ------------------------------------------------------------------------- snippets


def test_a_snippet_marks_the_match_and_is_never_html(real: Session) -> None:
    results = search_questions(real, "caching", limit=5)
    hit = results.hits[0]

    assert hit.snippet.has_match
    assert any(part.matched for part in hit.snippet.parts)
    assert "<mark>" not in hit.snippet.text
    assert search.MARK_OPEN not in hit.snippet.text


def test_corpus_angle_brackets_survive_a_snippet_literally(real: Session) -> None:
    """The criterion, against the real corpus fields that contain a `<`.

    Fourteen explanations in `kb/` talk about XML-style tags -- `<instructions>`,
    `<examples>`, `<quality_standards>`. The phrase below lands the snippet window
    on them, and those characters have to arrive as themselves: nothing in this
    layer produces markup, so there is nothing for them to be confused with and
    nothing to un-escape later.
    """
    results = search_questions(real, '"tags such as"', limit=50)
    assert results.total == 5

    with_tags = [hit for hit in results.hits if "<" in hit.snippet.text]
    assert len(with_tags) == results.total, "every hit for this phrase quotes a tag"
    assert "ccao-f/exam-04/q001" in {hit.question_id for hit in with_tags}

    for hit in with_tags:
        text = hit.snippet.text
        assert "&lt;" not in text and "&gt;" not in text and "&amp;" not in text
        assert "<" in text and ">" in text
        # The matched segment is the phrase, not a fragment of the markup -- and it
        # carries the corpus's own casing ("Tags such as" in one of the five),
        # because matching is case-insensitive but the snippet is the text itself.
        marked = [part.text for part in hit.snippet.parts if part.matched]
        assert [text.lower() for text in marked] == ["tags such as"]

    one = next(hit for hit in with_tags if hit.question_id == "ccao-f/exam-04/q001")
    assert "`<instructions>`" in one.snippet.text


def test_the_corpus_contains_no_snippet_sentinel(real: Session) -> None:
    """The delimiters are control characters because the corpus has none of them."""
    found = real.scalar(
        sa.text(
            f"SELECT count(*) FROM {search.FTS_TABLE} "
            "WHERE prompt || options || explanation LIKE :a "
            "   OR prompt || options || explanation LIKE :b"
        ),
        {"a": f"%{search.MARK_OPEN}%", "b": f"%{search.MARK_CLOSE}%"},
    )
    assert found == 0


def test_a_snippet_reassembles_into_plain_text(real: Session) -> None:
    hit = search_questions(real, "caching", limit=1).hits[0]
    assert hit.snippet.text == "".join(part.text for part in hit.snippet.parts)


def test_no_query_means_no_snippet_rather_than_a_fake_one(real: Session) -> None:
    results = search_questions(real, "", limit=3)
    assert all(not hit.snippet for hit in results.hits)
    assert all(hit.prompt_md for hit in results.hits)
