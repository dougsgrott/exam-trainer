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

import json
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


# ------------------------------------------------------------------- question detail
#
# 010's detail page. Three statements -- the question with its exam, certification
# and domain; its options; its references -- rather than an ORM object the template
# can lazily walk into an N+1.


@dataclass(frozen=True)
class OptionRow:
    id: str
    label: str
    position: int
    text_md: str
    is_correct: bool
    explanation_md: str | None


@dataclass(frozen=True)
class ReferenceRow:
    id: str
    display_url: str
    host: str | None
    raw_url: str
    position: int
    question_count: int


@dataclass(frozen=True)
class QuestionDetail:
    id: str
    prompt_md: str
    overall_explanation_md: str | None
    type: str
    select_count: int
    correct_labels: list
    question_number: int | None
    origin: str
    certification_id: str
    certification_name: str | None
    exam_id: str | None
    exam_title: str | None
    exam_mode: str | None
    domain_label: str | None
    mark: str | None = None
    options: tuple[OptionRow, ...] = ()
    references: tuple[ReferenceRow, ...] = ()

    @property
    def multi(self) -> bool:
        return self.type == "multi_select"

    @property
    def mode(self) -> str:
        return self.exam_mode or "unspecified"


_DETAIL_SQL = """
SELECT question.id, question.prompt_md, question.overall_explanation_md,
       question.type, question.select_count, question.correct_labels,
       question.question_number, question.origin,
       question.certification_id, certification.name AS certification_name,
       question.exam_id, exam.title AS exam_title, exam.mode AS exam_mode,
       question.domain_label,
       CASE WHEN current_mark.value IS NULL OR current_mark.value = 'cleared'
            THEN NULL ELSE current_mark.value END AS mark
  FROM question
  LEFT JOIN exam ON exam.id = question.exam_id
  LEFT JOIN certification ON certification.id = question.certification_id
  LEFT JOIN current_mark ON current_mark.question_id = question.id
 WHERE question.id = :id
"""


def question_detail(session: Session, question_id: str) -> QuestionDetail | None:
    """One question, everything a page shows, or None when there is no such id."""
    row = session.execute(sa.text(_DETAIL_SQL), {"id": question_id}).mappings().first()
    if row is None:
        return None

    options = session.execute(
        sa.select(corpus.QuestionOption)
        .where(corpus.QuestionOption.question_id == question_id)
        .order_by(corpus.QuestionOption.position)
    ).scalars().all()

    references = session.execute(
        sa.text(
            "SELECT reference.id, reference.display_url, reference.host, "
            "       question_reference.raw_url, question_reference.position, "
            "       reference.question_count "
            "  FROM question_reference "
            "  JOIN reference ON reference.id = question_reference.reference_id "
            " WHERE question_reference.question_id = :id "
            " ORDER BY question_reference.position, reference.id"
        ),
        {"id": question_id},
    ).mappings().all()

    correct = row["correct_labels"]
    if isinstance(correct, str):  # JSON comes back as text through a raw statement
        correct = json.loads(correct)

    return QuestionDetail(
        id=row["id"],
        prompt_md=row["prompt_md"],
        overall_explanation_md=row["overall_explanation_md"],
        type=row["type"],
        select_count=int(row["select_count"] or 1),
        correct_labels=list(correct or []),
        question_number=row["question_number"],
        origin=row["origin"],
        certification_id=row["certification_id"],
        certification_name=row["certification_name"],
        exam_id=row["exam_id"],
        exam_title=row["exam_title"],
        exam_mode=row["exam_mode"],
        domain_label=row["domain_label"],
        mark=row["mark"],
        options=tuple(
            OptionRow(
                id=option.id,
                label=option.label,
                position=option.position,
                text_md=option.text_md,
                is_correct=bool(option.is_correct),
                explanation_md=option.explanation_md,
            )
            for option in options
        ),
        references=tuple(
            ReferenceRow(
                id=reference["id"],
                display_url=reference["display_url"],
                host=reference["host"],
                raw_url=reference["raw_url"],
                position=int(reference["position"]),
                question_count=int(reference["question_count"]),
            )
            for reference in references
        ),
    )
