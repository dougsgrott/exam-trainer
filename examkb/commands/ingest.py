"""`examkb ingest` -- project `kb/` into the database.

Argument parsing, one transaction, and an exit code. The work is
`examkb/ingest.py`; the transaction boundary is here because it is the thing a
command owns: this process commits, and a crash before the commit leaves the
previous projection exactly as it was.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from examkb import ingest as ingest_module
from examkb.cli import subcommand
from examkb.compat import KBNotFound, StaleShardIndex
from examkb.db import new_engine
from examkb.migrations import current_revision, head_revision
from examkb.settings import get_settings
from sqlalchemy.orm import Session


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Empty the projection first, then rebuild it. Reproduces every key.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="After ingesting, round-trip every text field in kb/ through the database.",
    )
    parser.add_argument("--kb", default=None, help="Corpus directory (default: kb/).")
    parser.add_argument("--quiet", action="store_true", help="Print nothing unless it fails.")


@subcommand("ingest", "Load kb/ into the database projection.", configure=configure)
def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    kb = Path(args.kb).resolve() if args.kb else settings.kb_dir

    if current_revision() != head_revision():
        print(
            "examkb ingest: the database is not at the latest migration; "
            "run `examkb db upgrade` first",
            file=sys.stderr,
        )
        return 1

    engine = new_engine(settings.database_url)
    try:
        with Session(engine) as session:
            try:
                result = ingest_module.ingest(session, kb, rebuild=args.rebuild)
            except KBNotFound as missing:
                print(
                    f"examkb ingest: missing {missing.path}; run `make pipeline` first",
                    file=sys.stderr,
                )
                return 1
            except StaleShardIndex as stale:
                print(f"examkb ingest: {stale}", file=sys.stderr)
                return 1
            except ingest_module.IngestError as error:
                print(f"examkb ingest: {error}", file=sys.stderr)
                return 1

            # Everything above is one transaction. Nothing is visible to another
            # reader until this line, and a crash before it changes nothing.
            session.commit()

            if not args.quiet:
                print(result.summary())

            if args.verify:
                verification = ingest_module.verify(session, kb)
                stream = sys.stdout if verification.ok else sys.stderr
                if not args.quiet or not verification.ok:
                    print(verification.summary(), file=stream)
                if not verification.ok:
                    return 1
    finally:
        engine.dispose()

    return 0
