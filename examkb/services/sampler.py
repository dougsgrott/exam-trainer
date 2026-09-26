"""Choosing the questions for an exam.

Three properties, and the reason each one is not negotiable:

1. **Seeded.** The seed is returned with the draw and stored on `attempt.seed`
   (013), so an exam sat months ago can be reconstructed from its row rather than
   from a list of ids nobody kept. A draw nobody can reproduce is a draw nobody can
   debug when its domain mix looks wrong.
2. **Uniform within the filters.** `random.sample` over the whole eligible pool,
   not "take the first N after an ORDER BY" -- the second one looks random and
   quietly favours whatever the index happened to order by, which is exactly the
   silent skew that makes progress numbers meaningless. 019 replaces *uniform* with
   *blueprint-weighted*; it does not replace *unbiased*.
3. **The pool is the browse pool.** `search.question_scope()` builds the `FROM ...
   WHERE ...` for both, so a filter that shows 78 questions on `/browse` draws from
   78 here. Two definitions of "matching these filters" is a bug waiting for a
   person to notice their 60-item exam is 58.

Recency is a *preference*, not a filter. Questions not seen inside the window are
drawn first; if there are not enough, the rest come from the ones seen longest ago.
Nothing is ever excluded for being recent, because an exam that returns 43 items
because you studied yesterday is worse than one that repeats a question.

Nothing here writes. The sampler answers "which questions, in what order, with what
seed" and 013 is what turns that into an attempt.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from examkb.models.base import UTCDateTime, utcnow
from examkb.services.search import SearchFilters, question_scope

# SQLite stores an INTEGER in 8 bytes, but `attempt.seed` is read by people in bug
# reports; 2**31 keeps it short enough to type and wide enough that two draws on
# the same day do not collide.
SEED_BOUND = 2**31

# "Recently" is a parameter, and this is only its default. The window is long
# enough that a fortnight of daily practice does not start repeating, and short
# enough that a corpus of 549 is not exhausted.
DEFAULT_RECENT_WINDOW = timedelta(days=30)

# 044 owns format profiles as data. Until then the two real certifications' lengths
# live here, in one place, rather than as a literal in the runner.
DEFAULT_LENGTHS: dict[str, int] = {"ccao-f": 60, "ccar-p": 63}
DEFAULT_LENGTH = 60
DEFAULT_TIME_LIMIT_SECONDS = 120 * 60

# A question nobody should be asked. `disputed` is deliberately absent: an open
# dispute means the key is in question, not that the question is unusable, and
# dropping it silently would hide the disagreement instead of surfacing it.
EXCLUDED_VERIFICATIONS: frozenset[str] = frozenset({"known_bad"})
FLAGGED_VERIFICATIONS: frozenset[str] = frozenset({"disputed"})


class SamplerError(ValueError):
    """Bad input. An empty pool is a result, not an error."""


@dataclass(frozen=True)
class Candidate:
    """One question in the pool, with what the sampler needs to decide about it."""

    question_id: str
    certification_id: str
    exam_id: str | None
    domain_label: str | None
    type: str
    verification: str
    last_seen_at: datetime | None = None

    @property
    def flagged(self) -> bool:
        return self.verification in FLAGGED_VERIFICATIONS


@dataclass(frozen=True)
class Draw:
    """The questions, the seed that chose them, and everything 013 has to record."""

    question_ids: tuple[str, ...]
    seed: int
    requested: int
    pool_size: int
    eligible: int
    """After exclusions, `known_bad` and the filters. What could have been drawn."""

    excluded_known_bad: tuple[str, ...] = ()
    flagged: tuple[str, ...] = ()
    """Drawn anyway, and annotated. Today that means an open dispute."""

    fresh_used: int = 0
    stale_used: int = 0
    filters: SearchFilters = field(default_factory=SearchFilters)
    recent_window_days: int | None = None

    @property
    def count(self) -> int:
        return len(self.question_ids)

    @property
    def shortfall(self) -> int:
        """How many fewer than asked for. Never padded, never silently swallowed."""
        return max(0, self.requested - self.count)

    @property
    def short(self) -> bool:
        return self.shortfall > 0

    def sampler_json(self) -> dict:
        """What goes in `attempt.sampler_json` -- enough to replay this draw."""
        return {
            "seed": self.seed,
            "requested": self.requested,
            "drawn": self.count,
            "pool_size": self.pool_size,
            "eligible": self.eligible,
            "shortfall": self.shortfall,
            "recent_window_days": self.recent_window_days,
            "fresh_used": self.fresh_used,
            "stale_used": self.stale_used,
            "excluded_known_bad": list(self.excluded_known_bad),
            "flagged": list(self.flagged),
            "filters": {
                name: value
                for name, value in vars(self.filters).items()
                if value is not None
            },
        }

    def summary(self) -> str:
        head = f"sampled {self.count} of {self.requested} from {self.eligible} eligible"
        parts = [f"{head} (seed {self.seed})"]
        if self.short:
            parts.append(f"  short by {self.shortfall}: the pool does not hold that many")
        if self.flagged:
            parts.append(f"  {len(self.flagged)} with an open dispute, included and flagged")
        if self.excluded_known_bad:
            parts.append(f"  {len(self.excluded_known_bad)} excluded as known_bad")
        return "\n".join(parts)


# ------------------------------------------------------------------------------ the pool

_POOL_COLUMNS = """
       question.id            AS question_id,
       question.certification_id AS certification_id,
       question.exam_id       AS exam_id,
       question.domain_label  AS domain_label,
       question.type          AS type,
       coalesce(question_verification.verification, 'unverified') AS verification,
       (SELECT max(coalesce(item.first_shown_at, item.answered_at))
          FROM attempt_item item
         WHERE item.question_id = question.id
           AND item.first_shown_at IS NOT NULL) AS last_seen_at
