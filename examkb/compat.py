"""Import bridge to the stdlib-only helpers in `tools/`.

`tools/` is not a package -- its scripts are PEP 723 standalone programs that run
with `tools/` as `sys.path[0]`, so they import `_shared` by bare name and
`from tools import _shared` would raise ModuleNotFoundError there. Rather than make
the scripts importable as a package (which would break the standalone contract),
the app comes to them: this module puts `<repo>/tools` on `sys.path` once, so both
sides share a single `sys.modules["_shared"]` and there is one copy of each helper.

The path is *appended*, never inserted at the front: `tools/` also holds `charts`,
`build_kb`, `kb_stats` and friends, and none of those bare names should be able to
shadow a standard-library or third-party module for the app.

Every app-side use of a tool helper goes through here:

    from examkb.compat import load_questions, md_to_html
"""

from __future__ import annotations

import sys

from examkb.settings import get_settings


def ensure_tools_on_path() -> str:
    """Put `<repo>/tools` on `sys.path` if it is not already there. Idempotent."""
    tools_dir = str(get_settings().tools_dir)
    if tools_dir not in sys.path:
        sys.path.append(tools_dir)
    return tools_dir


TOOLS_DIR = ensure_tools_on_path()

import charts  # noqa: E402  -- a module, not a name: 016 renders its SVG server-side

from _shared import (  # noqa: E402  -- import needs the path above
    KBNotFound,
    QUESTIONS_FILE,
    SHARDS_FILE,
    StaleShardIndex,
    blueprint_files,
    discover_shards,
    load_blueprints,
    load_questions,
    markdown_violations,
    mask_code_spans,
    md_to_html,
    norm,
    normalize_reference,
    questions_path,
    read_shard_index,
    slugify,
    strip_markdown,
)

__all__ = [
    "KBNotFound",
    "QUESTIONS_FILE",
    "SHARDS_FILE",
    "StaleShardIndex",
    "TOOLS_DIR",
    "charts",
    "blueprint_files",
    "discover_shards",
    "ensure_tools_on_path",
    "load_blueprints",
    "load_questions",
    "markdown_violations",
    "mask_code_spans",
    "md_to_html",
    "norm",
    "normalize_reference",
    "questions_path",
    "read_shard_index",
    "slugify",
    "strip_markdown",
]
