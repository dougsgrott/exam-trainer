"""Backup: the journal is the only thing here that cannot be rebuilt.

Every test in this file is ultimately about one sentence -- a backup that was not
verified is not a backup -- and about the reason `cp` is not good enough, which
the first test demonstrates rather than asserts: it copies the file the naive way
beside a real snapshot and shows the naive copy is missing this session's work.

Nothing here writes to the real backup destination (`/mnt/c/exam-kb-backups`).
Every test passes an explicit `destination`, and the subprocess ones set
`EXAMKB_BACKUP_DIR`.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import REPO_ROOT
from sqlalchemy.orm import Session

from examkb import backup as backup_module
from examkb.backup import BackupError, list_snapshots, prune, restore, take_backup, verify_database
from examkb.models import journal

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


# ------------------------------------------------------------------------------ helpers


def url_for(engine: sa.Engine) -> str:
    return str(engine.url)


def path_of(engine: sa.Engine) -> Path:
    return Path(engine.url.database)


def write_marks(engine: sa.Engine, count: int) -> None:
    """Journal rows, left in the WAL: the session stays open, so nothing checkpoints."""
    with Session(engine) as session:
        for index in range(count):
            session.add(
                journal.Mark(
                    question_id=f"ccao-f/exam-01/q{index:03d}",
                    value="known",
                    source="ui",
                    created_at=NOW + timedelta(minutes=index),
                )
            )
        session.commit()


def count_marks(database: Path) -> int:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return connection.execute("SELECT count(*) FROM mark").fetchone()[0]
    finally:
        connection.close()


def fake_snapshot(destination: Path, template: Path, stamp: str, revision: str = "0001") -> Path:
    """A real, valid snapshot file with a chosen timestamp in its name."""
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"examkb-{stamp}-r{revision}.db"
    shutil.copyfile(template, path)
    return path


def cli(*arguments: str, database: Path, backups: Path) -> subprocess.CompletedProcess:
    environment = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{database}",
        "EXAMKB_BACKUP_DIR": str(backups),
    }
    return subprocess.run(
        [sys.executable, "-m", "examkb.cli", *arguments],
        cwd=REPO_ROOT, env=environment, capture_output=True, text=True,
    )


# ------------------------------------------------------------------------ the snapshot


def test_a_snapshot_catches_what_is_still_in_the_wal(tmp_db: sa.Engine, tmp_path: Path) -> None:
    """The reason this module does not use `cp`, demonstrated rather than asserted."""
    write_marks(tmp_db, 5)
    naive = tmp_path / "naive-copy.db"
    shutil.copyfile(path_of(tmp_db), naive)

    result = take_backup(url=url_for(tmp_db), destination=tmp_path / "backups")

    assert result.snapshot is not None
    assert count_marks(result.snapshot.path) == 5
    assert verify_database(result.snapshot.path) is None
    # The same five marks, through the copy anyone would have reached for first.
    assert count_marks(naive) == 0


def test_the_snapshot_is_named_for_when_it_was_taken_and_what_it_was_at(
    tmp_db: sa.Engine, tmp_path: Path
) -> None:
    result = take_backup(url=url_for(tmp_db), destination=tmp_path / "backups", label="pre-upgrade")

    snapshot = result.snapshot
    assert snapshot is not None
    assert snapshot.revision == "0001"
    assert snapshot.label == "pre-upgrade"
    assert snapshot.name.startswith("examkb-")
    assert (datetime.now(timezone.utc) - snapshot.taken_at) < timedelta(minutes=5)
    assert list_snapshots(tmp_path / "backups") == [snapshot]


def test_an_unverifiable_snapshot_is_discarded_not_kept(
    tmp_db: sa.Engine, tmp_path: Path, monkeypatch
) -> None:
    """The failure mode that matters: something that *looks* like a backup."""
    destination = tmp_path / "backups"
    monkeypatch.setattr(backup_module, "verify_database", lambda path: "simulated corruption")

    with pytest.raises(BackupError, match="did not verify and was discarded"):
        take_backup(url=url_for(tmp_db), destination=destination)

    assert list(destination.glob("*")) == []


def test_a_corrupt_database_cannot_be_backed_up(tmp_db: sa.Engine, tmp_path: Path) -> None:
    write_marks(tmp_db, 3)
    tmp_db.dispose()  # checkpoint, so the corruption below lands in the real pages
    with open(path_of(tmp_db), "r+b") as database:
        database.seek(4096)
        database.write(b"\x00not a page" * 64)

    with pytest.raises(BackupError, match="VACUUM INTO failed"):
        take_backup(url=url_for(tmp_db), destination=tmp_path / "backups")

    assert list((tmp_path / "backups").glob("*")) == []


def test_there_is_nothing_to_back_up_before_the_first_migration(tmp_path: Path) -> None:
    """A gate that blocked the first `db upgrade` would block only the safe case."""
    result = take_backup(
        url=f"sqlite:///{tmp_path / 'not-yet.db'}", destination=tmp_path / "backups"
    )

    assert result.snapshot is None
    assert "nothing to back up" in result.summary()


def test_a_non_sqlite_url_says_so_rather_than_guessing(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="SQLite files only"):
        take_backup(url="postgresql://localhost/examkb", destination=tmp_path)


# ----------------------------------------------------------------------- the destination


def test_the_destination_is_created_if_it_is_missing(tmp_db: sa.Engine, tmp_path: Path) -> None:
    destination = tmp_path / "off" / "box" / "backups"
    assert not destination.exists()

    result = take_backup(url=url_for(tmp_db), destination=destination)

    assert destination.is_dir()
    assert result.snapshot is not None and result.snapshot.path.parent == destination


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_an_unwritable_destination_fails_loudly(tmp_db: sa.Engine, tmp_path: Path) -> None:
    """Silently skipping the backup is the one behaviour this must never have."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(BackupError, match="backup directory"):
            take_backup(url=url_for(tmp_db), destination=locked / "backups")
    finally:
        locked.chmod(0o700)


