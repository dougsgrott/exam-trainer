"""`tools/charts.py`'s SVG, as something a template can print.

The plan picked this stack partly so the reports' charts could be reused
server-side "at zero porting cost". This module is that cost: it reaches
`tools/charts.py` through the compat bridge (002's rule), wraps the finished
strings in `Markup` so Jinja prints them instead of escaping them, and does
nothing else. `charts.py` itself is untouched.

Two things make the `Markup` safe rather than a hole. `charts.py` escapes every
value it is handed with its own `_esc`, so corpus text cannot become markup; and
the colours are CSS custom properties (`--series-1`, `--text-secondary`) that
`app.css` defines, which is why the same chart body serves light and dark without
a second render.
"""

from __future__ import annotations

from markupsafe import Markup

from examkb.compat import charts


def hbar(items: list[tuple[str, float]], *, label: str, **options) -> Markup:
    """Horizontal bars for nominal categories -- domains have no natural order."""
    return Markup(charts.hbar(items, label=label, **options))


def table(headers: list[str], rows: list[list[str]], *, caption: str = "") -> Markup:
    """The companion table every chart ships with, so no bar is the only record."""
    return Markup(charts.table(headers, rows, caption=caption))


def stat_tile(label: str, value: str, note: str = "") -> Markup:
    return Markup(charts.stat_tile(label, value, note))
