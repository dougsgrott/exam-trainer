"""Helpers shared by the `tools/` scripts and the `examkb` app.

**Standard library only.** The scripts in this directory are PEP 723 standalone
programs that must keep running under a plain `python3` with nothing installed, so
anything imported here has to come with the interpreter.

Import it as `from _shared import ...`, never `from tools._shared import ...`:
under `uv run tools/X.py` the repo root is not on `sys.path` but `tools/` is
`sys.path[0]`, so the package-qualified form raises ModuleNotFoundError. The app
reaches the same module through `examkb/compat.py`, which appends `<repo>/tools`
to `sys.path` -- both sides then share one `sys.modules["_shared"]`.
"""

from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path

__all__ = [
    "KBNotFound",
    "QUESTIONS_DIR",
    "QUESTIONS_FILE",
    "SHARDS_FILE",
    "StaleShardIndex",
    "discover_shards",
    "display_path",
    "load_questions",
    "md_to_html",
    "norm",
    "normalize_reference",
    "questions_path",
    "read_shard",
    "read_shard_index",
    "shards_path",
    "slugify",
    "strip_markdown",
]


# ------------------------------------------------------------------------ paths


def display_path(path: Path, root: Path) -> str:
    """`path` relative to `root` when it is inside it, absolute when it is not.

    Only for printing. `--kb` and `--out` accept any directory, including a
    throwaway one outside the repo, and a status line is not a reason to crash.
    """
    path = Path(path)
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


# ------------------------------------------------------------------------ text


def slugify(text: str) -> str:
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


# -------------------------------------------------------------- markdown subset
#
# The corpus uses one measured subset of Markdown -- bold, italic, inline code and
# links, with literal text backslash-escaped. These two functions are the only
# readers of it: `strip_markdown` reverses it back to plain text (how
# verify_lossless proves nothing was dropped) and `md_to_html` renders it.


def strip_markdown(text: str) -> str:
    """Reduce generated Markdown back to its literal text."""
    code: list[str] = []

    def stash(match: re.Match) -> str:
        code.append(match.group(1))
        return f"\x00{len(code) - 1}\x00"

    # Code spans are protected first so the unescaping step below cannot touch their
    # literal content (which the converter deliberately leaves unescaped).
    text = re.sub(r"`([^`]*)`", stash, text)
    text = re.sub(r"\[([^\]]*)\]\((?:https?|mailto)[^)]*\)", r"\1", text)  # links
    text = text.replace("**", "")  # bold
    text = re.sub(r"(?<![\w\\])\*([^*]+?)(?<!\\)\*(?!\w)", r"\1", text)  # italics
    text = re.sub(r"\\(.)", r"\1", text)  # backslash escapes
    text = re.sub(r"\x00(\d+)\x00", lambda m: code[int(m.group(1))], text)
    return norm(text)


def md_to_html(text: str) -> str:
    """Render the KB's Markdown subset (bold, italic, code, links, escapes) to HTML."""
    if not text:
        return ""
    blocks = []
    for block in re.split(r"\n{2,}", text):
        code_spans: list[str] = []

        def stash(match: re.Match) -> str:
            code_spans.append(match.group(1))
            return f"\x00{len(code_spans) - 1}\x00"

        block = re.sub(r"`([^`]*)`", stash, block)
        block = escape(block, quote=False)
        block = re.sub(
            r"\[([^\]]*)\]\((https?://[^)\s]+)\)",
            lambda m: f'<a href="{escape(m.group(2), quote=True)}" '
            f'target="_blank" rel="noopener">{m.group(1)}</a>',
            block,
        )
        block = re.sub(r"(?<!\\)\*\*(.+?)(?<!\\)\*\*", r"<strong>\1</strong>", block, flags=re.S)
        block = re.sub(r"(?<![\w\\])\*(.+?)(?<!\\)\*(?!\w)", r"<em>\1</em>", block, flags=re.S)
        block = re.sub(r"\\(.)", r"\1", block)
        block = re.sub(
            r"\x00(\d+)\x00",
            lambda m: f"<code>{escape(code_spans[int(m.group(1))], quote=False)}</code>",
            block,
        )
        blocks.append(f"<p>{block.strip()}</p>")
    return "".join(blocks)


# ------------------------------------------------------------------ references

