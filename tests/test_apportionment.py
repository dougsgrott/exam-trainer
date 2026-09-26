"""Blueprint-weighted sampling: largest remainder, and what a thin pool does to it.

The arithmetic here has one result worth leading with. Largest remainder on 018's
published weights gives **13/10/9/8/7/7/6** for a 60-item CCAO-F exam -- which is
the mix 012 found by counting every practice exam in the corpus and recorded as an
unexplained regularity of the vendor's data. It was this rule all along. The same
thing happens on CCAR-P: 63 items across 19/17/16/14/14/13/7 gives 12/11/10/9/9/8/4,
exactly a third of the corpus's own 36/33/30/27/27/24/12.

That coincidence is the strongest evidence in this repository that 018's weights
are right, and it is asserted here rather than admired.

One thing this file deliberately does *not* test is convergence. Apportionment is
not a stochastic method that approaches the blueprint over many exams -- when the
pool can supply the shares, **every single draw hits the mix exactly**, and the
1000-draw test asserts that rather than an average. What remains random is *which*
questions fill each slice, and that is measured separately.
"""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from examkb.services import sampler
from examkb.services.blueprint import CORPUS, NONE, OFFICIAL, WeightPlan, resolve_weights
from examkb.services.sampler import (
    Candidate,
    DomainShare,
    apportion,
    apportion_within,
    draw_from,
)

CCAO_F = {
    "d1": 21.0, "d2": 16.0, "d3": 15.0, "d4": 14.0, "d5": 12.0, "d6": 12.0, "d7": 10.0,
}
CCAR_P = {
    "d1": 19.0, "d2": 17.0, "d3": 16.0, "d4": 14.0, "d5": 14.0, "d6": 13.0, "d7": 7.0,
}
NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)


def candidates(spec: dict[str, int], *, seen: dict[str, datetime] | None = None):
    """`{domain_id: how many}` -> a pool, ids stable and distinct."""
    seen = seen or {}
    return [
        Candidate(
            question_id=f"{domain}/q{index:03d}",
            certification_id="cert",
            exam_id=None,
            domain_id=domain,
            domain_label=domain.upper(),
            type="single_select",
            verification="verified",
            last_seen_at=seen.get(domain),
        )
        for domain, count in spec.items()
        for index in range(count)
    ]


def plan(weights: dict[str, float], source: str = OFFICIAL) -> WeightPlan:
    return WeightPlan(
        source=source, weights=dict(weights), labels={k: k.upper() for k in weights}
    )


def mix(draw: sampler.Draw) -> dict[str, int]:
    return Counter(question_id.split("/")[0] for question_id in draw.question_ids)


# ------------------------------------------------------------------- the arithmetic


def test_60_items_across_the_ccao_f_weights() -> None:
    """The criterion, and the vendor's own mix."""
    seats = apportion(60, CCAO_F)

    assert sum(seats.values()) == 60
    assert [seats[key] for key in sorted(CCAO_F)] == [13, 10, 9, 8, 7, 7, 6]


def test_63_items_across_the_ccar_p_weights() -> None:
    seats = apportion(63, CCAR_P)

    assert sum(seats.values()) == 63
    assert [seats[key] for key in sorted(CCAR_P)] == [12, 11, 10, 9, 9, 8, 4]


def test_the_apportionment_is_the_vendors_own_exam_mix(real_session: Session) -> None:
    """012 counted 13/10/9/8/7/7/6 off every practice exam and could not explain it.

    It is largest remainder on the published weights. This asserts the two agree
    against the real projection rather than against the constants at the top of
    this file, so a re-transcription that moved a weight would show up here.
    """
    weights = resolve_weights(real_session, "ccao-f")
    seats = apportion(60, weights.weights)
    by_label = {weights.labels[key]: value for key, value in seats.items()}

    assert weights.source == OFFICIAL
    assert by_label == {
        "Output Evaluation and Validation": 13,
        "Workflow Integration and Solution Design": 10,
        "Governance, Risk, and Responsible Use": 9,
        "Prompting and Task Execution": 8,
        "Product and Model Selection": 7,
        "Configuration and Knowledge Management": 7,
        "Troubleshooting and Optimization": 6,
    }


