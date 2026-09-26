"""The sampler: an exam's question set, and the properties that make it trustworthy.

Two of the criteria here are about things nobody would ever notice going wrong.

**The distribution.** "Take 60 questions" has an obviously-correct implementation
and several plausible-looking wrong ones -- `ORDER BY random() LIMIT 60` per
domain, or a shuffle that only touches the head of the list. Each would produce an
exam that looks fine and a per-domain progress number that means nothing. So a
thousand seeded draws are run and the per-domain totals compared against the pool's
own proportions, and a deliberately biased draw is run beside them to prove the
tolerance is capable of failing.

**The pool.** `/browse` and the sampler must agree about which questions exist. A
draw that quietly excluded a domain the browse page shows would be discovered by
someone counting their own exam. Both go through `search.question_scope()`, and a
test asserts they return the same number for the same filters.

Everything else -- seeds, exclusions, shortfalls, `known_bad` -- is the sampler
being honest about what it did, which is the whole reason the draw is a dataclass
and not a list of ids.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from examkb import db as db_module
from examkb.ingest import ingest
from examkb.models import journal
from examkb.services.browse import browse_page
from examkb.services.sampler import (
    DEFAULT_LENGTHS,
    Candidate,
    Draw,
    SamplerError,
    draw_from,
    pool,
    sample,
)
from examkb.services.search import UNSPECIFIED, SearchFilters

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
CCAO_DOMAIN = "Output Evaluation and Validation"
DRAWS = 1000
TOLERANCE = 0.03
"""Worst observed deviation over these fixed seeds is 1.55%; a biased draw is 30%+."""


@pytest.fixture(autouse=True)
def clean_caches():
    db_module.engine_for.cache_clear()
    yield
    db_module.engine_for.cache_clear()


@pytest.fixture
def corpus(real_session: Session) -> Session:
    """The real 549, projected. `real_session` copies a pre-ingested template."""
    return real_session


@pytest.fixture
def candidates(corpus: Session) -> list[Candidate]:
    return pool(corpus, filters=SearchFilters(certification_id="ccao-f"))


def ids(draw: Draw) -> set[str]:
    return set(draw.question_ids)


def seen(session: Session, question_id: str, *, at: datetime, attempt_id: int = 1) -> None:
    """Record that a question was shown, the way 013 will."""
    if session.get(journal.Attempt, attempt_id) is None:
        session.add(
            journal.Attempt(
                id=attempt_id, certification_id="ccao-f", started_at=at, seed=1, item_count=0
            )
        )
        session.flush()
    position = session.scalar(
        sa.select(sa.func.count()).select_from(journal.AttemptItem).where(
            journal.AttemptItem.attempt_id == attempt_id
        )
    )
    session.add(
        journal.AttemptItem(
            attempt_id=attempt_id,
            position=position,
            question_id=question_id,
            snapshot_json={},
            option_order=["A", "B", "C", "D"],
            first_shown_at=at,
        )
    )
    session.flush()


# ------------------------------------------------------------------------ the seed


def test_the_same_seed_draws_the_same_questions(corpus: Session) -> None:
    first = sample(corpus, certification_id="ccao-f", count=60, seed=42)
    again = sample(corpus, certification_id="ccao-f", count=60, seed=42)

    assert first.question_ids == again.question_ids
    assert len(first.question_ids) == 60


def test_a_different_seed_draws_a_different_exam(corpus: Session) -> None:
    first = sample(corpus, certification_id="ccao-f", count=60, seed=42)
    other = sample(corpus, certification_id="ccao-f", count=60, seed=43)

    assert first.question_ids != other.question_ids
    # Overlap is expected -- 60 of 360 twice -- but not near-identity.
    assert len(ids(first) & ids(other)) < 30


def test_a_seed_is_generated_and_reported_when_none_is_given(corpus: Session) -> None:
    drawn = sample(corpus, certification_id="ccao-f", count=5)

    assert 0 <= drawn.seed < 2**31
    assert sample(corpus, certification_id="ccao-f", count=5, seed=drawn.seed).question_ids == (
        drawn.question_ids
    )


def test_a_draw_replays_from_what_gets_stored(corpus: Session) -> None:
    """The criterion: seed plus recorded filters reproduce the same ids.

    This is what 013 writes onto `attempt.seed` and `attempt.sampler_json`, so the
    replay path is exercised through that record rather than through the call.
    """
    original = sample(
        corpus,
        filters=SearchFilters(certification_id="ccao-f", domain_label=CCAO_DOMAIN),
        count=20,
        seed=7,
    )
    recorded = original.sampler_json()

    replayed = sample(
        corpus,
        filters=SearchFilters(**recorded["filters"]),
        count=recorded["requested"],
        seed=recorded["seed"],
    )

    assert replayed.question_ids == original.question_ids
    assert recorded["filters"] == {
        "certification_id": "ccao-f",
        "domain_label": CCAO_DOMAIN,
    }


# ------------------------------------------------------------------- honest shortfall


def test_asking_for_more_than_exists_returns_what_exists_and_says_so(corpus: Session) -> None:
    """The criterion: 60 from a pool of 40 returns 40 and reports the shortfall."""
    drawn = sample(
        corpus,
        filters=SearchFilters(certification_id="ccar-p", domain_label=
                              "Developer Productivity & Operational Enablement"),
        count=60,
        seed=1,
    )

    assert drawn.eligible == 12
    assert drawn.count == 12
    assert drawn.requested == 60
    assert drawn.shortfall == 48 and drawn.short
    assert "short by 48" in drawn.summary()


def test_a_full_draw_reports_no_shortfall(corpus: Session) -> None:
    drawn = sample(corpus, certification_id="ccao-f", count=60, seed=1)

    assert not drawn.short and drawn.shortfall == 0
    assert "short by" not in drawn.summary()


def test_an_empty_pool_is_a_result_not_an_error(corpus: Session) -> None:
    drawn = sample(corpus, filters=SearchFilters(certification_id="no-such-cert"), count=60)

    assert drawn.question_ids == ()
    assert drawn.eligible == 0
    assert drawn.shortfall == 60


def test_zero_is_a_legal_draw_and_a_negative_one_is_not(candidates) -> None:
    assert draw_from(candidates, count=0, seed=1).question_ids == ()
    with pytest.raises(SamplerError, match="cannot draw"):
        draw_from(candidates, count=-1, seed=1)


def test_the_default_length_is_the_certification_s(corpus: Session) -> None:
    assert sample(corpus, certification_id="ccao-f", seed=1).count == DEFAULT_LENGTHS["ccao-f"] == 60
    assert sample(corpus, certification_id="ccar-p", seed=1).count == DEFAULT_LENGTHS["ccar-p"] == 63


# ---------------------------------------------------------------------- exclusions


def test_exclusions_are_honoured_exactly(corpus: Session) -> None:
    first = sample(corpus, certification_id="ccao-f", count=60, seed=5)
    excluded = ids(first)

    second = sample(corpus, certification_id="ccao-f", count=60, seed=5, exclude=excluded)

    assert not ids(second) & excluded
    assert second.count == 60
    assert second.eligible == 360 - 60


def test_no_question_appears_twice_in_one_draw(corpus: Session) -> None:
    for seed in range(20):
        drawn = sample(corpus, certification_id="ccao-f", count=60, seed=seed)
        assert len(drawn.question_ids) == len(set(drawn.question_ids)) == 60


def test_excluding_almost_everything_still_draws_what_is_left(corpus: Session) -> None:
    everything = {candidate.question_id for candidate in pool(corpus)}
    keep = sorted(everything)[:3]

    drawn = sample(corpus, count=60, seed=1, exclude=everything - set(keep))

    assert sorted(drawn.question_ids) == keep
    assert drawn.shortfall == 57


# -------------------------------------------------------------------- distribution


def test_a_thousand_draws_match_the_pool_proportions(candidates) -> None:
    """The criterion. Fixed seeds, so this is deterministic rather than flaky."""
    by_id = {candidate.question_id: candidate for candidate in candidates}
    pool_counts = Counter(candidate.domain_label for candidate in candidates)
    assert len(candidates) == 360

    drawn = Counter()
    for seed in range(DRAWS):
        for question_id in draw_from(candidates, count=60, seed=seed).question_ids:
            drawn[by_id[question_id].domain_label] += 1

    total = sum(drawn.values())
    assert total == DRAWS * 60

    for domain, in_pool in pool_counts.items():
        expected = total * in_pool / len(candidates)
        deviation = abs(drawn[domain] - expected) / expected
        assert deviation < TOLERANCE, f"{domain}: {deviation:.1%} from its pool share"


def test_the_distribution_check_can_actually_fail(candidates) -> None:
    """Prove the tolerance is capable of rejecting something.

    The first attempt at this control took the first 60 of the pool -- which is
    ordered by id, so it is exam-01 -- and scored a deviation of **exactly zero**.
    Not a bug in the check: every ccao-f exam is built to the same 13/10/9/8/7/7/6
    domain mix, so one whole exam *is* a perfectly representative sample. See
    `test_each_exam_is_already_built_to_the_same_domain_mix`.

    So the control is a draw that is biased on purpose: order the pool by domain
    and take the head, which is what an `ORDER BY domain` and a `LIMIT` produce.
    """
    pool_counts = Counter(candidate.domain_label for candidate in candidates)
    by_domain = sorted(candidates, key=lambda candidate: candidate.domain_label or "")

    drawn = Counter()
    for _ in range(DRAWS):
        for candidate in by_domain[:60]:
            drawn[candidate.domain_label] += 1

    total = sum(drawn.values())
    worst = max(
        abs(drawn[domain] - total * in_pool / len(candidates)) / (total * in_pool / len(candidates))
        for domain, in_pool in pool_counts.items()
    )
    assert worst > TOLERANCE * 5


def test_each_exam_is_already_built_to_the_same_domain_mix(corpus: Session) -> None:
    """A property of the corpus, found by a control that refused to fail.

    All six ccao-f practice exams carry an identical 13/10/9/8/7/7/6 spread across
    the seven domains, which says the vendor apportioned them to one blueprint.
    Worth knowing twice over: it is why "the first 60 by id" is accidentally
    unbiased here, and it is a corpus-derived weighting 019 can fall back on when
    no official blueprint exists.
    """
    rows = corpus.execute(
        sa.text(
            "SELECT exam_id, domain_label, count(*) FROM question "
            "WHERE certification_id = 'ccao-f' GROUP BY 1, 2"
        )
    ).all()

    by_exam: dict[str, list[int]] = {}
    for exam_id, _domain, count in rows:
        by_exam.setdefault(exam_id, []).append(count)

    mixes = {tuple(sorted(counts, reverse=True)) for counts in by_exam.values()}
    assert len(by_exam) == 6
    assert mixes == {(13, 10, 9, 8, 7, 7, 6)}


def test_every_question_in_the_pool_can_be_drawn(candidates) -> None:
    """No question is structurally unreachable -- a head-of-list bias would show here."""
    reachable: set[str] = set()
    for seed in range(200):
        reachable |= set(draw_from(candidates, count=60, seed=seed).question_ids)

    assert len(reachable) == len(candidates) == 360


# --------------------------------------------------------------------- verification


def test_a_known_bad_question_is_excluded(corpus: Session) -> None:
    """The criterion. `question_verification` (005) is where the judgement lives."""
    victim = "ccao-f/exam-01/q001"
    corpus.add(
        journal.VerificationEvent(
            question_id=victim, level="known_bad", source="human", created_at=NOW
        )
    )
    corpus.flush()

    drawn = sample(corpus, certification_id="ccao-f", count=360, seed=1)

    assert victim not in drawn.question_ids
    assert victim in drawn.excluded_known_bad
    assert drawn.eligible == 359
    assert "1 excluded as known_bad" in drawn.summary()


def test_a_disputed_question_stays_in_the_pool_and_is_flagged(corpus: Session) -> None:
    """The criterion, and the plan's rule: nothing vanishes silently."""
    disputed = "ccao-f/exam-01/q002"
    corpus.add(
        journal.Dispute(
            question_id=disputed, state="open", claim_md="B looks right too", created_at=NOW
        )
    )
    corpus.flush()

    drawn = sample(corpus, certification_id="ccao-f", count=360, seed=1)

    assert disputed in drawn.question_ids
    assert disputed in drawn.flagged
    assert drawn.eligible == 360
    assert "open dispute, included and flagged" in drawn.summary()


