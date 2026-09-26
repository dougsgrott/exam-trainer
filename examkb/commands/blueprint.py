"""`examkb blueprint check` -- the published weights beside the corpus's own.

Read-only, and deliberately not a gate. It exits 0 when the numbers agree and 1
when the *join* broke, because an unjoined domain is a transcription mistake
somebody has to fix; a deviation outside tolerance only prints a warning, because
the corpus is allowed to change and a study tool that refuses to start over a
third of a percentage point would be a study tool nobody uses.
"""

from __future__ import annotations

import argparse
import sys

from examkb import db, migrations
from examkb.cli import subcommand
from examkb.services import blueprint as blueprint_service


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "action",
        choices=("check",),
        help="check: report the published weights against the corpus's own.",
    )
    parser.add_argument(
        "--cert",
        action="append",
        dest="certs",
        metavar="ID",
        help="Certification to check; repeatable. Defaults to every blueprint there is.",
    )
    parser.add_argument("--quiet", action="store_true", help="Print nothing unless it fails.")


def _report(check: blueprint_service.CrossCheck) -> None:
    version = check.version_label or "unversioned"
    sums = "" if check.weights_sum is None else f", weights sum to {check.weights_sum:g}"
    print(f"{check.certification_id}  {version}  {check.regime}{sums}")
    if not check.has_source_document:
        # Loud on purpose. 017's sha256 is the drift mechanism and a blueprint
        # without one cannot be re-verified against anything.
        print("  no source document recorded -- weights cannot be re-verified against a file")

    width = max((len(row.label) for row in check.rows), default=0)
    for row in check.rows:
        published = "    --" if row.published_pct is None else f"{row.published_pct:6.1f}%"
        deviation = row.deviation_pp
        gap = "" if deviation is None else f"  {deviation:5.2f}pp"
        flag = "" if row.within_tolerance else "  OVER TOLERANCE"
        print(
            f"  {row.label:<{width}}  {published}  corpus {row.corpus_pct:6.2f}%"
            f" ({row.question_count:>3}){gap}{flag}"
        )

    worst = check.worst_deviation_pp
    if worst is not None:
        verdict = "within" if check.within_tolerance else "OUTSIDE"
        print(
            f"  worst deviation {worst:.2f}pp, {verdict} the "
            f"{blueprint_service.TOLERANCE_PP}pp tolerance"
        )
    for label in check.unjoined_domains:
        print(f"  UNJOINED corpus domain: {label!r} matches no blueprint node", file=sys.stderr)
    for label in check.unjoined_nodes:
        print(f"  UNJOINED blueprint node: {label!r} matches no corpus domain", file=sys.stderr)


@subcommand("blueprint", "Report a blueprint against the corpus it describes.", configure=configure)
def run(args: argparse.Namespace) -> int:
    if migrations.current_revision() != migrations.head_revision():
        print(
            "examkb blueprint: this database is behind its migrations; "
            "run `examkb db upgrade` first",
            file=sys.stderr,
        )
        return 1

    status = 0
    with db.session_for() as session:
        wanted = args.certs or blueprint_service.certifications_with_blueprints(session)
        if not wanted:
            print(
                "examkb blueprint: no blueprints are ingested; transcribe one into "
                "data/blueprints/<vendor>/ and run `make blueprints && examkb ingest`",
                file=sys.stderr,
            )
            return 1

        for certification_id in wanted:
            try:
                check = blueprint_service.cross_check(session, certification_id)
            except blueprint_service.BlueprintError as error:
                print(f"examkb blueprint: {error}", file=sys.stderr)
                status = 1
                continue
            if not check.joined_cleanly:
                status = 1
            if not args.quiet:
                _report(check)
            elif not check.joined_cleanly:
                _report(check)
        session.rollback()
    return status
