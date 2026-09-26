"""The light/dark toggle, and the one preference it shares with the reports.

Most of this shipped in 008 without a switch attached: `app.css` already carried
light tokens, a `prefers-color-scheme` block guarded by
`:root:not([data-theme="light"])`, and an explicit `:root[data-theme="dark"]`. What
050 adds is the switch and somewhere to remember it — and the key it remembers it
under is `tools/build_report.py`'s, so the app and the static reports move together.

The part worth testing hardest is the one nobody sees when it works: the stored
theme has to be on `<html>` *before the stylesheet is applied*, or every navigation
flashes the wrong colours. That is a fact about where the script sits in the
document, so it is asserted about the document.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from examkb import db as db_module
from examkb import status as status_module
from examkb.web.app import create_app

@pytest.fixture(autouse=True)
def clean_caches():
    db_module.engine_for.cache_clear()
    db_module.forget_projection_ready()
    yield
    db_module.engine_for.cache_clear()
    db_module.forget_projection_ready()


REPO_ROOT = Path(__file__).resolve().parent.parent
CSS = REPO_ROOT / "examkb" / "web" / "static" / "app.css"
BASE = REPO_ROOT / "examkb" / "web" / "templates" / "base.html"
STORAGE_KEY = "kb-theme"


@pytest.fixture
def client(tmp_db) -> TestClient:
    """Any page will do; the toggle lives in `base.html`, so it is on all of them.

    A migrated-but-empty database, because the subject here is the masthead and
    not what the pages do without a schema -- 051 owns that.
    """
    url = str(tmp_db.url)
    app = create_app(
        database_url=url, status_provider=lambda: status_module.projection_status(url=url)
    )
    return TestClient(app)


# ------------------------------------------------------------------ one preference


def test_the_app_and_the_reports_use_the_same_key() -> None:
    """The criterion, read off `build_report.py` rather than off a copy of the string.

    If the reports ever rename their key this fails here, which is the only place
    that would notice — the two surfaces are otherwise unaware of each other.
    """
    reports = (REPO_ROOT / "tools" / "build_report.py").read_text(encoding="utf-8")
    keys = set(re.findall(r"localStorage\.(?:get|set)Item\('([^']+)'", reports))

    assert STORAGE_KEY in keys, f"build_report.py no longer stores a theme under {STORAGE_KEY!r}"
    assert STORAGE_KEY in BASE.read_text(encoding="utf-8")


def test_the_app_stores_nothing_else(client: TestClient) -> None:
    """One key. A second would be a second preference nobody asked for."""
    head = client.get("/").text.split("</head>")[0]
    keys = set(re.findall(r"localStorage\.(?:get|set)Item\('([^']+)'", head))

    assert keys == {STORAGE_KEY}


# --------------------------------------------------------------------- no flash


def test_the_theme_is_applied_before_the_stylesheet(client: TestClient) -> None:
    """The criterion. Loaded from a file it would arrive after first paint."""
    body = client.get("/").text
    head = body[: body.index("</head>")]

    assert head.index(STORAGE_KEY) < head.index("app.css"), "the script runs too late"
    assert "<script>" in head, "it is inline; an external file cannot be early enough"
    assert 'src="/static/theme' not in body, "a separate file would flash"


def test_the_script_sets_the_attribute_synchronously(client: TestClient) -> None:
    head = client.get("/").text.split("</head>")[0]
    script = head.split("<script>")[1].split("</script>")[0]

    # The attribute is set at parse time; only the button's label waits for the DOM.
    before_listener = script.split("DOMContentLoaded")[0]
    assert "setAttribute('data-theme'" in before_listener
    assert "defer" not in head.split("app.css")[0]


# ---------------------------------------------------------------------- the toggle


def test_the_toggle_is_on_every_page(client: TestClient) -> None:
    for path in ("/", "/browse", "/exam", "/results"):
        body = client.get(path, follow_redirects=True).text
        assert 'id="theme-toggle"' in body, path
        assert "examkbToggleTheme()" in body, path


def test_the_toggle_is_a_real_button_with_a_label(client: TestClient) -> None:
    body = client.get("/").text
    button = re.search(r"<button[^>]*id=\"theme-toggle\"[^>]*>", body).group(0)

    assert 'type="button"' in button
    assert "aria-label=" in button


def test_only_the_two_known_values_are_ever_honoured(client: TestClient) -> None:
    """Anything else in storage is ignored rather than written onto the document."""
    script = client.get("/").text.split("<script>")[1].split("</script>")[0]

    assert "stored === 'dark' || stored === 'light'" in script


def test_storage_failures_do_not_break_the_page(client: TestClient) -> None:
    """Private browsing throws on `localStorage`; the page must still render."""
    script = client.get("/").text.split("<script>")[1].split("</script>")[0]

    assert script.count("try {") >= 2 and script.count("catch (e) {}") >= 2


# ------------------------------------------------------------------------ the CSS


def test_the_palette_covers_all_three_states() -> None:
    """Follow the system, forced light, forced dark. 008 built it this way."""
    css = CSS.read_text(encoding="utf-8")

    assert "@media (prefers-color-scheme: dark)" in css
    assert ':root:not([data-theme="light"])' in css, "forced light must beat the OS"
    assert ':root[data-theme="dark"]' in css, "forced dark must work in a light OS"


def test_every_colour_the_toggle_changes_is_a_token() -> None:
    """A literal colour outside the token blocks would not follow the theme."""
    css = CSS.read_text(encoding="utf-8")
    after_tokens = css.split(':root[data-theme="dark"]', 1)[1]
    body = after_tokens.split("}", 1)[1]

    literals = [
        match
        for match in re.findall(r"#[0-9a-fA-F]{3,8}\b", body)
        # The one exception is documented where it is written.
        if "--seq-1" not in body[max(0, body.index(match) - 40) : body.index(match)]
    ]
    assert not literals, f"literal colours outside the palette: {literals}"


def test_the_charts_follow_the_theme_because_they_use_tokens() -> None:
    """`/results`' SVG needs no second render; that is why it was worth reusing."""
    from examkb.web.charts import hbar

    chart = str(hbar([("A", 1.0)], label="x"))

    assert "var(--series-1)" in chart
    assert not re.search(r"#[0-9a-fA-F]{3,6}", chart)


def test_the_toggle_costs_no_request(client: TestClient) -> None:
    """The criterion: nothing is fetched to make any of this work."""
    body = client.get("/").text
    resources = re.findall(r'(?:<img[^>]+src|<script[^>]+src|<link[^>]+href)="([^"]+)"', body)

    for resource in resources:
        assert resource.startswith("/static/") or resource.startswith("data:"), resource
    assert "theme" not in " ".join(resources)