def test_a_resolved_dispute_stops_being_flagged(corpus: Session) -> None:
    question_id = "ccao-f/exam-01/q003"
    corpus.add(
        journal.Dispute(
            question_id=question_id,
            state="rejected",
            claim_md="turned out to be fine",
            created_at=NOW,
            resolved_at=NOW,
        )
    )
    corpus.flush()

    drawn = sample(corpus, certification_id="ccao-f", count=360, seed=1)

    assert question_id in drawn.question_ids
    assert question_id not in drawn.flagged


# ------------------------------------------------------------------------- recency


def test_questions_not_seen_recently_are_preferred(corpus: Session) -> None:
    everything = sorted(
        candidate.question_id
        for candidate in pool(corpus, filters=SearchFilters(exam_id="ccao-f/exam-01"))
    )
    stale, fresh = everything[:50], everything[50:]
    for question_id in stale:
        seen(corpus, question_id, at=NOW)

    drawn = sample(
        corpus,
        filters=SearchFilters(exam_id="ccao-f/exam-01"),
        count=10,
        seed=1,
        now=NOW + timedelta(days=1),
    )

    assert set(drawn.question_ids) <= set(fresh)
    assert drawn.fresh_used == 10 and drawn.stale_used == 0


def test_when_everything_is_recent_it_still_returns_n(corpus: Session) -> None:
    """The criterion: recency degrades, it does not starve the exam."""
    everything = [
        candidate.question_id
        for candidate in pool(corpus, filters=SearchFilters(exam_id="ccao-f/exam-01"))
    ]
    for question_id in everything:
        seen(corpus, question_id, at=NOW)

    drawn = sample(
        corpus,
        filters=SearchFilters(exam_id="ccao-f/exam-01"),
        count=20,
        seed=1,
        now=NOW + timedelta(days=1),
    )

    assert drawn.count == 20
    assert not drawn.short
    assert drawn.fresh_used == 0 and drawn.stale_used == 20


