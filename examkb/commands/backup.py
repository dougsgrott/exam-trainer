"""`examkb backup` and `examkb restore`.

Two subcommands in one module because they are two halves of one question: is the
journal safe, and can you get it back. The work is `examkb/backup.py`; what lives
here is argument parsing, the confirmation prompt, and an exit code.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from examkb import backup as backup_module
from examkb.cli import subcommand
from examkb.settings import get_settings


def configure_backup(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--to", default=None, help="Destination directory for the snapshot.")
    parser.add_argument("--keep", type=int, default=None, help="How many snapshots to retain.")
    parser.add_argument("--label", default=None, help="A word to put in the snapshot's name.")
    parser.add_argument(
        "--list", action="store_true", dest="list_only", help="List snapshots and exit."
    )
    parser.add_argument("--quiet", action="store_true", help="Print nothing unless it fails.")


@subcommand("backup", "Snapshot the journal database and verify the snapshot.",
            configure=configure_backup)
def run_backup(args: argparse.Namespace) -> int:
    destination = Path(args.to) if args.to else get_settings().backup_dir

    if args.list_only:
        snapshots = backup_module.list_snapshots(destination)
        if not snapshots:
            print(f"no snapshots in {destination}")
            return 0
        print(f"{len(snapshots)} snapshot(s) in {destination}, newest first:")
        for snapshot in snapshots:
            print(f"  {snapshot}")
        return 0

    try:
        result = backup_module.take_backup(
            destination=destination, keep=args.keep, label=args.label
        )
    except backup_module.BackupError as error:
        print(f"examkb backup: {error}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(result.summary())
    return 0


def configure_restore(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="snapshot", required=True, help="Snapshot to restore.")
    parser.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt. Required off a terminal."
    )
    parser.add_argument("--to", default=None, help="Where the pre-restore snapshot is written.")


@subcommand("restore", "Replace the journal database with a snapshot.",
            configure=configure_restore)
def run_restore(args: argparse.Namespace) -> int:
    settings = get_settings()
    snapshot = Path(args.snapshot)
    if not snapshot.is_absolute() and not snapshot.exists():
        # A bare filename means one of ours, in the backup directory.
        candidate = (Path(args.to) if args.to else settings.backup_dir) / snapshot.name
        if candidate.exists():
            snapshot = candidate

    confirmed = args.yes or _confirm(snapshot, settings.database_path)
    try:
        result = backup_module.restore(
            snapshot,
            confirmed=confirmed,
            destination=Path(args.to) if args.to else None,
        )
    except backup_module.BackupError as error:
        print(f"examkb restore: {error}", file=sys.stderr)
        return 1

    print(result.summary())
    return 0


def _confirm(snapshot: Path, target: Path | None) -> bool:
    """Ask, but only where there is someone to ask. Never assume yes."""
    if not sys.stdin.isatty():
        return False
    print(f"This replaces {target} with {snapshot.name}.")
    print("Attempts, marks, disputes and chat in the current database will be replaced.")
    answer = input("Type the database's name to confirm: ").strip()
    return bool(target) and answer == target.name
