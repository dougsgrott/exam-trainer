"""`examkb import-marks` -- the user's browser study state, into the journal.

The only part of this that needs explaining is the timestamp. `kb-marks` has no
timestamps: it is `{"<question id>": "known"}` and nothing else. So the import has
to invent one, and "when you happened to run the import" is a worse answer than
"when the browser last wrote the file". The file's mtime is used by default,
`--at` overrides it, and either way it is recorded on the row rather than guessed
at again later.

Re-running is expected, not exceptional -- the blob is the user's real study state
and they will import it more than once. The second run reports everything as
already current and writes nothing.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from examkb import db, migrations
from examkb.cli import subcommand
from examkb.services import marks as marks_service

STDIN = "-"


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "path",
        help=f"The exported kb-marks JSON, or {STDIN!r} to read standard input.",
    )
    parser.add_argument(
        "--at",
        default=None,
        help="ISO timestamp for the imported marks. Defaults to the file's mtime.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be imported and write nothing.",
    )
    parser.add_argument("--quiet", action="store_true", help="Print nothing unless it fails.")


def _when(raw: str | None, fallback: datetime | None) -> datetime | None:
    if raw is None:
        return fallback
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError as error:
        raise ValueError(f"--at {raw!r} is not an ISO timestamp: {error}") from error
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


@subcommand("import-marks", "Import a browser kb-marks blob into the journal.", configure=configure)
def run(args: argparse.Namespace) -> int:
    if migrations.current_revision() != migrations.head_revision():
        print(
            "examkb import-marks: this database is behind its migrations; "
            "run `examkb db upgrade` first",
            file=sys.stderr,
        )
        return 1

    if args.path == STDIN:
        raw, mtime = sys.stdin.read(), None
    else:
        path = Path(args.path).expanduser()
        if not path.is_file():
            print(f"examkb import-marks: no such file: {path}", file=sys.stderr)
            return 1
        raw, mtime = marks_service.read_blob(path)

    try:
        at = _when(args.at, mtime)
    except ValueError as error:
        print(f"examkb import-marks: {error}", file=sys.stderr)
        return 1

    with db.session_for() as session:
        try:
            result = marks_service.import_marks(session, raw, at=at)
        except marks_service.MarkError as error:
            print(f"examkb import-marks: {error}", file=sys.stderr)
            return 1
        if args.dry_run:
            session.rollback()
        else:
            session.commit()

    if not args.quiet:
        print(result.summary())
        if args.dry_run:
            print("  --dry-run: nothing was written")
    return 0
