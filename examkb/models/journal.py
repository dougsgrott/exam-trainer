"""JOURNAL tables: everything nothing can regenerate.

Attempts, marks, disputes, chat, jobs and candidates exist in this database and
nowhere else. `kb/` is a pure function of `data/` and the projection is a pure
function of `kb/`, so both are droppable; this module is why 007 (backup) and 027
(annotation export) exist.

Two rules hold across every table here:

1. **No foreign key into PROJECTION.** Journal rows carry a plain `question_id`
   (or `blueprint_node_id`, or `certification_id`) with an index and no referential
   action, because a question that leaves `kb/` leaves the projection -- and the
   attempt that was sat on it is still history. `attempt_item.snapshot_json` is
   what makes that survivable: the question as displayed is stored in the row.
2. **Ingest never writes here.** 006 asserts it by snapshotting every table in this
   module around a rebuild.

Two triggers defend invariants that the service layer must not be the only thing
enforcing; both are created in `0001_initial` and both are asserted against SQLite
directly, not through a service: `attempt_item` snapshots are immutable once
answered (013), and no chat thread may open on a question that sits in an
unsubmitted attempt (041).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from examkb.models.base import Journal, UTCDateTime
from examkb.models.corpus import enum_check

MARK_VALUES = ("known", "unsure", "flagged", "cleared")
MARK_SOURCES = ("ui", "import")
VERIFICATION_LEVELS = ("unverified", "ai_reviewed", "human_reviewed", "known_bad")
VERIFICATION_SOURCES = ("ai_review", "human", "import")
DISPUTE_STATES = ("open", "accepted", "rejected")
CHAT_SCOPES = ("results", "browse")
CHAT_ROLES = ("user", "assistant", "system")
LLM_OUTCOMES = ("ok", "error", "timeout", "refused")
JOB_STATES = ("queued", "running", "done", "failed", "interrupted", "cancelled")
JOB_ITEM_STATES = ("pending", "running", "done", "failed", "skipped")
CANDIDATE_STATES = ("draft", "queued", "approved", "rejected", "superseded")
CANDIDATE_ACTIONS = ("approve", "reject", "edit", "skip", "undo")
PROPOSAL_STATES = ("proposed", "accepted", "rejected")


# -------------------------------------------------------------------------- study state


class Mark(Journal):
    """A self-reported mark on a question. Append-only: the history *is* the fix.

    What this replaces is `localStorage['kb-marks']` -- three values, one browser,
    no history. Clearing a mark writes a `cleared` row rather than deleting one, so
    "known in March, unsure in June" survives; `current_mark` (a view) is what the
    UI reads.
    """

    __tablename__ = "mark"
    __table_args__ = (
        enum_check("value", MARK_VALUES, "value_known"),
        enum_check("source", MARK_SOURCES, "source_known"),
        Index("ix_mark_question_id_created_at", "question_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="ui")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


# ------------------------------------------------------------------------------ attempts


class Attempt(Journal):
    """One sitting. `submitted_at IS NULL` is the single definition of "open".

    The sampler's seed and its filters are recorded so the draw is reproducible
    from the row (012), and `apportionment_json` records what the blueprint asked
    for against what the pool could supply (019) -- an exam that under-covered a
    domain says so instead of quietly reporting a mix it did not sit.
    """

    __tablename__ = "attempt"
    __table_args__ = (
        Index("ix_attempt_certification_id_started_at", "certification_id", "started_at"),
        Index("ix_attempt_submitted_at", "submitted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    certification_id: Mapped[str] = mapped_column(Text, nullable=False)
    exam_id: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    abandoned_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requested_count: Mapped[int | None] = mapped_column(Integer)
    sampler_json: Mapped[dict | None] = mapped_column(JSON)
    weight_source: Mapped[str | None] = mapped_column(Text)  # official / corpus / none
    apportionment_json: Mapped[dict | None] = mapped_column(JSON)
    format_profile_json: Mapped[dict | None] = mapped_column(JSON)
    time_limit_seconds: Mapped[int | None] = mapped_column(Integer)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    correct_count: Mapped[int | None] = mapped_column(Integer)
    credit_total: Mapped[float | None] = mapped_column(Float)
    scaled_score: Mapped[int | None] = mapped_column(Integer)
    passed: Mapped[bool | None] = mapped_column(Boolean)
    graded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AttemptItem(Journal):
    """One question as it was actually shown, and what was done with it.

    `snapshot_json` is the question exactly as displayed and `option_order` is the
    permutation it was displayed in; together they are what lets 016 replay an exam
    years later, after `ingest --rebuild` has replaced the corpus text and 040 has
    overridden the key. A trigger refuses any update to either once `answered_at`
    is set.

    `is_correct` and `credit` are written once by the grader and never retroactively
    changed -- an accepted dispute produces an explicit regrade (040), recorded as
    its own row, rather than a silent rewrite of history.
    """

    __tablename__ = "attempt_item"
    __table_args__ = (
        UniqueConstraint("attempt_id", "position", name="uq_attempt_item_attempt_id_position"),
        Index("ix_attempt_item_attempt_id", "attempt_id"),
        Index("ix_attempt_item_question_id", "question_id"),
        Index("ix_attempt_item_blueprint_node_id", "blueprint_node_id"),
        Index("ix_attempt_item_answered_at", "answered_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("attempt.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    question_id: Mapped[str] = mapped_column(Text, nullable=False)
    blueprint_node_id: Mapped[str | None] = mapped_column(Text)
    snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    option_order: Mapped[list] = mapped_column(JSON, nullable=False)
    selected_labels: Mapped[list | None] = mapped_column(JSON)
    first_shown_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    answered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    time_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    change_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seen_ordinal: Mapped[int | None] = mapped_column(Integer)
    is_correct: Mapped[bool | None] = mapped_column(Boolean)
    credit: Mapped[float | None] = mapped_column(Float)
    graded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    graded_key: Mapped[list | None] = mapped_column(JSON)
    override_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("override.id"))


class Regrade(Journal):
    """An explicit, deliberate re-score of a past attempt against an override.

    Never automatic. The original numbers stay on the row beside the new ones, so
    "you scored 47, and 49 under the corrected key" is one query and neither number
    is lost.
    """

    __tablename__ = "regrade"
    __table_args__ = (Index("ix_regrade_attempt_id", "attempt_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("attempt.id", ondelete="CASCADE"), nullable=False
    )
    override_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("override.id"))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    original_correct_count: Mapped[int | None] = mapped_column(Integer)
    original_credit_total: Mapped[float | None] = mapped_column(Float)
    original_scaled_score: Mapped[int | None] = mapped_column(Integer)
    new_correct_count: Mapped[int | None] = mapped_column(Integer)
    new_credit_total: Mapped[float | None] = mapped_column(Float)
    new_scaled_score: Mapped[int | None] = mapped_column(Integer)


class RegradeItem(Journal):
    __tablename__ = "regrade_item"
    __table_args__ = (
        UniqueConstraint(
            "regrade_id", "attempt_item_id", name="uq_regrade_item_regrade_id_attempt_item_id"
        ),
        Index("ix_regrade_item_regrade_id", "regrade_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    regrade_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("regrade.id", ondelete="CASCADE"), nullable=False
    )
    attempt_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("attempt_item.id"), nullable=False
    )
    was_correct: Mapped[bool | None] = mapped_column(Boolean)
    now_correct: Mapped[bool | None] = mapped_column(Boolean)
    was_credit: Mapped[float | None] = mapped_column(Float)
    now_credit: Mapped[float | None] = mapped_column(Float)


# ------------------------------------------------------------- verification and disputes


class VerificationEvent(Journal):
    """The only thing ever written about a question's trustworthiness.

    `verification` itself is a view (`question_verification`) with explicit
    precedence -- `known_bad` > open dispute > `human_reviewed` > `ai_reviewed` >
    `unverified` -- and never a mutable column, so no re-run can silently overwrite
    a judgement. Events are append-only; the view is what anyone reads.
    """

    __tablename__ = "verification_event"
    __table_args__ = (
        enum_check("level", VERIFICATION_LEVELS, "level_known"),
        enum_check("source", VERIFICATION_SOURCES, "source_known"),
        Index("ix_verification_event_question_id_created_at", "question_id", "created_at"),
        Index("ix_verification_event_level", "level"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    note_md: Mapped[str | None] = mapped_column(Text)
    llm_call_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("llm_call.id"))
    candidate_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("candidate.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class Dispute(Journal):
    """"This key looks wrong" -- first-class on any question, corpus or generated.

    An open dispute changes the derived verification immediately and closing it
    changes it back. A disputed question **stays in the exam pool**, flagged, and
    contributes to mastery at trust weight 0.3 (020). Nothing vanishes silently.

    The two provenance pointers -- which answer it was disputed from, which chat
    turn raised it -- are plain columns, not foreign keys. `attempt_item` points at
    `override`, `override` points at `dispute`, and a key declared here would close
    a reference cycle that SQLite cannot break: there is no ADD CONSTRAINT to defer
    one edge to. The cycle is broken at its least load-bearing edge, and these two
    columns are indexed and documented rather than enforced.
    """

    __tablename__ = "dispute"
    __table_args__ = (
        enum_check("state", DISPUTE_STATES, "state_known"),
        Index("ix_dispute_question_id_state", "question_id", "state"),
        Index("ix_dispute_state", "state"),
        Index("ix_dispute_attempt_item_id", "attempt_item_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_item_id: Mapped[int | None] = mapped_column(Integer)
    chat_thread_id: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    claim_md: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolution_md: Mapped[str | None] = mapped_column(Text)


class Override(Journal):
    """A corrected answer key, used for **new** grading only.

    The whole of question versioning, patch files and a JSON-Patch applier was cut
    and replaced by three things: one mutable projection row, this table, and the
    attempt snapshot. Retiring an override restores the original key for future
    grading and leaves every past grade exactly where it was.
    """

    __tablename__ = "override"
    __table_args__ = (
        Index("ix_override_question_id_retired_at", "question_id", "retired_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[str] = mapped_column(Text, nullable=False)
    dispute_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("dispute.id"))
    correct_labels: Mapped[list] = mapped_column(JSON, nullable=False)
    reason_md: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


# ---------------------------------------------------------------------------------- chat


class ChatThread(Journal):
    """A conversation about one question, reachable only from the results page.

    The gate is a trigger keyed on the **question**, not on the thread's shape: no
    thread may be created for a question that sits in any attempt with
    `submitted_at IS NULL`. Keying it on the thread would leave the obvious hole --
    open an attempt, then chat about one of its questions from `/browse`.
    """

    __tablename__ = "chat_thread"
    __table_args__ = (
        enum_check("scope", CHAT_SCOPES, "scope_known"),
        Index("ix_chat_thread_question_id", "question_id"),
        Index("ix_chat_thread_attempt_id", "attempt_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("attempt.id"))
    attempt_item_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("attempt_item.id"))
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class ChatMessage(Journal):
    """One turn. `is_partial` is set while a stream is in flight and cleared on
    completion, so a dropped stream leaves a coherent partial message rather than
    an empty row. Chat Markdown is rendered by a different renderer from the corpus
    subset and never enters `kb/` or `data/`."""

    __tablename__ = "chat_message"
    __table_args__ = (
        enum_check("role", CHAT_ROLES, "role_known"),
        Index("ix_chat_message_thread_id_created_at", "thread_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thread_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chat_thread.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_partial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    finish_reason: Mapped[str | None] = mapped_column(Text)
    llm_call_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("llm_call.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


# ------------------------------------------------------------------------- LLM and jobs


class LlmCall(Journal):
    """Every model call, including the ones that failed.

    The hash chain, the budget ledger and HTTP 402 were cut; this table is what
    replaced them. It is also the V1a gate's assertion: `SELECT count(*) FROM
    llm_call` is 0 through issues 001–022, and the first row anything writes here
    arrives with 029.

    `subject_type` / `subject_id` point loosely at what a call produced (a
    candidate, a question's classification) without a foreign key, because the
    thing produced may be a PROJECTION row and journal never points into projection.
    """

    __tablename__ = "llm_call"
    __table_args__ = (
        enum_check("outcome", LLM_OUTCOMES, "outcome_known", nullable=True),
        Index("ix_llm_call_purpose_started_at", "purpose", "started_at"),
        Index("ix_llm_call_job_id", "job_id"),
        Index("ix_llm_call_outcome", "outcome"),
        Index("ix_llm_call_subject_type_subject_id", "subject_type", "subject_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("job.id"))
    subject_type: Mapped[str | None] = mapped_column(Text)
    subject_id: Mapped[str | None] = mapped_column(Text)
    prompt_sha256: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    semaphore_limit: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Job(Journal):
    """A long-running CLI job. HTTP enqueues; the web process never runs one.

    A 549-question backfill is 1.2–2.3 hours and `uvicorn --reload` kills it on
    every code edit, which is the entire reason this table exists. `claimed_by` is
    what stops two runners taking the same job: the claim is an UPDATE guarded on
    `state = 'queued'`, so the loser sees zero rows changed.
    """

    __tablename__ = "job"
    __table_args__ = (
        enum_check("state", JOB_STATES, "state_known"),
        Index("ix_job_state_created_at", "state", "created_at"),
        Index("ix_job_kind", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    params_json: Mapped[dict | None] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    total_items: Mapped[int | None] = mapped_column(Integer)
    completed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checkpoint_json: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    claimed_by: Mapped[str | None] = mapped_column(Text)
    resume_command: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class JobItem(Journal):
    """One checkpointed unit of work. A kill at item 400 resumes at 401 because the
    first 399 are `done` here, not because a counter said so. A failed item records
    its error and does not abort the run."""

    __tablename__ = "job_item"
    __table_args__ = (
        enum_check("state", JOB_ITEM_STATES, "state_known"),
        UniqueConstraint("job_id", "item_key", name="uq_job_item_job_id_item_key"),
        Index("ix_job_item_job_id_state", "job_id", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job.id", ondelete="CASCADE"), nullable=False
    )
    item_key: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


# ---------------------------------------------------------------------------- generation


class GenerationRun(Journal):
    """The run manifest: model, prompt version, and what the generation was grounded on.

    `degraded_exemplar_mode` is recorded explicitly, with the certification the
    exemplars were borrowed from. A cold-start generation that quietly used another
    vendor's register is the failure this column exists to make impossible to miss.
    """

    __tablename__ = "generation_run"
    __table_args__ = (
        Index("ix_generation_run_certification_id", "certification_id"),
        Index("ix_generation_run_job_id", "job_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # the data/generated/ run id
    job_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("job.id"))
    certification_id: Mapped[str] = mapped_column(Text, nullable=False)
    blueprint_id: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    grounding_json: Mapped[dict | None] = mapped_column(JSON)
    degraded_exemplar_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exemplar_certification_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Candidate(Journal):
    """An unapproved generated question. Disposable by design.

    One write path per lifecycle stage: a question is either an unapproved
    candidate (here, throwaway) or approved (a file under `data/generated/` is the
    truth and this database is a cache). **Approval is what writes the file** --
    deleting every row in this table loses nothing that was approved.
    """

    __tablename__ = "candidate"
    __table_args__ = (
        enum_check("state", CANDIDATE_STATES, "state_known"),
        Index("ix_candidate_state_created_at", "state", "created_at"),
        Index("ix_candidate_generation_run_id", "generation_run_id"),
        Index("ix_candidate_normalized_sha256", "normalized_sha256"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("generation_run.id", ondelete="CASCADE"), nullable=False
    )
    blueprint_node_id: Mapped[str | None] = mapped_column(Text)
    llm_call_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("llm_call.id"))
    type: Mapped[str] = mapped_column(Text, nullable=False)
    select_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    prompt_md: Mapped[str] = mapped_column(Text, nullable=False)
    options_json: Mapped[list] = mapped_column(JSON, nullable=False)
    correct_labels: Mapped[list] = mapped_column(JSON, nullable=False)
    overall_explanation_md: Mapped[str | None] = mapped_column(Text)
    prompt_sha256: Mapped[str | None] = mapped_column(Text)
    normalized_sha256: Mapped[str | None] = mapped_column(Text)
    item_sha256: Mapped[str | None] = mapped_column(Text)
    dedup_max_score: Mapped[float | None] = mapped_column(Float)
    dedup_nearest_question_id: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    published_question_id: Mapped[str | None] = mapped_column(Text)
    published_path: Mapped[str | None] = mapped_column(Text)


class CandidateReview(Journal):
    """One blind voter's re-solve, key withheld. Three per candidate, independent.

    A split vote is stored as three rows and surfaced as a split; it is never
    averaged into a number, and unanimous disagreement with the key flags the
    candidate rather than rewriting it.
    """

    __tablename__ = "candidate_review"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id", "voter_index", name="uq_candidate_review_candidate_id_voter_index"
        ),
        Index("ix_candidate_review_candidate_id", "candidate_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidate.id", ondelete="CASCADE"), nullable=False
    )
    voter_index: Mapped[int] = mapped_column(Integer, nullable=False)
    llm_call_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("llm_call.id"))
    chosen_labels: Mapped[list | None] = mapped_column(JSON)
    agrees_with_key: Mapped[bool | None] = mapped_column(Boolean)
    rationale_md: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class CandidateDecision(Journal):
    """Append-only review-queue actions, which is what makes undo possible.

    Undo restores the previous decision rather than deleting the last one, so the
    trail of what was approved, edited and rejected -- and why -- survives.
    """

    __tablename__ = "candidate_decision"
    __table_args__ = (
        enum_check("action", CANDIDATE_ACTIONS, "action_known"),
        Index("ix_candidate_decision_candidate_id_created_at", "candidate_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidate.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    edited_json: Mapped[dict | None] = mapped_column(JSON)
    undoes_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("candidate_decision.id"))
    actor: Mapped[str] = mapped_column(Text, nullable=False, default="user")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class TaxonProposal(Journal):
    """The taxonomy inbox: a genuine miss, proposed and awaiting one decision.

    Classification lands in the closed official bullet set; anything outside it
    stops here instead of being applied. `blocked_by_taxon_id` records the existing
    vocabulary the trigram gate matched -- "prompt cache" against "prompt caching" --
    so a rejection is inspectable rather than a verdict.

    JOURNAL, not PROJECTION: a proposal is not derivable from `kb/`. Accepting one
    writes it to `data/annotations/`, and the projection picks it up from there.
    """

    __tablename__ = "taxon_proposal"
    __table_args__ = (
        enum_check("state", PROPOSAL_STATES, "state_known"),
        Index("ix_taxon_proposal_state_created_at", "state", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scheme_id: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="proposed")
    question_id: Mapped[str | None] = mapped_column(Text)
    evidence_json: Mapped[dict | None] = mapped_column(JSON)
    blocked_by_taxon_id: Mapped[str | None] = mapped_column(Text)
    similarity: Mapped[float | None] = mapped_column(Float)
    llm_call_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("llm_call.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    decided_note: Mapped[str | None] = mapped_column(Text)
