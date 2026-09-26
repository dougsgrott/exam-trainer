"""An abandoned attempt is not an open one.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-26

`0001` gave the chat gate one predicate -- `attempt.submitted_at IS NULL` -- and
named it "the single definition of open". Abandoning was not an operation yet, so
the gap did not show. It does now: abandoning sets `abandoned_at` and leaves
`submitted_at` NULL, so every question in an abandoned exam stays un-chattable for
ever, with no way back short of editing the table.

The same file already knew better elsewhere. `first_exposure_response` filters
`submitted_at IS NOT NULL AND abandoned_at IS NULL`, so the statistics view and the
chat gate disagreed about the same word from the day they were written. This makes
them agree, and 013's service reads the same two columns.

A trigger is a replaceable object (005's rule): dropped and recreated here, with
`0001`'s version restored on downgrade, so the definition lives in exactly one
migration at any revision.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = "chat_thread_requires_no_open_attempt"

# Keyed on the question, not on the thread's shape: otherwise the obvious hole
# stays open -- start an attempt, then ask about one of its questions from
# /browse. "Open" is now "started and not finished", either way of finishing.
OPEN_EXCLUDES_ABANDONED = f"""
CREATE TRIGGER {NAME}
BEFORE INSERT ON chat_thread
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM attempt_item AS item
    JOIN attempt ON attempt.id = item.attempt_id
    WHERE item.question_id = NEW.question_id
      AND attempt.submitted_at IS NULL
      AND attempt.abandoned_at IS NULL
)
BEGIN
    SELECT RAISE(ABORT,
        'chat is closed while an attempt containing this question is open');
END
"""

SUBMITTED_ONLY = f"""
CREATE TRIGGER {NAME}
BEFORE INSERT ON chat_thread
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM attempt_item AS item
    JOIN attempt ON attempt.id = item.attempt_id
    WHERE item.question_id = NEW.question_id
      AND attempt.submitted_at IS NULL
)
BEGIN
    SELECT RAISE(ABORT,
        'chat is closed while an attempt containing this question is open');
END
"""


def upgrade() -> None:
    op.execute(f"DROP TRIGGER {NAME}")
    op.execute(OPEN_EXCLUDES_ABANDONED)


def downgrade() -> None:
    op.execute(f"DROP TRIGGER {NAME}")
    op.execute(SUBMITTED_ONLY)