def test_all_nine_vendor_exams_have_exactly_the_apportioned_mix(
    real_session: Session,
) -> None:
    """The finding, stated as strongly as the data allows.

    012 recorded that every practice exam in the corpus is built to one domain mix
    and could not account for it. Here it is accounted for: **all nine** -- six
    CCAO-F exams of 60 items and three CCAR-P exams of 63, two different weight
    vectors and two different lengths -- reproduce largest remainder applied to
    018's transcribed weights, exactly, with no residue. The vendor used this
    rule, and this sampler now uses the same one.

    It is also a far tighter check on 018's weights than the aggregate 0.67pp
    comparison, because it has to land on the right *integers* nine times. What it
    is not is a way to tell official weights from corpus-derived ones: on this
    corpus the two agree closely enough to produce identical integers, which is
    the same fact 018 measured from the other end.
    """
    import sqlalchemy as sa

    from examkb.models import corpus

    checked = 0
    for certification in ("ccao-f", "ccar-p"):
        weights = resolve_weights(real_session, certification)
        exams = real_session.scalars(
            sa.select(corpus.Question.exam_id)
            .where(corpus.Question.certification_id == certification)
            .distinct()
            .order_by(corpus.Question.exam_id)
        ).all()
        for exam_id in exams:
            actual = dict(
                real_session.execute(
                    sa.select(corpus.Question.domain_id, sa.func.count())
                    .where(corpus.Question.exam_id == exam_id)
                    .group_by(corpus.Question.domain_id)
                ).all()
            )
            assert actual == apportion(sum(actual.values()), weights.weights), exam_id
            checked += 1

    assert checked == 9


@pytest.mark.parametrize("count", list(range(0, 130)))
def test_every_apportionment_sums_to_exactly_what_was_asked(count: int) -> None:
    """The property behind both criteria, at every length rather than two."""
    assert sum(apportion(count, CCAO_F).values()) == count
    assert sum(apportion(count, CCAR_P).values()) == count


def test_a_share_is_never_negative_and_never_exceeds_the_count() -> None:
    seats = apportion(5, CCAO_F)

    assert all(0 <= seat <= 5 for seat in seats.values())


def test_weights_need_not_sum_to_100() -> None:
    """A range regime bounds to 80/105 and a corpus-derived plan is raw counts."""
    seats = apportion(10, {"a": 2.0, "b": 3.0})

    assert seats == {"a": 4, "b": 6}


def test_zero_and_negative_weights_get_nothing() -> None:
    seats = apportion(10, {"a": 1.0, "b": 0.0, "c": -5.0})

    assert seats == {"a": 10, "b": 0, "c": 0}


def test_no_weights_at_all_apportions_nothing() -> None:
    assert apportion(10, {}) == {}
    assert apportion(10, {"a": 0.0}) == {"a": 0}


# ------------------------------------------------------------------------- the ties


def test_a_tie_in_the_remainder_is_broken_by_the_seed() -> None:
    """The criterion: deterministic for a given seed, and not always the same key.

    A fixed tie-break would hand the last seat to the same domain in every exam
    ever drawn, which over a few hundred sittings is a real over-representation
    rather than a rounding detail.
    """
    import random

    tied = {"a": 50.0, "b": 50.0}
    winners = {
        seed: next(key for key, seat in apportion(1, tied, rng=random.Random(seed)).items() if seat)
        for seed in range(40)
    }

    assert set(winners.values()) == {"a", "b"}, "the tie-break never moves"
    for seed, winner in winners.items():
        again = apportion(1, tied, rng=random.Random(seed))
        assert next(key for key, seat in again.items() if seat) == winner


def test_without_an_rng_the_tie_break_is_the_key() -> None:
    """Pure and deterministic, for callers that want the arithmetic and not a draw."""
    assert apportion(1, {"b": 50.0, "a": 50.0}) == {"a": 1, "b": 0}


# ------------------------------------------------------------ a pool thinner than a share


def test_a_thin_pool_yields_what_it_has_and_the_rest_is_redistributed() -> None:
    """The criterion: apportioned 9, holds 2, yields 2, and the 7 go elsewhere."""
    weights = {"thin": 15.0, "a": 45.0, "b": 40.0}
    capacity = {"thin": 2, "a": 100, "b": 100}

    asked = apportion(60, weights)
    granted = apportion_within(60, weights, capacity)

    assert asked["thin"] == 9
    assert granted["thin"] == 2
    assert sum(granted.values()) == 60, "the 7 were redistributed, not dropped"
    assert granted["a"] > asked["a"] and granted["b"] > asked["b"]


