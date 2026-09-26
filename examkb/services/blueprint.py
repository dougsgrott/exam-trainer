"""The blueprint ↔ corpus join, and the cross-check it makes possible.

017 shipped the format and the parser; this is what reads the result back. Two
jobs, and the split between them is the point:

**The join** is `=`. A corpus domain string matches a top-level blueprint node
when the two labels are byte-identical, and never otherwise. 005 constrained
`blueprint_domain_map.join_method` to `exact` so the database itself refuses a
fallback, and ingest builds the rows (`ingest.domain_map_rows`) so the join is
recorded once rather than recomputed per request. This module only reads it.

The temptation to fuzzy-match is real, because the vendor's own punctuation is
inconsistent across its two certifications -- `Governance, Risk, and Responsible
Use` against `Governance, Safety & Risk Management`, `and` against `&`, a comma
in `Evaluation, Testing & Optimization` and none in `Solution Design &
Architecture`. Every normalisation that tidies those also, sooner or later,
merges two domains that genuinely differ, and it does it silently. So an unjoined
domain is an error somebody has to look at, not a near-miss to be resolved.

**The cross-check** is reported, never enforced. The published weights are the
vendor's; the corpus's own distribution is evidence about them, and the two
agreeing to within a percentage point is worth showing. But a corpus that drifts
-- because 032 generated questions, or because a shard was added -- must flag, not
fail. Nothing downstream may refuse to work because the numbers moved, which is
why `Deviation.within_tolerance` is a field on a report and not a raise.

For the Anthropic blueprints specifically the direction of evidence is unusual and
worth stating: their guides are not public (they are issued through the Claude
Partner Network portal), so the transcription carries weights and no source
document, and this cross-check is the only independent corroboration there is.
It is corroboration, not provenance. See `data/blueprints/anthropic/*.blueprint`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.orm import Session

from examkb.models import corpus

# The plan's figure, and the one 018 asserts against. A percentage point is far
# tighter than sampling noise on 189 questions would give you by luck, so the two
# agreeing this closely is evidence the transcription is right -- and a number
# here rather than in a test because 022 wants to show it on a page.
TOLERANCE_PP = 0.7


class BlueprintError(RuntimeError):
    """Asked about a blueprint that is not there."""


@dataclass(frozen=True)
class Deviation:
    """One domain: what the vendor published against what the corpus holds."""

    domain_id: str
    label: str
    published_pct: float | None
    question_count: int
    corpus_pct: float

    @property
    def deviation_pp(self) -> float | None:
        """Percentage *points*, not percent. None when nothing was published."""
        if self.published_pct is None:
            return None
        return round(abs(self.corpus_pct - self.published_pct), 4)

    @property
    def within_tolerance(self) -> bool:
        deviation = self.deviation_pp
        return deviation is None or deviation <= TOLERANCE_PP + 1e-9


@dataclass(frozen=True)
class CrossCheck:
    """One certification's blueprint, joined to its corpus, with the arithmetic."""

    certification_id: str
    blueprint_id: str
    version_label: str | None
    regime: str
    weights_sum: float | None
    weights_sum_to_100: bool | None
    max_depth: int
    has_source_document: bool
    rows: tuple[Deviation, ...]
    unjoined_domains: tuple[str, ...]
    unjoined_nodes: tuple[str, ...]

    @property
    def question_count(self) -> int:
        return sum(row.question_count for row in self.rows)

    @property
    def worst_deviation_pp(self) -> float | None:
        deviations = [row.deviation_pp for row in self.rows if row.deviation_pp is not None]
        return max(deviations) if deviations else None

    @property
    def joined_cleanly(self) -> bool:
        """Every corpus domain found a node and every node found a domain.

        Both directions matter. A node with no domain is a domain the corpus has
        no questions for -- fine for a blueprint-only certification (phase 8),
        and a transcription typo for this one.
        """
        return not self.unjoined_domains and not self.unjoined_nodes

    @property
    def within_tolerance(self) -> bool:
        return all(row.within_tolerance for row in self.rows)

    def summary(self) -> str:
        worst = self.worst_deviation_pp
        gap = "no published weights" if worst is None else f"worst {worst:.2f}pp"
        return (
            f"{self.certification_id}: {len(self.rows)} domain(s), "
            f"{self.question_count} question(s), {self.regime}, {gap}"
        )


