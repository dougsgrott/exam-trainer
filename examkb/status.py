"""Is the projection usable, and is it current?

One question with five possible answers, asked on every page load (008's banner),
by `doctor` (047), and by anything that needs to refuse politely instead of raising
`no such table: question`:

    no corpus -> no database -> not migrated -> nothing ingested -> stale -> current

It lives here rather than under `examkb/web/` for a reason the layering rule found:
telling "not migrated" apart from "broken" means catching `OperationalError`, and
no module under `examkb/web/` may import SQLAlchemy. Pushing the tolerance down one
layer left the web side with a plain dataclass to render, which is what the rule was
asking for in the first place.

Reading the corpus fingerprint is cheap and cached against `kb/shards.json`'s stat,
because a banner that re-hashed 3 MB of JSONL on every request would be a banner
somebody eventually deletes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from examkb import db, ingest, queries
from examkb.queries import CorpusCounts
from examkb.settings import get_settings

# Ordered worst-first. Everything before READY means there is nothing to show yet.
NO_CORPUS = "no-corpus"
NO_DATABASE = "no-database"
NOT_MIGRATED = "not-migrated"
NOT_INGESTED = "not-ingested"
STALE = "stale"
READY = "ready"


@dataclass(frozen=True)
class ProjectionStatus:
    """What the database holds, and whether it still matches `kb/`."""

    state: str
    headline: str
    detail: str
    fix: str
    """The command that moves this state forward. Empty when there is nothing to do."""

    projection_fingerprint: str | None = None
    corpus_fingerprint: str | None = None
    counts: CorpusCounts | None = None

    @property
    def ready(self) -> bool:
        return self.state == READY

    @property
    def usable(self) -> bool:
        """True when there are rows to show, current or not."""
        return self.state in (READY, STALE)

    @property
    def short(self) -> str:
        """The fingerprint prefix a person can compare by eye, as ingest prints it."""
        return (self.projection_fingerprint or "")[:12]


def _stat_key(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_size, info.st_mtime_ns)


_CORPUS_CACHE: dict[Path, tuple[tuple | None, str | None]] = {}


def _cache_key(kb: Path) -> tuple:
    """What has to change before the fingerprint is worth recomputing.

    The shard index, and every blueprint file. 018 is what made the second half
    necessary: before it there were no blueprints, so hashing shards alone gave
    the same answer as hashing the whole corpus and the omission was invisible.
    Blueprint files are a handful of small JSON documents and this runs on a page
    load, so stat-ing each one is cheaper than re-reading them.
    """
    return (
        _stat_key(kb / "shards.json"),
        tuple((str(path), _stat_key(path)) for path in ingest.blueprint_files(kb)),
    )


def corpus_fingerprint(kb: Path) -> str | None:
    """`kb_fingerprint(kb)`, recomputed only when `kb/` changes on disk.

    None when there is no corpus at all -- an empty directory hashes to something,
    and "the projection does not match the empty corpus you do not have" is not a
    thing to put in front of a person.

    **This must hash exactly what ingest stores, and it does it by calling the same
    function.** It used to hash `shard_rows` while ingest stored `corpus_rows`;
    the two agreed only while `kb/blueprints/` was empty, and the day a blueprint
    landed every page in the app would have said "older than kb/" forever, with an
    `examkb ingest` that wrote nothing to contradict it.
    """
    kb = Path(kb)
    key = _cache_key(kb)
    cached = _CORPUS_CACHE.get(kb)
    if cached is not None and cached[0] == key:
        return cached[1]

    if not kb.is_dir():
        value = None
    else:
        # `corpus_rows`, and not its two halves re-composed here: that composition
        # existing in two places is the bug this replaced.
        rows = ingest.corpus_rows(kb)
        value = ingest.fingerprint(rows) if rows else None

    _CORPUS_CACHE[kb] = (key, value)
    return value


def forget_corpus_fingerprint() -> None:
    """Drop the cache. For tests, and for anything that just rebuilt `kb/`."""
    _CORPUS_CACHE.clear()


def projection_status(*, url: str | None = None, kb: Path | None = None) -> ProjectionStatus:
    """The one call the banner, the home page and `doctor` all make."""
    settings = get_settings()
    url = url or settings.database_url
    kb = Path(kb) if kb is not None else settings.kb_dir

    corpus = corpus_fingerprint(kb)
    if corpus is None:
        return ProjectionStatus(
            state=NO_CORPUS,
            headline="No corpus.",
            detail=f"Nothing to project: {kb} has no question shards.",
            fix="make pipeline",
        )

    path = db.database_path(url)
    if path is not None and not path.exists():
        return ProjectionStatus(
            state=NO_DATABASE,
            headline="No database yet.",
            detail="The projection has not been created.",
            fix="examkb db upgrade && examkb ingest",
            corpus_fingerprint=corpus,
        )

    try:
        with Session(db.engine_for(url)) as session:
            projected = ingest.projection_fingerprint(session)
            counts = queries.corpus_counts(session) if projected is not None else None
    except OperationalError as error:
        # The schema is not there. Anything else is a real fault and propagates.
        if "no such table" not in str(error).lower():
            raise
        return ProjectionStatus(
            state=NOT_MIGRATED,
            headline="The database has no schema.",
            detail="Migrations have not been applied to this database.",
            fix="examkb db upgrade",
            corpus_fingerprint=corpus,
        )
    except SQLAlchemyError as error:  # pragma: no cover -- a damaged file, not a state
        return ProjectionStatus(
            state=NO_DATABASE,
            headline="The database could not be opened.",
            detail=str(error),
            fix="examkb restore --from <snapshot>",
            corpus_fingerprint=corpus,
        )

    if projected is None:
        return ProjectionStatus(
            state=NOT_INGESTED,
            headline="Nothing ingested yet.",
            detail="The schema is there; the projection is empty.",
            fix="examkb ingest",
            corpus_fingerprint=corpus,
        )

    if projected != corpus:
        return ProjectionStatus(
            state=STALE,
            headline="This projection is older than kb/.",
            detail=(
                f"Showing corpus {projected[:12]}; kb/ is now {corpus[:12]}. "
                "Pages below may not match the files on disk."
            ),
            fix="examkb ingest",
            projection_fingerprint=projected,
            corpus_fingerprint=corpus,
            counts=counts,
        )

    return ProjectionStatus(
        state=READY,
        headline="Projection is current.",
        detail=f"corpus {projected[:12]}",
        fix="",
        projection_fingerprint=projected,
        corpus_fingerprint=corpus,
        counts=counts,
    )