# Hosts that mirror the same documentation. Order matters: the docs sites put the
# locale segment in two different places (docs.anthropic.com/en/docs/X versus
# platform.claude.com/docs/en/X), and a naive prefix strip leaves a bogus "en" bucket.
_DOC_HOSTS = ("docs.anthropic.com", "platform.claude.com", "docs.claude.com")
_SUPPORT_HOSTS = ("support.anthropic.com", "support.claude.com")


def normalize_reference(url: str) -> str:
    """Collapse mirror hosts so the same page counts once."""
    trimmed = re.sub(r"^https?://", "", url).rstrip("/")
    host, _, path = trimmed.partition("/")

    if host in _DOC_HOSTS:
        # Strip any leading run of "en/" and "docs/" segments, in either order.
        segments = path.split("/")
        while segments and segments[0] in ("en", "docs"):
            segments.pop(0)
        return "docs/" + "/".join(segments)
    if host in _SUPPORT_HOSTS:
        return "support/" + re.sub(r"^en/articles/", "", path)
    return f"{host}/{path}" if path else host


# ---------------------------------------------------------------- loading kb/
#
# The corpus is a set of shards, not one file. `parse_udemy.py` owns
# `questions.jsonl` and rewrites it wholesale on every run; every other producer
# writes its own file under `kb/questions/`, where no parser can touch it. The
# index of them all, `kb/shards.json`, is written by `tools/build_shards.py` and by
# nothing else -- a parser that owned the index could erase the other producers'
# work just by running.

QUESTIONS_FILE = "questions.jsonl"
QUESTIONS_DIR = "questions"
SHARDS_FILE = "shards.json"


class KBNotFound(FileNotFoundError):
    """The corpus is not on disk. Carries the path that was looked for."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"missing {path}")
        self.path = path


class StaleShardIndex(RuntimeError):
    """`shards.json` does not list a shard that is on disk.

    Raised rather than silently skipping the file: a shard missing from the index
    is exactly the failure this layout exists to prevent.
    """

    def __init__(self, kb: Path, missing: list[Path]) -> None:
        names = ", ".join(sorted(str(p.relative_to(kb)) for p in missing))
        super().__init__(
            f"{kb / SHARDS_FILE} does not list {names}; run tools/build_shards.py"
        )
        self.kb = Path(kb)
        self.missing = missing


def questions_path(kb: Path) -> Path:
    return Path(kb) / QUESTIONS_FILE


def shards_path(kb: Path) -> Path:
    return Path(kb) / SHARDS_FILE


def discover_shards(kb: Path) -> list[Path]:
    """Every question shard on disk, in a deterministic order.

    `questions.jsonl` first because it is the original and the one a parser owns,
    then `questions/*.jsonl` sorted by name. Other `.jsonl` files in `kb/` --
    `annotations.jsonl`, for one -- are not questions and are not shards.
    """
    kb = Path(kb)
    found = []
    if questions_path(kb).exists():
        found.append(questions_path(kb))
    found.extend(sorted((kb / QUESTIONS_DIR).glob("*.jsonl")))
    return found


def read_shard_index(kb: Path) -> list[dict] | None:
    """The `shards.json` entries, or None when there is no index yet."""
    path = shards_path(kb)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["shards"]


def read_shard(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def load_questions(kb: Path, *, provider: str | None = None) -> list[dict]:
    """Every question in the corpus, shard by shard, in index order.

    The one place that knows how the corpus is laid out on disk, so a layout change
    is one function rather than four scripts. With no `shards.json` this reads the
    shards it finds, which for a corpus that is only `questions.jsonl` is exactly
    what every consumer did before the index existed.

    `provider` filters on each record's own `source.provider` rather than on the
    index, so the filter holds even if the index is wrong.
    """
    kb = Path(kb)
    found = discover_shards(kb)
    index = read_shard_index(kb)

    if index is None:
        paths = found
    else:
        paths = [kb / entry["path"] for entry in index]
        unlisted = [p for p in found if p not in paths]
        if unlisted:
            raise StaleShardIndex(kb, unlisted)

    if not paths:
        raise KBNotFound(questions_path(kb))

    questions: list[dict] = []
    for path in paths:
        if not path.exists():
            raise KBNotFound(path)
        questions.extend(read_shard(path))
    if provider is not None:
        questions = [q for q in questions if q["source"]["provider"] == provider]
    return questions
