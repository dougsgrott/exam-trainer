"""Nothing this app serves comes from the network, and nothing it serves changed.

Two different promises, both easy to break by accident:

1. **Offline.** The reports this app replaces open from a USB stick with no
   network; the app keeps that rule. One `<script src="https://...">` added in a
   hurry turns a working page into a page that hangs on a plane, and it is exactly
   the kind of line that survives review because it looks normal.
2. **Unchanged.** A vendored file is a copy of somebody else's code sitting in this
   repo. `VENDOR.json` records where each one came from and what it hashed to, so
   an upgrade is a diff somebody chose rather than 52 KB that moved quietly.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from conftest import REPO_ROOT

from examkb.web.app import STATIC_DIR, VENDOR_MANIFEST, check_assets, vendored_assets

TEMPLATES = REPO_ROOT / "examkb" / "web" / "templates"

# Anything that would make the browser open a connection off this machine. `//` is
# in the list because a protocol-relative URL is the one people forget.
EXTERNAL = re.compile(r"""(?:src|href|action|content)\s*=\s*["'](?:https?:)?//""", re.IGNORECASE)


def test_the_manifest_lists_every_third_party_file() -> None:
    """A file in static/ that nobody vendored is a file nobody can vouch for."""
    recorded = {asset["file"] for asset in vendored_assets()}
    ours = {"app.css", "VENDOR.json"}
    on_disk = {path.name for path in STATIC_DIR.iterdir() if path.is_file()}
    unaccounted = on_disk - recorded - ours
    assert not unaccounted, f"static files with no provenance: {sorted(unaccounted)}"


@pytest.mark.parametrize("asset", vendored_assets(), ids=lambda a: a["file"])
def test_a_vendored_file_still_hashes_to_what_was_recorded(asset: dict) -> None:
    path = STATIC_DIR / asset["file"]
    raw = path.read_bytes()
    assert len(raw) == asset["bytes"]
    assert hashlib.sha256(raw).hexdigest() == asset["sha256"], (
        f"{asset['file']} is not the {asset['name']} {asset['version']} that was "
        f"vendored. If this was an upgrade, update VENDOR.json in the same commit."
    )


@pytest.mark.parametrize("asset", vendored_assets(), ids=lambda a: a["file"])
def test_every_vendored_file_records_where_it_came_from(asset: dict) -> None:
    assert asset["license"]
    assert len(asset["urls"]) >= 2, "two independent sources, so a bad mirror shows up"
    assert all(url.startswith("https://") for url in asset["urls"])


def test_the_app_refuses_to_start_with_an_asset_missing(monkeypatch, tmp_path: Path) -> None:
    """Silently losing HTMX looks exactly like buttons that stopped working."""
    from examkb.web import app as app_module

    monkeypatch.setattr(app_module, "STATIC_DIR", tmp_path)
    with pytest.raises(RuntimeError) as raised:
        check_assets()
    assert "htmx.min.js" in str(raised.value)


@pytest.mark.parametrize(
    "path", sorted(TEMPLATES.rglob("*.html")), ids=lambda p: str(p.relative_to(TEMPLATES))
)
def test_no_template_fetches_anything_off_this_machine(path: Path) -> None:
    # Jinja comments are stripped first: a comment cannot fetch anything, and the
    # comment in base.html explaining this rule names the very words it forbids.
    text = re.sub(r"\{#.*?#\}", " ", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    offenders = EXTERNAL.findall(text)
    assert not offenders, f"{path.name} points at an external origin"
    for word in ("cdn.", "googleapis", "unpkg", "jsdelivr", "preconnect", "dns-prefetch"):
        assert word not in text.lower(), f"{path.name} mentions {word}"


def test_the_external_origin_scan_would_catch_one() -> None:
    """Prove the regex fires, including on the protocol-relative form."""
    assert EXTERNAL.search('<script src="https://unpkg.com/htmx.org"></script>')
    assert EXTERNAL.search('<link href="//fonts.googleapis.com/css">')
    assert not EXTERNAL.search('<script src="/static/htmx.min.js"></script>')


# ---------------------------------------------------------------- the shared palette
#
# The plan claims this app "reuses tools/charts.py server-side at zero porting
# cost". That is only true while the tokens charts.py emits by name are defined by
# the page it lands on, so the claim gets a test rather than a sentence.

CHART_TOKENS = ("--grid", "--text-primary", "--text-secondary", "--text-muted",
                "--seq-ink-low", "--seq-ink-high")


@pytest.mark.parametrize("token", CHART_TOKENS)
def test_the_app_defines_the_tokens_charts_emits(token: str) -> None:
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    charts = (REPO_ROOT / "tools" / "charts.py").read_text(encoding="utf-8")
    assert f"var({token})" in charts, f"charts.py no longer uses {token}; update this list"
    assert f"{token}:" in css, f"app.css does not define {token}, which charts.py emits"


def test_the_palette_has_a_dark_mode_for_every_light_token() -> None:
    """A token defined only in light mode reads as black-on-black after sunset."""
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    blocks = css.split(":root")
    light = set(re.findall(r"(--[a-z0-9-]+):", blocks[1]))
    dark = set(re.findall(r"(--[a-z0-9-]+):", "".join(blocks[2:])))
    # `--radius`, `--font` and friends are not colours and do not change.
    invariant = {"--radius", "--font", "--mono"}
    assert (light - invariant) <= dark


def test_the_manifest_is_json_a_person_can_read() -> None:
    manifest = json.loads(VENDOR_MANIFEST.read_text(encoding="utf-8"))
    assert "_comment" in manifest, "the file should say what it is for"
    assert manifest["assets"]