def test_the_redistribution_uses_the_same_rule_not_whoever_is_biggest() -> None:
    """The 7 spare seats split 45/40, not 7 to the larger domain."""
    granted = apportion_within(
        60, {"thin": 15.0, "a": 45.0, "b": 40.0}, {"thin": 2, "a": 100, "b": 100}
    )

    assert granted == {"thin": 2, "a": 31, "b": 27}
    assert apportion(58, {"a": 45.0, "b": 40.0}) == {"a": 31, "b": 27}


def test_a_shortfall_is_reported_on_the_draw() -> None:
    """Never silently under-covered: the target is recorded beside what was drawn."""
    pool = candidates({"thin": 2, "a": 100, "b": 100})
    draw = draw_from(
        pool, count=60, seed=7, weights=plan({"thin": 15.0, "a": 45.0, "b": 40.0})
    )

    thin = next(share for share in draw.shares if share.domain_id == "thin")
    assert (thin.target, thin.drawn, thin.available, thin.short) == (9, 2, 2, 7)
    assert draw.under_covered == (thin,)
    assert draw.count == 60, "the exam is still full"
    assert not draw.short, "a redistributed shortfall is not a shortfall in the exam"
    assert "THIN: 2 of 9" in draw.summary()


def test_every_domain_thin_shortens_the_exam_and_says_so() -> None:
    pool = candidates({"a": 3, "b": 2})
    draw = draw_from(pool, count=60, seed=7, weights=plan({"a": 50.0, "b": 50.0}))

    assert draw.count == 5
    assert draw.shortfall == 55
    assert "short by 55" in draw.summary()


def test_a_domain_with_no_questions_at_all_loses_its_whole_share() -> None:
    pool = candidates({"a": 50, "b": 50})
    draw = draw_from(
        pool, count=20, seed=1, weights=plan({"a": 40.0, "b": 40.0, "empty": 20.0})
    )

    empty = next(share for share in draw.shares if share.domain_id == "empty")
    assert (empty.target, empty.drawn, empty.available) == (4, 0, 0)
    assert draw.count == 20


# -------------------------------------------------------------------- the draw itself


def test_the_realised_mix_is_the_apportioned_mix(real_session: Session) -> None:
    weights = resolve_weights(real_session, "ccao-f")
    draw = sampler.sample(real_session, certification_id="ccao-f", count=60, seed=2026)

    realised = Counter(
        weights.labels[share.domain_id] for share in draw.shares for _ in range(share.drawn)
    )
    assert sum(realised.values()) == 60
    assert realised["Output Evaluation and Validation"] == 13
    assert realised["Troubleshooting and Optimization"] == 6


def test_1000_weighted_draws_all_hit_the_blueprint_exactly(real_session: Session) -> None:
    """Not "converges on" -- *is*. Apportionment leaves the mix nothing to chance.

    The criterion asked for convergence within tolerance, which is what you need
    from a stochastic method. This is not one: when the pool can supply the
    shares, the deviation is zero on every single exam, so the test asserts zero
    a thousand times rather than a mean.
    """
    pool = sampler.pool(real_session, filters=sampler.SearchFilters(certification_id="ccao-f"))
    weights = resolve_weights(real_session, "ccao-f")
    target = apportion(60, weights.weights)

    for seed in range(1000):
        draw = draw_from(pool, count=60, seed=seed, weights=weights, recent_within=None)
        assert {share.domain_id: share.drawn for share in draw.shares} == target