def test_the_oldest_are_taken_first_when_topping_up(corpus: Session) -> None:
    everything = sorted(
        candidate.question_id
        for candidate in pool(corpus, filters=SearchFilters(exam_id="ccao-f/exam-01"))
    )
    ancient, yesterday = everything[:5], everything[5:]
    for question_id in ancient:
        seen(corpus, question_id, at=NOW - timedelta(days=5), attempt_id=1)
    for question_id in yesterday:
        seen(corpus, question_id, at=NOW, attempt_id=2)

    drawn = sample(
        corpus,
        filters=SearchFilters(exam_id="ccao-f/exam-01"),
        count=5,
        seed=1,
        now=NOW + timedelta(days=1),
    )

    assert sorted(drawn.question_ids) == ancient


def test_the_recency_window_is_a_parameter(corpus: Session) -> None:
    """"Recently" is an argument, not a constant buried in a query."""
    everything = sorted(
        candidate.question_id
        for candidate in pool(corpus, filters=SearchFilters(exam_id="ccao-f/exam-01"))
    )
    for question_id in everything[:50]:
        seen(corpus, question_id, at=NOW)
    later = NOW + timedelta(days=10)

    wide = sample(
        corpus, filters=SearchFilters(exam_id="ccao-f/exam-01"), count=10, seed=1,
        recent_within=timedelta(days=30), now=later,
    )
    narrow = sample(
        corpus, filters=SearchFilters(exam_id="ccao-f/exam-01"), count=10, seed=1,
        recent_within=timedelta(days=1), now=later,
    )

    assert wide.stale_used == 0, "seen 10 days ago is recent in a 30-day window"
    assert set(narrow.question_ids) | set(wide.question_ids)
    assert narrow.recent_window_days == 1 and wide.recent_window_days == 30


