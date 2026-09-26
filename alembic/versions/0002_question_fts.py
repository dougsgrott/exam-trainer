"""The FTS5 index over the corpus, and the back-fill that makes it true on arrival.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26

Three choices in here are not obvious, and each was forced by something real:

1. **Not an external-content table.** FTS5's `content=` option keys rows by an
   integer `rowid`, and every PROJECTION key in this schema is content-derived
   TEXT (`ccao-f/exam-01/q001`) precisely so a rebuild reproduces it. The indexed
   text also spans two tables -- the prompt and overall explanation live on
   `question`, the option text and per-option explanations on `question_option` --
   which no single content table covers. So this is an ordinary FTS5 table holding
   its own copy of the text: about 2 MB more on disk, in exchange for `snippet()`
   (a contentless table cannot produce one) and a key nobody has to keep in step.

2. **The index is populated here, not left for the first ingest.** Re-ingesting an
   unchanged corpus writes nothing at all by design (006), so an empty index would
   have stayed empty until somebody happened to run `--rebuild` -- and search would
   have quietly returned nothing on a database that looked healthy. The back-fill
   below leaves `db upgrade` with a consistent database whatever runs next.

3. **The DDL lives here and only here.** `examkb/services/search.py` rebuilds the
   index by reading this statement back out of `sqlite_master` and re-executing it,
   so there is no second copy of the definition in the application to go stale --
   the same rule `0001` follows for the views and triggers.

`group_concat(x, sep ORDER BY y)` needs SQLite 3.44; this box runs 3.45.1 and the
project is SQLite-only until `DATABASE_URL` says otherwise.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `porter` so "caching" finds "cache"; `remove_diacritics 2` so a pasted "café"
# finds "cafe". Column order is load-bearing: `bm25()` takes one weight per column
# in this order, and `search.py` weights the prompt above the explanations.
CREATE_FTS = """
CREATE VIRTUAL TABLE question_fts USING fts5(
    question_id UNINDEXED,
    prompt,
    options,
    explanation,
    tokenize = 'porter unicode61 remove_diacritics 2'
)
"""

# The same three strings `search.py` builds in Python, in the same order, so the
# index this migration leaves behind and the one a rebuild produces hold the same
# text. A test asserts they agree rather than trusting that they do.
BACKFILL_FTS = """
INSERT INTO question_fts (question_id, prompt, options, explanation)
SELECT question.id,
       question.prompt_md,
       coalesce(
           (SELECT group_concat(option.text_md, char(10) ORDER BY option.position)
              FROM question_option option
             WHERE option.question_id = question.id),
           ''
       ),
       trim(
           coalesce(question.overall_explanation_md, '') || char(10) ||
           coalesce(
               (SELECT group_concat(option.explanation_md, char(10) ORDER BY option.position)
                  FROM question_option option
                 WHERE option.question_id = question.id
                   AND option.explanation_md IS NOT NULL),
               ''
           ),
           char(10) || ' '
       )
  FROM question
 ORDER BY question.id
"""


def upgrade() -> None:
    op.execute(CREATE_FTS)
    op.execute(BACKFILL_FTS)


def downgrade() -> None:
    # Dropping the virtual table takes its five shadow tables with it.
    op.execute("DROP TABLE question_fts")