def test_within_a_slice_the_questions_are_still_drawn_uniformly(
    real_session: Session,
) -> None:
    """Apportionment fixes the mix. It must not also fix *which* questions.

    The failure this catches is a slice that always returns the same 13 questions
    -- the mix would be perfect and the exam would be identical every time.
    """
    pool = sampler.pool(real_session, filters=sampler.SearchFilters(certification_id="ccao-f"))
    weights = resolve_weights(real_session, "ccao-f")
    domain = "ccao-f/troubleshooting-and-optimization"
    in_domain = [c.question_id for c in pool if c.domain_id == domain]

    wanted = set(in_domain)
    seen: Counter = Counter()
    draws = 400
    for seed in range(draws):
        draw = draw_from(pool, count=60, seed=seed, weights=weights, recent_within=None)
        seen.update(qid for qid in draw.question_ids if qid in wanted)

    assert len(seen) == len(in_domain), "some questions are unreachable"

    # Each question's count is Binomial(400, 6/36): mean 66.7, sd 7.45. Four
    # standard deviations is the bound, computed rather than eyeballed -- a
    # tolerance picked to fit one observed run is a tolerance that fails on the
    # next seed for no reason anybody can explain.
    share = 6 / len(in_domain)
    mean = draws * share
    deviation = (draws * share * (1 - share)) ** 0.5
    assert statistics.mean(seen.values()) == pytest.approx(mean, rel=0.01)
    worst = max(abs(count - mean) for count in seen.values())
    assert worst < 4 * deviation, f"{worst:.1f} is more than 4 sd ({deviation:.2f})"


def test_a_weighted_draw_replays_from_its_seed(real_session: Session) -> None:
    first = sampler.sample(real_session, certification_id="ccao-f", count=60, seed=99)
    again = sampler.sample(real_session, certification_id="ccao-f", count=60, seed=99)

    assert first.question_ids == again.question_ids


def test_the_exam_is_not_grouped_by_domain(real_session: Session) -> None:
    """A weighted draw that came back sorted would be a tell, and an easier exam."""
    pool = sampler.pool(real_session, filters=sampler.SearchFilters(certification_id="ccao-f"))
    weights = resolve_weights(real_session, "ccao-f")
    draw = draw_from(pool, count=60, seed=3, weights=weights, recent_within=None)

    by_position = [qid.rsplit("/", 1)[0] for qid in draw.question_ids]
    runs = sum(1 for a, b in zip(by_position, by_position[1:]) if a != b)
    assert runs > 40, "the questions came back in domain order"


def test_apportionment_never_repeats_a_question(real_session: Session) -> None:
    draw = sampler.sample(real_session, certification_id="ccao-f", count=60, seed=5)

    assert len(set(draw.question_ids)) == 60


def test_the_weighted_draw_is_tighter_than_the_uniform_one(real_session: Session) -> None:
    """The negative control: without this, apportionment could be doing nothing.

    012 recorded that a single uniform 60-item draw wanders a long way from the
    mix -- seed 2026 gave 14/13/11/7/7/4/4 against 13/10/9/8/7/7/6. That spread is
    what this issue removes, and the comparison is the proof it removed it.
    """
    pool = sampler.pool(real_session, filters=sampler.SearchFilters(certification_id="ccao-f"))
    weights = resolve_weights(real_session, "ccao-f")
    target = apportion(60, weights.weights)

    def worst(draw) -> int:
        realised = Counter(
            next(c.domain_id for c in pool if c.question_id == qid)
            for qid in draw.question_ids
        )
        return max(abs(realised[key] - value) for key, value in target.items())

    uniform = [
        worst(draw_from(pool, count=60, seed=seed, recent_within=None)) for seed in range(30)
    ]
    weighted = [
        worst(draw_from(pool, count=60, seed=seed, weights=weights, recent_within=None))
        for seed in range(30)
    ]

    assert max(weighted) == 0
    assert max(uniform) >= 3, "the uniform draw is not lumpy enough to be a control"


# ------------------------------------------------------------------------- recency


def test_recency_applies_within_a_slice_not_across_the_exam() -> None:
    """A domain you drilled yesterday keeps its share.

    If recency ran across the whole exam, a heavily-studied domain would lose its
    seats to an untouched one and the blueprint mix -- the point of the issue --
    would bend to whatever was studied last week.
    """
    recent = {"drilled": NOW - timedelta(days=1)}
    pool = candidates({"drilled": 40, "fresh": 40}, seen=recent)
    draw = draw_from(
        pool,
        count=20,
        seed=4,
        weights=plan({"drilled": 50.0, "fresh": 50.0}),
        recent_within=timedelta(days=30),
        now=NOW,
    )

    assert mix(draw) == {"drilled": 10, "fresh": 10}