"""


def pool(
    session: Session,
    *,
    filters: SearchFilters | None = None,
    q: str = "",
) -> list[Candidate]:
    """Every question the filters admit, with its verification and last-seen time.

    One statement. `question_verification` is 005's view; the correlated subquery
    is the recency lookup -- a question counts as "seen" when an `attempt_item`
    recorded showing it, including in an attempt that was later abandoned.
    Abandoning an exam does not un-see the questions, and 013's rule that an
    abandoned attempt contributes to no *statistic* is about scores, not about what
    the person has already read.

    `.columns()` is not decoration: a bare `text()` hands back whatever the driver
    produced, and SQLite produces a **string** for a timestamp. `last_seen_at` is
    compared against a cutoff two functions down, so untyped it would be a
    `TypeError` the first time anybody had actually sat an exam -- and never
    before.
    """
    scope, values = question_scope(q, filters)
    statement = sa.text(
        f"SELECT {_POOL_COLUMNS} {_with_verification(scope)} ORDER BY question.id"
    ).columns(
        question_id=sa.Text,
        certification_id=sa.Text,
        exam_id=sa.Text,
        domain_label=sa.Text,
        type=sa.Text,
        verification=sa.Text,
        last_seen_at=UTCDateTime(),
    )
    rows = session.execute(statement, values).mappings().all()

    return [
        Candidate(
            question_id=row["question_id"],
            certification_id=row["certification_id"],
            exam_id=row["exam_id"],
            domain_label=row["domain_label"],
            type=row["type"],
            verification=row["verification"],
            last_seen_at=row["last_seen_at"],
        )
        for row in rows
    ]


def _with_verification(scope: str) -> str:
    """Splice the verification view into the shared scope's FROM clause.

    The join has to land before the WHERE, which is why this is string surgery
    rather than concatenation. `question_scope()` owns the filters; this owns one
    more join, and neither has to know the other's shape beyond this line.
    """
    join = (
        " LEFT JOIN question_verification"
        " ON question_verification.question_id = question.id"
    )
    if " WHERE " in scope:
        head, where = scope.split(" WHERE ", 1)
        return f"{head}{join} WHERE {where}"
    return scope + join


# --------------------------------------------------------------------------- the draw


def sample(
    session: Session,
    *,
    count: int | None = None,
    certification_id: str | None = None,
    filters: SearchFilters | None = None,
    q: str = "",
    exclude: Iterable[str] = (),
    seed: int | None = None,
    recent_within: timedelta | None = DEFAULT_RECENT_WINDOW,
    now: datetime | None = None,
) -> Draw:
    """Fetch the pool, then draw from it. The call 013 makes.

    `certification_id` is a convenience: it is the filter every caller sets, and
    naming it here keeps the common call to one line. An explicit
    `filters.certification_id` wins.
    """
    filters = filters or SearchFilters()
    if certification_id and filters.certification_id is None:
        filters = replace(filters, certification_id=certification_id)

    if count is None:
        count = DEFAULT_LENGTHS.get(filters.certification_id or "", DEFAULT_LENGTH)

    return draw_from(
        pool(session, filters=filters, q=q),
        count=count,
        exclude=exclude,
        seed=seed,
        recent_within=recent_within,
        now=now,
        filters=filters,
    )


def draw_from(
    candidates: Sequence[Candidate],
    *,
    count: int,
    exclude: Iterable[str] = (),
    seed: int | None = None,
    recent_within: timedelta | None = DEFAULT_RECENT_WINDOW,
    now: datetime | None = None,
    filters: SearchFilters | None = None,
) -> Draw:
    """The draw itself, over a pool somebody already has. No database.

    Split out from `sample()` for two reasons that turned out to be the same one:
    019 apportions across blueprint nodes and then draws *within* each, over slices
    of a pool it fetched once; and the distribution criterion needs a thousand draws,
    which is a thousand queries if the only entry point also does the fetching.
    """
    if count < 0:
        raise SamplerError(f"cannot draw {count} questions")

    seed = seed if seed is not None else random.randrange(SEED_BOUND)
    rng = random.Random(seed)
    excluded_ids = set(exclude)

    known_bad = tuple(
        candidate.question_id
        for candidate in candidates
        if candidate.verification in EXCLUDED_VERIFICATIONS
    )
    eligible = [
        candidate
        for candidate in candidates
        if candidate.question_id not in excluded_ids
        and candidate.verification not in EXCLUDED_VERIFICATIONS
    ]

    chosen, fresh_used, stale_used = _choose(eligible, count, rng, recent_within, now)
    # The order questions are asked in is part of the draw, and it is shuffled
    # rather than left as whatever order the two tiers produced -- otherwise every
    # exam would open with the questions you have never seen.
    rng.shuffle(chosen)

    return Draw(
        question_ids=tuple(candidate.question_id for candidate in chosen),
        seed=seed,
        requested=count,
        pool_size=len(candidates),
        eligible=len(eligible),
        excluded_known_bad=known_bad,
        flagged=tuple(candidate.question_id for candidate in chosen if candidate.flagged),
        fresh_used=fresh_used,
        stale_used=stale_used,
        filters=filters or SearchFilters(),
        recent_window_days=None if recent_within is None else recent_within.days,
    )


def _choose(
    eligible: Sequence[Candidate],
    count: int,
    rng: random.Random,
    recent_within: timedelta | None,
    now: datetime | None,
) -> tuple[list[Candidate], int, int]:
    """Fresh questions first, then the ones seen longest ago. Never fewer than it can."""
    if count == 0 or not eligible:
        return [], 0, 0

    fresh, stale = _split_by_recency(eligible, recent_within, now)

    if len(fresh) >= count:
        # The common case, and the one the distribution test exercises: a plain
        # uniform sample over everything eligible.
        return rng.sample(list(fresh), count), count, 0

    chosen = list(fresh)
    rng.shuffle(chosen)
    remaining = count - len(chosen)

    # Oldest first, with ties broken by the seed so two questions last seen in the
    # same attempt do not always come back in id order.
    stale = sorted(stale, key=lambda candidate: (candidate.last_seen_at, rng.random()))
    topped_up = stale[:remaining]
    return [*chosen, *topped_up], len(chosen), len(topped_up)


def _split_by_recency(
    eligible: Sequence[Candidate], recent_within: timedelta | None, now: datetime | None
) -> tuple[list[Candidate], list[Candidate]]:
    if recent_within is None:
        return list(eligible), []

    cutoff = (now or utcnow()) - recent_within

    fresh: list[Candidate] = []
    stale: list[Candidate] = []
    for candidate in eligible:
        seen = candidate.last_seen_at
        (fresh if seen is None or seen < cutoff else stale).append(candidate)
    return fresh, stale