# -------------------------------------------------------------------------- retention


def test_retention_keeps_exactly_n_and_never_the_newest(
    tmp_db: sa.Engine, tmp_path: Path
) -> None:
    destination = tmp_path / "backups"
    template = take_backup(url=url_for(tmp_db), destination=destination).snapshot.path
    stamps = [f"2026090{day}T120000Z" for day in range(1, 6)]
    for stamp in stamps:
        fake_snapshot(destination, template, stamp)
    template.unlink()

    pruned = prune(destination, keep=2)

    surviving = [snapshot.name for snapshot in list_snapshots(destination)]
    assert len(surviving) == 2
    assert surviving[0] == f"examkb-{stamps[-1]}-r0001.db"  # the newest, always kept
    assert len(pruned) == 3
    assert all(not path.exists() for path in pruned)


def test_keep_is_never_low_enough_to_delete_everything(
    tmp_db: sa.Engine, tmp_path: Path
) -> None:
    destination = tmp_path / "backups"
    take_backup(url=url_for(tmp_db), destination=destination)

    assert prune(destination, keep=0) == []
    assert len(list_snapshots(destination)) == 1


def test_a_backup_prunes_after_it_has_written_the_new_one(
    tmp_db: sa.Engine, tmp_path: Path
) -> None:
    destination = tmp_path / "backups"
    first = take_backup(url=url_for(tmp_db), destination=destination).snapshot.path
    fake_snapshot(destination, first, "20260901T120000Z")
    fake_snapshot(destination, first, "20260902T120000Z")

    result = take_backup(url=url_for(tmp_db), destination=destination, keep=2)

    names = {snapshot.name for snapshot in list_snapshots(destination)}
    assert result.snapshot.name in names
    assert len(names) == 2
    assert len(result.pruned) == 2


# ---------------------------------------------------------------------------- restore


def test_restore_refuses_without_explicit_confirmation(tmp_db: sa.Engine, tmp_path: Path) -> None:
    snapshot = take_backup(url=url_for(tmp_db), destination=tmp_path / "backups").snapshot

    with pytest.raises(BackupError, match="Re-run with --yes"):
        restore(snapshot.path, url=url_for(tmp_db), confirmed=False)


def test_restore_brings_the_journal_back_and_keeps_what_it_replaced(
    tmp_db: sa.Engine, tmp_path: Path
) -> None:
    destination = tmp_path / "backups"
    write_marks(tmp_db, 4)
    snapshot = take_backup(url=url_for(tmp_db), destination=destination).snapshot

    with Session(tmp_db) as session:
        session.execute(sa.text("DELETE FROM mark"))
        session.commit()
    tmp_db.dispose()
    assert count_marks(path_of(tmp_db)) == 0

    result = restore(snapshot.path, url=url_for(tmp_db), confirmed=True, destination=destination)

    assert count_marks(path_of(tmp_db)) == 4
    # The journal that was overwritten is still on disk, labelled for what it was.
    assert result.previous.snapshot is not None
    assert result.previous.snapshot.label == "pre-restore"
    assert count_marks(result.previous.snapshot.path) == 0


def test_restore_refuses_a_snapshot_that_does_not_verify(
    tmp_db: sa.Engine, tmp_path: Path
) -> None:
    broken = tmp_path / "examkb-20260901T120000Z-r0001.db"
    broken.write_bytes(b"SQLite format 3\x00" + b"rubbish" * 200)

    with pytest.raises(BackupError, match="refusing to restore"):
        restore(broken, url=url_for(tmp_db), confirmed=True, destination=tmp_path / "backups")