def test_a_slice_that_is_entirely_stale_still_fills_its_share() -> None:
    everything = {"a": NOW - timedelta(days=1), "b": NOW - timedelta(days=1)}
    pool = candidates({"a": 30, "b": 30}, seen=everything)
    draw = draw_from(
        pool,
        count=20,
        seed=4,
        weights=plan({"a": 50.0, "b": 50.0}),
        recent_within=timedelta(days=30),
        now=NOW,
    )

    assert mix(draw) == {"a": 10, "b": 10}
    assert draw.stale_used == 20


# ------------------------------------------------------------------- weight sources


def test_an_exact_blueprint_is_official(real_session: Session) -> None:
    assert resolve_weights(real_session, "ccao-f").source == OFFICIAL
    assert resolve_weights(real_session, "ccar-p").source == OFFICIAL


def test_no_blueprint_falls_back_to_the_corpus(real_session: Session, monkeypatch) -> None:
    """The scope's rule: official when present, corpus-derived when not."""
    monkeypatch.setattr(
        "examkb.services.blueprint.weights_for", lambda session, certification: {}
    )
    resolved = resolve_weights(real_session, "ccao-f")

    assert resolved.source == CORPUS
    assert resolved.weights["ccao-f/output-evaluation-and-validation"] == 78.0
    # Corpus proportions apportion to the corpus's own mix, which on this corpus
    # is within one item of the blueprint's everywhere.
    seats = apportion(60, resolved.weights)
    assert sorted(seats.values(), reverse=True) == [13, 10, 9, 8, 7, 7, 6]


def test_a_certification_with_nothing_at_all_is_none(real_session: Session) -> None:
    """The dbt case: a blueprint with no weights, and no corpus to derive any."""
    resolved = resolve_weights(real_session, "dbt-analytics-engineering")

    assert resolved.source == NONE
    assert resolved.weights == {}
    assert not resolved.weighted


def test_an_unweighted_plan_draws_uniformly_and_records_none() -> None:
    """The criterion: samples uniformly, records `weights: none`."""
    pool = candidates({"a": 50, "b": 50})
    draw = draw_from(pool, count=20, seed=1, weights=WeightPlan(source=NONE, weights={}))

    assert draw.weight_source == NONE
    assert draw.shares == ()
    assert draw.apportionment_json() is None
    assert draw.count == 20


def test_asking_for_no_weighting_gets_none(real_session: Session) -> None:
    draw = sampler.sample(
        real_session, certification_id="ccao-f", count=60, seed=1, weighted=False
    )

    assert draw.weight_source == NONE
    assert not draw.weighted


def test_a_domain_the_weights_do_not_mention_is_counted_not_silently_dropped() -> None:
    pool = candidates({"named": 50, "unnamed": 30})
    draw = draw_from(pool, count=20, seed=1, weights=plan({"named": 100.0}))

    assert draw.unweighted_skipped == 30
    assert set(mix(draw)) == {"named"}
    assert draw.apportionment_json()["unweighted_skipped"] == 30


# --------------------------------------------------------------- what reaches the row


def test_the_attempt_records_the_weight_source_and_the_apportionment(
    real_session: Session,
) -> None:
    """The scope's rule: a past exam's mix is explainable from its own row."""
    from examkb.services import attempts

    attempt = attempts.start(real_session, certification_id="ccao-f", count=60, seed=11)
    real_session.flush()

    assert attempt.weight_source == OFFICIAL
    record = attempt.apportionment_json
    assert record["weight_source"] == OFFICIAL
    assert len(record["domains"]) == 7
    assert sum(row["drawn"] for row in record["domains"]) == 60
    assert sum(row["target"] for row in record["domains"]) == 60
    assert sum(row["weight"] for row in record["domains"]) == 100.0


def test_the_recorded_apportionment_names_the_domains(real_session: Session) -> None:
    from examkb.services import attempts

    attempt = attempts.start(real_session, certification_id="ccao-f", count=60, seed=12)
    real_session.flush()

    rows = {row["label"]: row for row in attempt.apportionment_json["domains"]}
    assert rows["Output Evaluation and Validation"]["target"] == 13
    assert rows["Output Evaluation and Validation"]["weight"] == 21.0


def test_the_sampler_json_carries_the_source_too(real_session: Session) -> None:
    draw = sampler.sample(real_session, certification_id="ccao-f", count=60, seed=13)

    assert draw.sampler_json()["weight_source"] == OFFICIAL
