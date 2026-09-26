"""The one architectural rule the plan keeps: no SQLAlchemy in routes or templates.

The plan cut a lot of structure out of its own design and kept exactly this, for a
reason worth restating: a query written inside a route is a query with no test.
Once a page builds its own `select()`, the only way to assert the number it shows
is to render the page and parse the HTML, and the number stops being checkable
anywhere else. Keeping queries in `examkb/queries.py` and the services means
`tests/test_ingest.py` can assert "78" against a function call.

This file walks `examkb/web/` rather than listing its modules, so the rule covers
010's `browse.py` and 015's runner before they are written -- which is the only
version of this test worth having. The day it fails, the fix is to put the query in
`queries.py` and call it, not to add an exception here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from conftest import REPO_ROOT

WEB = REPO_ROOT / "examkb" / "web"

# Importing any of these from under examkb/web/ is the failure. `examkb.models` is
# on the list beside SQLAlchemy itself: a route holding an ORM object is a route
# that can lazy-load a relationship inside a template, which is the same problem
# wearing a different hat.
FORBIDDEN_ROOTS = ("sqlalchemy", "alembic", "examkb.models", "examkb.db")

TEMPLATES = WEB / "templates"

# Things that only appear in a template if somebody put a query or a live ORM
# object in one. `.query(` and `session.` are the tells; `select(` is checked as a
# call so that the word "select" in prose does not trip it.
TEMPLATE_SMELLS = ("sqlalchemy", "session.", ".query(", "select(", "__table__", "metadata.tables")


def python_modules() -> list[Path]:
    return sorted(WEB.rglob("*.py"))


def imported_names(path: Path) -> set[str]:
    """Every module named by an `import` or `from ... import` in `path`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_there_is_something_to_scan() -> None:
    """A scan over an empty tree passes for the wrong reason."""
    modules = python_modules()
    assert len(modules) >= 4
    assert WEB / "app.py" in modules


@pytest.mark.parametrize("path", python_modules(), ids=lambda p: str(p.relative_to(WEB)))
def test_no_web_module_imports_sqlalchemy(path: Path) -> None:
    offenders = sorted(
        name
        for name in imported_names(path)
        for root in FORBIDDEN_ROOTS
        if name == root or name.startswith(root + ".")
    )
    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)} imports {offenders}. "
        "Queries belong in examkb/queries.py or a service; the web layer calls them."
    )


def test_the_scan_would_catch_an_offender(tmp_path: Path) -> None:
    """Prove the scanner fires, so a passing run means something."""
    offender = tmp_path / "browse.py"
    offender.write_text("from sqlalchemy import select\nimport examkb.models\n", encoding="utf-8")
    names = imported_names(offender)
    assert any(
        name == root or name.startswith(root + ".")
        for name in names
        for root in FORBIDDEN_ROOTS
    )


@pytest.mark.parametrize(
    "path", sorted(TEMPLATES.rglob("*.html")), ids=lambda p: str(p.relative_to(TEMPLATES))
)
def test_no_template_runs_a_query(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    # Only the expression and statement blocks; prose in a comment is not code.
    import re

    code = " ".join(re.findall(r"\{[{%][^}]*[}%]\}", text))
    found = sorted(smell for smell in TEMPLATE_SMELLS if smell in code)
    assert not found, f"{path.name} looks like it is querying: {found}"


# The complete set of app modules the web layer is allowed to reach for. A new name
# here should be a deliberate edit, not something that happened -- 010 added two and
# had to come here to do it, which is the test working.
#
# `examkb.compat` is the bridge to `tools/_shared.py`: pure text helpers, no
# database. `browse`, `marks`, `runner` and `results` are the session seams -- each opens the
# session and returns frozen dataclasses, so `examkb.db` stays forbidden under
# `web/`. Note `examkb.services.attempts` is *not* here: the runner reaches it, the
# web layer does not, because `attempts` hands back ORM objects.
ALLOWED_MODULES = {
    "examkb.compat",
    "examkb.queries",
    "examkb.services.browse",
    "examkb.services.marks",
    "examkb.services.results",
    "examkb.services.runner",
    "examkb.status",
    "examkb.settings",
}
# `examkb.services` is the bare package name that `from examkb.services import browse`
# also produces. Exact only: `examkb.services.search` is still not admitted.
ALLOWED_EXACT = {"examkb", "examkb.__version__", "examkb.services"}


def is_allowed(name: str) -> bool:
    """Exact matching, deliberately.

    A prefix match on `examkb` would let `examkb.models` through and this check
    would be decoration; `test_the_allowlist_rejects_the_thing_it_is_for` is what
    keeps that honest.
    """
    if name in ALLOWED_EXACT or name in ALLOWED_MODULES:
        return True
    # `from examkb.queries import CorpusCounts` lands as the module and the symbol.
    return name.rsplit(".", 1)[0] in ALLOWED_MODULES


def test_routes_reach_the_database_only_through_the_documented_seams() -> None:
    unexpected: set[str] = set()
    for path in python_modules():
        for name in imported_names(path):
            if not name.startswith("examkb") or name.startswith("examkb.web"):
                continue
            if not is_allowed(name):
                unexpected.add(name)
    assert not unexpected, f"new dependency from the web layer: {sorted(unexpected)}"


@pytest.mark.parametrize(
    "name",
    [
        "examkb.models",
        "examkb.models.corpus",
        "examkb.db",
        "examkb.ingest",
        "examkb.backup",
        # Allowing the `examkb.services` package must not admit everything under it.
        "examkb.services.search",
    ],
)
def test_the_allowlist_rejects_the_thing_it_is_for(name: str) -> None:
    assert not is_allowed(name)


@pytest.mark.parametrize(
    "name", ["examkb", "examkb.status", "examkb.queries.CorpusCounts", "examkb.__version__"]
)
def test_the_allowlist_admits_the_documented_seams(name: str) -> None:
    assert is_allowed(name)