def _blueprint(session: Session, certification_id: str) -> corpus.Blueprint:
    found = session.scalars(
        sa.select(corpus.Blueprint)
        .where(corpus.Blueprint.certification_id == certification_id)
        .where(corpus.Blueprint.is_current.is_(True))
        .order_by(corpus.Blueprint.id)
    ).first()
    if found is None:
        raise BlueprintError(
            f"no current blueprint for {certification_id!r}; "
            f"transcribe one into data/blueprints/<vendor>/ and run `make blueprints`"
        )
    return found


def cross_check(session: Session, certification_id: str) -> CrossCheck:
    """The published weights beside the corpus's own, joined exactly."""
    blueprint = _blueprint(session, certification_id)

    nodes = list(
        session.scalars(
            sa.select(corpus.BlueprintNode)
            .where(corpus.BlueprintNode.blueprint_id == blueprint.id)
            .where(corpus.BlueprintNode.depth == 1)
            .order_by(corpus.BlueprintNode.ordinal)
        )
    )
    domains = {
        domain.id: domain
        for domain in session.scalars(
            sa.select(corpus.Domain).where(
                corpus.Domain.certification_id == certification_id
            )
        )
    }
    domain_for_node = {
        row.node_id: row.domain_id
        for row in session.scalars(
            sa.select(corpus.BlueprintDomainMap).where(
                corpus.BlueprintDomainMap.node_id.in_([node.id for node in nodes])
            )
        )
    }

    # The denominator is the questions that *joined*, not every question the
    # certification has. Otherwise a domain the blueprint does not mention would
    # drag every other percentage down and the whole table would read as drift.
    total = sum(
        domains[domain_for_node[node.id]].question_count
        for node in nodes
        if node.id in domain_for_node
    )

    rows: list[Deviation] = []
    unjoined_nodes: list[str] = []
    for node in nodes:
        domain_id = domain_for_node.get(node.id)
        if domain_id is None:
            unjoined_nodes.append(node.label)
            continue
        domain = domains[domain_id]
        rows.append(
            Deviation(
                domain_id=domain.id,
                label=node.label,
                published_pct=node.weight_pct,
                question_count=domain.question_count,
                corpus_pct=round(100.0 * domain.question_count / total, 4) if total else 0.0,
            )
        )

    joined = set(domain_for_node.values())
    unjoined_domains = sorted(
        domain.label for domain in domains.values() if domain.id not in joined
    )

    return CrossCheck(
        certification_id=certification_id,
        blueprint_id=blueprint.id,
        version_label=blueprint.version_label,
        regime=blueprint.weight_regime,
        weights_sum=blueprint.weights_sum,
        weights_sum_to_100=blueprint.weights_sum_to_100,
        max_depth=blueprint.max_depth,
        has_source_document=bool(
            session.scalar(
                sa.select(sa.func.count())
                .select_from(corpus.BlueprintSource)
                .where(corpus.BlueprintSource.blueprint_id == blueprint.id)
            )
        ),
        rows=tuple(rows),
        unjoined_domains=tuple(unjoined_domains),
        unjoined_nodes=tuple(unjoined_nodes),
    )


