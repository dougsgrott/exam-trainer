"""Resolved paths and the one database URL.

Two rules this module exists to enforce:

1. The repo root is derived from where this package sits on disk, never from the
   current working directory, so `examkb` behaves the same from any directory.
2. Nothing else in the codebase constructs a database URL. `DATABASE_URL` is the
   single override, which is what makes Postgres a one-line change.

No feature flags live here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# examkb/settings.py -> examkb/ -> <repo root>
REPO_ROOT = Path(__file__).resolve().parent.parent

_WINDOWS_MOUNT = Path("/mnt/c")

# Ten snapshots of a database this size is a few tens of MB, which is nothing, and
# ten is enough history to notice "the thing I want back was two migrations ago".
DEFAULT_BACKUP_KEEP = 10


def _default_backup_dir(repo_root: Path) -> Path:
    """Off-box under /mnt/c when WSL exposes it, otherwise inside the repo.

    A WSL disk loss takes the journal with it, and the journal is the one
    unregenerable thing in this repo -- so the default destination is deliberately
    on the Windows side when there is one.
    """
    if _WINDOWS_MOUNT.is_dir():
        return _WINDOWS_MOUNT / "exam-kb-backups"
    return repo_root / "var" / "backups"


@dataclass(frozen=True)
class Settings:
    """Everything path- or connection-shaped, resolved once."""

    repo_root: Path
    kb_dir: Path
    data_dir: Path
    tools_dir: Path
    database_url: str
    backup_dir: Path
    backup_keep: int
    """How many snapshots survive a prune. The newest is never one of the losses."""

    @property
    def database_path(self) -> Path | None:
        """Filesystem path of the database, or None when it is not SQLite."""
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            return None
        return Path(self.database_url[len(prefix) :])


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings, read from the environment exactly once."""
    repo_root = REPO_ROOT
    default_db = repo_root / "examkb.db"
    backup_dir = os.environ.get("EXAMKB_BACKUP_DIR")
    keep = os.environ.get("EXAMKB_BACKUP_KEEP")
    return Settings(
        repo_root=repo_root,
        kb_dir=repo_root / "kb",
        data_dir=repo_root / "data",
        tools_dir=repo_root / "tools",
        database_url=os.environ.get("DATABASE_URL") or f"sqlite:///{default_db}",
        backup_dir=Path(backup_dir) if backup_dir else _default_backup_dir(repo_root),
        backup_keep=max(1, int(keep)) if keep else DEFAULT_BACKUP_KEEP,
    )