def test_recency_can_be_switched_off(corpus: Session) -> None:
    everything = [
        candidate.question_id
        for candidate in pool(corpus, filters=SearchFilters(exam_id="ccao-f/exam-01"))
    ]
    for question_id in everything:
        seen(corpus, question_id, at=NOW)

    drawn = sample(
        corpus, filters=SearchFilters(exam_id="ccao-f/exam-01"), count=10, seed=1,
        recent_within=None, now=NOW + timedelta(hours=1),
    )

    assert drawn.count == 10
    assert drawn.fresh_used == 10, "with no window, nothing is stale"
    assert drawn.recent_window_days is None


def test_a_question_seen_in_an_abandoned_attempt_still_counts_as_seen(corpus: Session) -> None:
    """Abandoning an exam does not un-see the questions.

    013's rule is that an abandoned attempt contributes to no *statistic*; recency
    is a preference about what the person has already read, which is a different
    thing and is deliberately decided the other way.
    """
    everything = sorted(
        candidate.question_id
        for candidate in pool(corpus, filters=SearchFilters(exam_id="ccao-f/exam-01"))
    )
    for question_id in everything[:50]:
        seen(corpus, question_id, at=NOW)
    corpus.execute(sa.update(journal.Attempt).values(abandoned_at=NOW))
    corpus.flush()

    drawn = sample(
        corpus, filters=SearchFilters(exam_id="ccao-f/exam-01"), count=10, seed=1,
        now=NOW + timedelta(days=1),
    )

    assert not set(drawn.question_ids) & set(everything[:50])


