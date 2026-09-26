"""Snapshots of the one thing in this repo that cannot be rebuilt.

`kb/` is a pure function of `data/` and the projection is a pure function of
`kb/`, so both are droppable. Attempts, marks, disputes and chat are not: they
exist in this database and nowhere else. That asymmetry is the whole reason this
module exists, and it sets every rule in it.

**`VACUUM INTO`, never `cp`.** The database runs in WAL mode, so the newest pages
can be sitting in `examkb.db-wal` while `examkb.db` looks complete. Copying the
file gives you something that opens cleanly and is quietly missing this morning's
attempt -- the worst possible failure, because it is invisible until you need it.
`VACUUM INTO` asks SQLite for a consistent snapshot of the *database*, WAL
included, and is safe to run while the app is using it.

**A backup that was not verified is not a backup.** Every snapshot is reopened and
checked (`integrity_check`, `foreign_key_check`) before it is given its final name;
a failed check deletes the partial file and raises rather than leaving something
that looks like a backup in the directory.

**Off-box by default.** `/mnt/c/exam-kb-backups` when WSL exposes a Windows side,
because a WSL disk loss takes `~` with it.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from examkb.db import database_path
from examkb.migrations import current_revision
from examkb.settings import get_settings

PREFIX = "examkb-"
SUFFIX = ".db"
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
NO_REVISION = "none"

_NAME = re.compile(
    rf"^{re.escape(PREFIX)}(?P<stamp>\d{{8}}T\d{{6}}Z)-r(?P<revision>[^-.]+)"
    rf"(?:-(?P<label>.+?))?(?:-(?P<ordinal>\d+))?{re.escape(SUFFIX)}$"
)


class BackupError(RuntimeError):
    """A snapshot could not be taken, or could not be trusted once taken."""


@dataclass(frozen=True)
class Snapshot:
    path: Path
    taken_at: datetime
    revision: str | None
    label: str | None
    size_bytes: int

    @property
    def name(self) -> str:
        return self.path.name

    def __str__(self) -> str:
        size = f"{self.size_bytes / 1_000_000:.1f} MB"
        revision = self.revision or "unmigrated"
        return f"{self.name}  ({size}, revision {revision})"


@dataclass(frozen=True)
class BackupResult:
    """What `take_backup` did, including deciding there was nothing to do."""

    snapshot: Snapshot | None
    destination: Path
    pruned: list[Path]
    elapsed_ms: int
    skipped: str | None = None

    def summary(self) -> str:
        if self.snapshot is None:
            return f"backup: {self.skipped}"
        lines = [f"backup: {self.snapshot.path} -- verified in {self.elapsed_ms} ms"]
        if self.pruned:
            names = ", ".join(path.name for path in self.pruned)
            lines.append(f"  pruned {len(self.pruned)} older snapshot(s): {names}")
        return "\n".join(lines)


# ------------------------------------------------------------------------ verifying


def verify_database(path: Path) -> str | None:
    """None when the file is a healthy SQLite database, else what is wrong with it.

    Deliberately opens its own connection with nothing else attached: the point is
    to read the bytes on disk the way a restore would, not to ask the process that
    just wrote them whether it is happy.
    """
    if not path.exists():
        return f"{path} does not exist"
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as error:  # pragma: no cover -- open failures are rare
        return f"{path} cannot be opened: {error}"
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            return f"{path} failed integrity_check: {integrity[0] if integrity else 'no result'}"
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            return f"{path} has {len(violations)} foreign key violation(s)"
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError as error:
        return f"{path} is not a readable database: {error}"
    finally:
        connection.close()
    return None


# ------------------------------------------------------------------------- snapshots


def parse_snapshot(path: Path) -> Snapshot | None:
    """A snapshot from its filename, or None when the file is not one of ours."""
    match = _NAME.match(path.name)
    if match is None:
        return None
    revision = match.group("revision")
    return Snapshot(
        path=path,
        taken_at=datetime.strptime(match.group("stamp"), STAMP_FORMAT).replace(
            tzinfo=timezone.utc
        ),
        revision=None if revision == NO_REVISION else revision,
        label=match.group("label"),
        size_bytes=path.stat().st_size if path.exists() else 0,
    )


def list_snapshots(destination: Path | None = None) -> list[Snapshot]:
    """Every snapshot in the destination, newest first."""
    destination = Path(destination or get_settings().backup_dir)
    if not destination.is_dir():
        return []
    found = [parse_snapshot(path) for path in destination.glob(f"{PREFIX}*{SUFFIX}")]
    return sorted(
        (snapshot for snapshot in found if snapshot is not None),
        key=lambda snapshot: (snapshot.taken_at, snapshot.name),
        reverse=True,
    )


def snapshot_name(taken_at: datetime, revision: str | None, label: str | None) -> str:
    stamp = taken_at.astimezone(timezone.utc).strftime(STAMP_FORMAT)
    parts = [f"{PREFIX}{stamp}", f"r{revision or NO_REVISION}"]
    if label:
        parts.append(re.sub(r"[^a-zA-Z0-9]+", "-", label).strip("-"))
    return "-".join(parts) + SUFFIX


def prune(destination: Path, keep: int) -> list[Path]:
    """Delete all but the `keep` newest snapshots. Never the newest, ever."""
    keep = max(1, keep)
    doomed = [snapshot.path for snapshot in list_snapshots(destination)[keep:]]
    for path in doomed:
        path.unlink(missing_ok=True)
    return doomed


# ---------------------------------------------------------------------------- backup


def take_backup(
    *,
    url: str | None = None,
    destination: Path | None = None,
    keep: int | None = None,
    label: str | None = None,
) -> BackupResult:
    """Snapshot the database, verify it, prune old ones. Never a partial file.

    Returns a result with `snapshot=None` when there is nothing to back up -- a
    fresh checkout has no database, and refusing the first `db upgrade` over that
    would be a gate that only ever blocks the one case it does not need to.
    """
    started = time.monotonic()
    settings = get_settings()
    url = url or settings.database_url
    destination = Path(destination or settings.backup_dir)
    keep = settings.backup_keep if keep is None else keep

    source = database_path(url)
    if source is None:
        raise BackupError(
            f"backup understands SQLite files only, and DATABASE_URL is {url!r}; "
            "use the database server's own backup tool"
        )
    if not source.exists():
        return BackupResult(
            snapshot=None,
            destination=destination,
            pruned=[],
            elapsed_ms=0,
            skipped=f"no database at {source} yet -- nothing to back up",
        )

    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BackupError(f"cannot create the backup directory {destination}: {error}") from error
    if not os.access(destination, os.W_OK):
        raise BackupError(f"the backup directory {destination} is not writable")

    revision = _revision_or_none(url)
    taken_at = datetime.now(timezone.utc)
    final = _unused_path(destination, snapshot_name(taken_at, revision, label))
    partial = final.with_name(final.name + ".partial")
    partial.unlink(missing_ok=True)

    connection = sqlite3.connect(source)
    try:
        # One statement, and it is the whole point of this module: a consistent
        # snapshot of the database including whatever is still in the WAL.
        connection.execute("VACUUM INTO ?", (str(partial),))
    except sqlite3.Error as error:
        partial.unlink(missing_ok=True)
        raise BackupError(f"VACUUM INTO failed for {source}: {error}") from error
    finally:
        connection.close()

    problem = verify_database(partial)
    if problem is not None:
        partial.unlink(missing_ok=True)
        raise BackupError(f"the snapshot did not verify and was discarded -- {problem}")

    partial.replace(final)
    snapshot = parse_snapshot(final)
    if snapshot is None:  # pragma: no cover -- only if the name format changed
        raise BackupError(f"wrote {final} but cannot parse its name back")

    return BackupResult(
        snapshot=snapshot,
        destination=destination,
        pruned=prune(destination, keep),
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def _revision_or_none(url: str) -> str | None:
    """The migration revision for the snapshot's name -- never a reason to fail.

    A database damaged enough that `alembic_version` will not read is exactly the
    one whose backup matters most. The name loses a detail; `VACUUM INTO` is what
    decides whether there is a snapshot at all.
    """
    try:
        return current_revision(url)
    except (SQLAlchemyError, sqlite3.DatabaseError):
        return None


def _unused_path(destination: Path, name: str) -> Path:
    """`name`, or `name-2`, `name-3`… Two snapshots can land in the same second."""
    path = destination / name
    ordinal = 1
    while path.exists():
        ordinal += 1
        path = destination / f"{Path(name).stem}-{ordinal}{SUFFIX}"
    return path


# ------------------------------------------------------------------------- the gate


def gate_before_migration(
    *,
    url: str | None = None,
    destination: Path | None = None,
    keep: int | None = None,
    label: str = "pre-upgrade",
) -> BackupResult:
    """Take a verified backup, or raise and let the migration not happen.

    A migration is the one routine operation that rewrites the journal's container.
    Taking the snapshot *after* it, or not checking that the snapshot is readable,
    would mean discovering both problems at the same moment -- the moment you
    needed the backup.
    """
    return take_backup(url=url, destination=destination, keep=keep, label=label)


# --------------------------------------------------------------------------- restore


@dataclass(frozen=True)
class RestoreResult:
    snapshot: Snapshot
    restored_to: Path
    previous: BackupResult

    def summary(self) -> str:
        lines = [f"restore: {self.restored_to} <- {self.snapshot.name}"]
        if self.previous.snapshot is not None:
            lines.append(f"  the journal it replaced is at {self.previous.snapshot.path}")
        return "\n".join(lines)


def restore(
    snapshot_path: Path,
    *,
    url: str | None = None,
    confirmed: bool = False,
    destination: Path | None = None,
) -> RestoreResult:
    """Overwrite the live journal with a snapshot. Refuses unless `confirmed`.

    The live database is snapshotted first, labelled `pre-restore`. Restoring the
    wrong file is a mistake someone makes exactly once, and it should cost them a
    filename rather than their history.
    """
    if not confirmed:
        raise BackupError(
            "restore overwrites the journal -- attempts, marks, disputes and chat. "
            "Re-run with --yes, or confirm at the prompt."
        )

    snapshot_path = Path(snapshot_path)
    problem = verify_database(snapshot_path)
    if problem is not None:
        raise BackupError(f"refusing to restore from a snapshot that does not verify -- {problem}")

    settings = get_settings()
    url = url or settings.database_url
    target = database_path(url)
    if target is None:
        raise BackupError(f"restore understands SQLite files only, and DATABASE_URL is {url!r}")

    previous = take_backup(url=url, destination=destination, label="pre-restore")

    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(target.name + ".restoring")
    shutil.copyfile(snapshot_path, staged)
    staged.replace(target)
    # The old WAL and shared-memory files describe the database that was just
    # replaced. Leaving them beside the new one is how a good restore becomes a
    # corrupt database on the next open.
    for sidecar in (f"{target}-wal", f"{target}-shm"):
        Path(sidecar).unlink(missing_ok=True)

    snapshot = parse_snapshot(snapshot_path) or Snapshot(
        path=snapshot_path,
        taken_at=datetime.fromtimestamp(snapshot_path.stat().st_mtime, tz=timezone.utc),
        revision=None,
        label=None,
        size_bytes=snapshot_path.stat().st_size,
    )
    return RestoreResult(snapshot=snapshot, restored_to=target, previous=previous)
