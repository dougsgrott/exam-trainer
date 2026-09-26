"""Read helpers over the projection.

The plan keeps exactly one architectural rule from the design it cut down: **no
SQLAlchemy in routes or templates**. This module and the services are where the
queries live, so a page is a function call and a test of a number is a test of
this module rather than of a rendered page.

It starts small on purpose -- 006 needs the counts that prove the projection
matches `kb/manifest.json`, and 009/010 extend it with search and filters rather
than inheriting a speculative API nobody wrote a caller for.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from examkb.models import corpus


@dataclass(frozen=True)
class CorpusCounts:
    certifications: int
    exams: int
    domains: int
    questions: int
    options: int
    references: int
    citations: int

    def __str__(self) -> str:
        return (
            f"{self.questions} questions, {self.options} options, "
            f"{self.references} references across {self.certifications} certifications"
        )


def corpus_counts(session: Session) -> CorpusCounts:
    def count(model) -> int:
        return session.scalar(sa.select(sa.func.count()).select_from(model)) or 0

    return CorpusCounts(
        certifications=count(corpus.Certification),
        exams=count(corpus.Exam),
        domains=count(corpus.Domain),
        questions=count(corpus.Question),
        options=count(corpus.QuestionOption),
        references=count(corpus.Reference),
        citations=count(corpus.QuestionReference),
    )


def question_count(session: Session) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(corpus.Question)) or 0


def counts_by_domain(session: Session) -> dict[tuple[str, str], int]:
    """`(certification, vendor's domain string) -> questions`.

    Keyed on the **label**, not the slug: this is the number that gets compared
    against `kb/manifest.json` and shown to a person, and both speak the vendor's
    string. A question whose domain did not resolve to a row still appears, under
    its own label, rather than vanishing from a total that is supposed to add up.
    """
    rows = session.execute(
        sa.select(
            corpus.Question.certification_id,
            corpus.Question.domain_label,
            sa.func.count(),
        ).group_by(corpus.Question.certification_id, corpus.Question.domain_label)
    ).all()
    return {(certification, domain): count for certification, domain, count in rows}


def counts_by_certification(session: Session) -> dict[str, int]:
    rows = session.execute(
        sa.select(corpus.Question.certification_id, sa.func.count()).group_by(
            corpus.Question.certification_id
        )
    ).all()
    return dict(rows)


def counts_by_exam(session: Session) -> dict[str, int]:
    rows = session.execute(
        sa.select(corpus.Question.exam_id, sa.func.count()).group_by(corpus.Question.exam_id)
    ).all()
    return {exam: count for exam, count in rows if exam is not None}


def counts_by_origin(session: Session) -> dict[str, int]:
    rows = session.execute(
        sa.select(corpus.Question.origin, sa.func.count()).group_by(corpus.Question.origin)
    ).all()
    return dict(rows)


def top_references(session: Session, limit: int = 10) -> list[tuple[str, str, int]]:
    """The most-cited pages: `(id, display_url, questions citing it)`.

    The reading list (023) does real work over this graph; this is the flat view
    that 010 shows on a question and that 047 uses to say the projection looks sane.
    """
    rows = session.execute(
        sa.select(corpus.Reference.id, corpus.Reference.display_url, corpus.Reference.question_count)
        .order_by(corpus.Reference.question_count.desc(), corpus.Reference.id)
        .limit(limit)
    ).all()
    return [(reference_id, url, count) for reference_id, url, count in rows]


def current_ingest_run(session: Session) -> corpus.IngestRun | None:
    """The run that built this projection, or None when nothing is projected yet."""
    fingerprint = session.scalar(sa.select(corpus.Question.ingest_run_id).limit(1))
    if fingerprint is None:
        return None
    return session.get(corpus.IngestRun, fingerprint)
