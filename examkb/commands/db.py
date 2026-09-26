"""`examkb db` -- apply, roll back and inspect migrations.

Thin on purpose: argument parsing and the exit code live here, everything else is
`examkb/migrations.py`. `db upgrade` is the command a fresh checkout runs first,
so its failure modes are the ones that get the clear messages.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from examkb import backup, migrations
from examkb.cli import subcommand
from examkb.settings import get_settings


def configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="db_action", metavar="<action>")

    upgrade = actions.add_parser("upgrade", help="Apply migrations up to a revision (default head).")
    upgrade.add_argument("revision", nargs="?", default="head")
    _add_backup_flags(upgrade)

    downgrade = actions.add_parser("downgrade", help="Roll back to a revision, or to `base`.")
    downgrade.add_argument("revision")
    _add_backup_flags(downgrade)

    actions.add_parser("current", help="Print the revision this database is at.")
    actions.add_parser("history", help="List every revision, newest first.")


def _add_backup_flags(parser: argparse.ArgumentParser) -> None:
    """Both migrating actions rewrite the journal's container, so both are gated."""
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Migrate without a verified backup first. Says so, loudly.",
    )
    parser.add_argument("--backup-to", default=None, help="Where the pre-migration snapshot goes.")


@subcommand(
    "db",
    "Apply, roll back and inspect database migrations.",
    configure=configure,
)
def run(args: argparse.Namespace) -> int:
    action = getattr(args, "db_action", None)
    if action is None:
        print("examkb db: pick an action -- upgrade, downgrade, current, history", file=sys.stderr)
        return 2

    url = get_settings().database_url
    try:
        if action in {"upgrade", "downgrade"} and not _gate(args, url):
            return 1

        if action == "upgrade":
            migrations.upgrade(args.revision, url=url)
            print(f"{_where(url)} is at {migrations.current_revision(url) or 'base'}")
            return 0

        if action == "downgrade":
            migrations.downgrade(args.revision, url=url)
            print(f"{_where(url)} is at {migrations.current_revision(url) or 'base'}")
            return 0

        if action == "current":
            current = migrations.current_revision(url)
            head = migrations.head_revision(url)
            settings = get_settings()
            missing = settings.database_path is not None and not settings.database_path.exists()
            state = "no database yet" if missing else (current or "base (no migrations applied)")
            print(f"{_where(url)}: {state}")
            if current != head:
                print(f"head is {head}; run `examkb db upgrade`", file=sys.stderr)
                return 1
            return 0

        if action == "history":
            head = migrations.head_revision(url)
            for revision, description in migrations.revisions(url):
                marker = " (head)" if revision == head else ""
                print(f"{revision}{marker}  {description}")
            return 0
    except migrations.MigrationsNotFound as error:
        print(f"examkb db: {error}", file=sys.stderr)
        return 1

    print(f"examkb db: unknown action {action!r}", file=sys.stderr)
    return 2


def _gate(args: argparse.Namespace, url: str) -> bool:
    """A verified snapshot, before the migration touches anything.

    The journal is the only unregenerable thing here, and a migration is the one
    routine operation that rewrites its container. `--no-backup` exists because an
    unwritable destination should not be able to strand someone who knows what
    they are doing -- but it prints, so the skip is never silent.
    """
    if args.no_backup:
        print("examkb db: --no-backup given; migrating without a snapshot", file=sys.stderr)
        return True

    try:
        result = backup.gate_before_migration(
            url=url,
            destination=Path(args.backup_to) if args.backup_to else None,
            label=f"pre-{args.db_action}",
        )
    except backup.BackupError as error:
        print(f"examkb db: refusing to migrate -- {error}", file=sys.stderr)
        return False

    print(result.summary())
    return True


def _where(url: str) -> str:
    """The database, named the way the user thinks of it: a path, or the URL."""
    settings = get_settings()
    if settings.database_path is not None and url == settings.database_url:
        return str(settings.database_path)
    return url