def test_last_seen_comes_back_as_a_datetime(corpus: Session) -> None:
    """A raw `text()` returns SQLite's timestamp as a string unless it is typed."""
    seen(corpus, "ccao-f/exam-01/q001", at=NOW)

    found = next(
        candidate
        for candidate in pool(corpus)
        if candidate.question_id == "ccao-f/exam-01/q001"
    )

    assert isinstance(found.last_seen_at, datetime)
    assert found.last_seen_at == NOW


# --------------------------------------------------------------- one pool, two callers


@pytest.mark.parametrize(
    "filters",
    [
        SearchFilters(certification_id="ccao-f"),
        SearchFilters(domain_label=CCAO_DOMAIN),
        SearchFilters(certification_id="ccar-p", type="multi_select"),
        SearchFilters(exam_mode=UNSPECIFIED),
        SearchFilters(exam_id="ccao-f/exam-01"),
    ],
)
def test_the_pool_is_the_browse_pool(real_db: Engine, filters: SearchFilters) -> None:
    """The criterion: the same filters, the same count, because it is the same SQL."""
    url = str(real_db.url)
    params = {
        "cert": filters.certification_id,
        "domain": filters.domain_label,
        "type": filters.type,
        "mode": filters.exam_mode,
        "exam": filters.exam_id,
    }
    listed = browse_page(url=url, params=params, per_page=1).total

    with Session(real_db) as session:
        drawn = sample(session, filters=filters, count=1, seed=1)

    assert drawn.pool_size == listed > 0


def test_the_mark_filter_reaches_the_pool(corpus: Session) -> None:
    """"Draw from what I have not marked" is `SearchFilters(mark=...)`, not new code."""
    from examkb.services.marks import set_mark

    for question_id in [f"ccao-f/exam-01/q{n:03d}" for n in range(1, 11)]:
        set_mark(corpus, question_id, "known", at=NOW)
    corpus.flush()

    known = sample(corpus, filters=SearchFilters(mark="known"), count=60, seed=1)
    unmarked = sample(corpus, filters=SearchFilters(mark=UNSPECIFIED), count=600, seed=1)

    assert known.eligible == 10
    assert unmarked.eligible == 539


def test_a_search_string_narrows_the_pool_too(corpus: Session) -> None:
    drawn = sample(corpus, q='"prompt caching"', count=100, seed=1)

    assert 0 < drawn.pool_size < 549
    assert drawn.count == drawn.pool_size


# ------------------------------------------------------------------------- the record


def test_the_sampler_json_is_enough_to_explain_the_draw(corpus: Session) -> None:
    drawn = sample(corpus, certification_id="ccao-f", count=60, seed=99)
    recorded = drawn.sampler_json()

    assert recorded["seed"] == 99
    assert recorded["requested"] == 60 and recorded["drawn"] == 60
    assert recorded["eligible"] == 360 and recorded["pool_size"] == 360
    assert recorded["shortfall"] == 0
    assert recorded["recent_window_days"] == 30
    assert recorded["filters"] == {"certification_id": "ccao-f"}


def test_the_sampler_writes_nothing(corpus: Session) -> None:
    """It answers a question. 013 is what turns the answer into an attempt."""
    before = {
        table: corpus.scalar(sa.text(f"SELECT count(*) FROM {table}"))
        for table in ("attempt", "attempt_item", "mark", "ingest_run", "question")
    }

    sample(corpus, certification_id="ccao-f", count=60, seed=1)

    after = {
        table: corpus.scalar(sa.text(f"SELECT count(*) FROM {table}"))
        for table in before
    }
    assert after == before