def weights_for(session: Session, certification_id: str) -> dict[str, float]:
    """`{domain_id: weight_pct}` for an exact blueprint -- what 019 apportions on.

    Keyed by *domain id*, not by label, because the sampler filters on domains and
    a label would make every caller redo the join. Empty when the certification has
    no blueprint at all, so 019 can fall back to corpus proportions without having
    to catch anything; a range regime is 019's decision to make, not this
    function's, and `CrossCheck.regime` is how it tells.
    """
    try:
        blueprint = _blueprint(session, certification_id)
    except BlueprintError:
        return {}

    rows = session.execute(
        sa.select(corpus.BlueprintDomainMap.domain_id, corpus.BlueprintNode.weight_pct)
        .join(corpus.BlueprintNode, corpus.BlueprintNode.id == corpus.BlueprintDomainMap.node_id)
        .where(corpus.BlueprintNode.blueprint_id == blueprint.id)
        .where(corpus.BlueprintNode.depth == 1)
        .where(corpus.BlueprintNode.weight_pct.is_not(None))
    ).all()
    return {domain_id: weight for domain_id, weight in rows}


def certifications_with_blueprints(session: Session) -> list[str]:
    return list(
        session.scalars(
            sa.select(corpus.Blueprint.certification_id)
            .where(corpus.Blueprint.is_current.is_(True))
            .order_by(corpus.Blueprint.certification_id)
            .distinct()
        )
    )


# ------------------------------------------------------------------- weights for 019

# What the exam's domain mix was built from, recorded on `attempt.weight_source`
# so a sitting from six months ago can explain its own shape.
OFFICIAL = "official"
CORPUS = "corpus"
NONE = "none"


@dataclass(frozen=True)
class WeightPlan:
    """`{domain_id: weight}` and where it came from. 019 apportions on this.

    Three sources, in the order they are preferred:

    `official`   an exact blueprint. The vendor said 21/16/15/14/12/12/10.
    `corpus`     no usable published weights, so the corpus's own domain
                 proportions stand in. This is not a worse kind of guess than
                 uniform sampling -- it is the *same* expectation with the
                 variance removed. A uniform 60-item draw on this corpus comes
                 out 14/13/11/7/7/4/4 against a mean of 13/10/9/8/7/7/6 (012),
                 and that lumpiness is what apportionment exists to remove.
    `none`       nothing to apportion on: no blueprint and no domains either.
                 The draw is plain uniform and says so.

    A `range` regime lands on `corpus` rather than on its own midpoints. Halving
    AZ-104's 20-25 would invent a precision the vendor declined to publish, and
    the corpus is evidence rather than arithmetic on a guess. `weights_sum_to_100
    IS NULL` is how a range is told from an exact one.
    """

    source: str
    weights: dict[str, float]
    """Empty when `source` is `none`. Never normalised -- the apportionment
    divides by the sum, so 21/16/... and 0.21/0.16/... behave identically."""

    labels: dict[str, str] = field(default_factory=dict)
    """`{domain_id: label}`, for reporting. Display only."""

    @property
    def weighted(self) -> bool:
        return bool(self.weights)


def resolve_weights(session: Session, certification_id: str) -> WeightPlan:
    """The best weights available for this certification, and their provenance.

    Never raises. A certification with no blueprint is the ordinary case for
    every vendor but Anthropic, and 019 has to cope with it rather than refuse.
    """
    labels = {
        domain.id: domain.label
        for domain in session.scalars(
            sa.select(corpus.Domain).where(
                corpus.Domain.certification_id == certification_id
            )
        )
    }

    official = weights_for(session, certification_id)
    if official:
        return WeightPlan(source=OFFICIAL, weights=official, labels=labels)

    corpus_weights = {
        domain_id: float(count)
        for domain_id, count in session.execute(
            sa.select(corpus.Domain.id, corpus.Domain.question_count)
            .where(corpus.Domain.certification_id == certification_id)
            .where(corpus.Domain.question_count > 0)
        ).all()
    }
    if corpus_weights:
        return WeightPlan(source=CORPUS, weights=corpus_weights, labels=labels)

    return WeightPlan(source=NONE, weights={}, labels=labels)