def test_restoring_clears_the_old_wal_beside_the_database(
    tmp_db: sa.Engine, tmp_path: Path
) -> None:
    """A stale `-wal` next to a restored file is how a good restore goes bad."""
    destination = tmp_path / "backups"
    write_marks(tmp_db, 2)
    snapshot = take_backup(url=url_for(tmp_db), destination=destination).snapshot
    write_marks(tmp_db, 3)
    assert Path(f"{path_of(tmp_db)}-wal").exists()

    restore(snapshot.path, url=url_for(tmp_db), confirmed=True, destination=destination)

    assert not Path(f"{path_of(tmp_db)}-wal").exists()
    assert count_marks(path_of(tmp_db)) == 2


# --------------------------------------------------------------------------- the gate


def test_the_first_upgrade_has_nothing_to_back_up_and_proceeds(tmp_path: Path) -> None:
    database = tmp_path / "examkb.db"
    backups = tmp_path / "backups"

    result = cli("db", "upgrade", database=database, backups=backups)

    assert result.returncode == 0, result.stderr
    assert "nothing to back up" in result.stdout
    assert list_snapshots(backups) == []
    assert database.exists()


def test_a_second_upgrade_snapshots_the_database_first(tmp_path: Path) -> None:
    database = tmp_path / "examkb.db"
    backups = tmp_path / "backups"
    cli("db", "upgrade", database=database, backups=backups)

    result = cli("db", "upgrade", database=database, backups=backups)

    assert result.returncode == 0, result.stderr
    snapshots = list_snapshots(backups)
    assert len(snapshots) == 1
    assert snapshots[0].label == "pre-upgrade"
    assert verify_database(snapshots[0].path) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_a_failed_gate_stops_the_migration_before_it_runs(tmp_path: Path) -> None:
    """The ordering claim: the database is still where it was afterwards."""
    database = tmp_path / "examkb.db"
    backups = tmp_path / "backups"
    assert cli("db", "upgrade", "--no-backup", database=database, backups=backups).returncode == 0

    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        result = cli(
            "db", "downgrade", "base", "--backup-to", str(locked / "nope"),
            database=database, backups=backups,
        )
    finally:
        locked.chmod(0o700)

    assert result.returncode == 1
    assert "refusing to migrate" in result.stderr
    current = cli("db", "current", database=database, backups=backups)
    assert "0001" in current.stdout
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0] == 37
    finally:
        connection.close()


def test_skipping_the_gate_is_possible_but_never_silent(tmp_path: Path) -> None:
    database = tmp_path / "examkb.db"
    backups = tmp_path / "backups"

    result = cli("db", "upgrade", "--no-backup", database=database, backups=backups)

    assert result.returncode == 0
    assert "--no-backup given" in result.stderr
    assert list_snapshots(backups) == []


def test_the_backup_command_lists_what_it_has(tmp_path: Path) -> None:
    database = tmp_path / "examkb.db"
    backups = tmp_path / "backups"
    cli("db", "upgrade", "--no-backup", database=database, backups=backups)

    taken = cli("backup", database=database, backups=backups)
    listed = cli("backup", "--list", database=database, backups=backups)

    assert taken.returncode == 0, taken.stderr
    assert "verified in" in taken.stdout
    assert listed.returncode == 0
    assert "1 snapshot(s)" in listed.stdout
    assert "revision 0001" in listed.stdout


# ------------------------------------------------------------ confirmation and defaults


def test_the_prompt_only_accepts_the_databases_own_name(monkeypatch, tmp_path: Path) -> None:
    """Typing "yes" is a reflex; typing `examkb.db` is a decision."""
    from examkb.commands import backup as backup_command

    target = tmp_path / "examkb.db"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    monkeypatch.setattr("builtins.input", lambda *_: "yes")
    assert backup_command._confirm(tmp_path / "snap.db", target) is False

    monkeypatch.setattr("builtins.input", lambda *_: "examkb.db")
    assert backup_command._confirm(tmp_path / "snap.db", target) is True


def test_there_is_no_prompt_to_answer_off_a_terminal(monkeypatch, tmp_path: Path) -> None:
    from examkb.commands import backup as backup_command

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert backup_command._confirm(tmp_path / "snap.db", tmp_path / "examkb.db") is False


def test_the_destination_defaults_off_box_and_is_overridable(monkeypatch) -> None:
    """A WSL disk loss takes `~` with it, and the journal is the unregenerable thing."""
    from examkb.settings import _default_backup_dir, get_settings

    repo_root = Path("/repo")
    monkeypatch.setattr("examkb.settings._WINDOWS_MOUNT", Path("/mnt/c"))
    assert _default_backup_dir(repo_root) == Path("/mnt/c/exam-kb-backups")

    monkeypatch.setattr("examkb.settings._WINDOWS_MOUNT", Path("/no/such/mount"))
    assert _default_backup_dir(repo_root) == repo_root / "var" / "backups"

    monkeypatch.setenv("EXAMKB_BACKUP_DIR", "/tmp/elsewhere")
    monkeypatch.setenv("EXAMKB_BACKUP_KEEP", "3")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.backup_dir == Path("/tmp/elsewhere")
        assert settings.backup_keep == 3
    finally:
        get_settings.cache_clear()
