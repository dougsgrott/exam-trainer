"""Inline-SVG chart primitives for the knowledge-base reports.

No dependencies and no runtime: every function returns a finished SVG string. Colors
are referenced as CSS custom properties (--series-1, --seq-N, --text-secondary, ...)
which the page defines for light and dark, so one chart body serves both themes.

Conventions enforced here rather than left to each call site:

* Thin marks with 4px rounded data-ends, anchored to the baseline.
* A 2px gap in the surface color between touching fills.
* Recessive axes and gridlines; no chart junk.
* Direct value labels (the palette's light-mode aqua sits below 3:1, so labels are
  relief, not decoration) with the full label text in a <title> for hover.
* Text always wears ink tokens, never the series color.
"""

from __future__ import annotations

from html import escape

# Sequential blue ramp, light -> dark. Referenced by index as var(--seq-N) so the page
# can re-step it for dark mode.
SEQUENTIAL_STEPS = 7

FONT = "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
LABEL_SIZE = 12
VALUE_SIZE = 12
TICK_SIZE = 11


def _esc(text: object) -> str:
    return escape(str(text), quote=True)


# Advance widths (per 1000 units of font size) for the common ASCII range, following
# the Helvetica metrics that the UI sans stack closely tracks. A flat average is not
# good enough: capitals, "&", and "W" are nearly twice the width of "i", so averaging
# silently overflows gutters and the browser clips the label.
_WIDTHS = {
    " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667, "'": 191,
    "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333, ".": 278, "/": 278,
    ":": 278, ";": 278, "<": 584, "=": 584, ">": 584, "?": 556, "@": 1015,
    "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778, "H": 722,
    "I": 278, "J": 500, "K": 667, "L": 556, "M": 833, "N": 722, "O": 778, "P": 667,
    "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722, "V": 667, "W": 944, "X": 667,
    "Y": 667, "Z": 611, "[": 278, "\\": 278, "]": 278, "_": 556,
    "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556, "h": 556,
    "i": 222, "j": 222, "k": 500, "l": 222, "m": 833, "n": 556, "o": 556, "p": 556,
    "q": 556, "r": 333, "s": 500, "t": 278, "u": 556, "v": 500, "w": 722, "x": 500,
    "y": 500, "z": 500, "\u00b7": 333, "\u2026": 1000, "\u2192": 800,
}
_DEFAULT_WIDTH = 556

# The stack resolves to a different face on every platform, and the ones that actually
# ship (DejaVu Sans on this Linux box, Segoe UI on Windows) measure 1.12-1.17x wider
# than the Helvetica metrics above -- verified by measuring getComputedTextLength in a
# real browser. Over-estimating costs a little gutter slack; under-estimating gets the
# label clipped, so round the worst case up.
_WIDTH_SAFETY = 1.2


def _text_width(text: str, size: int) -> float:
    raw = sum(_WIDTHS.get(ch, _DEFAULT_WIDTH) for ch in text) * size / 1000
    return raw * _WIDTH_SAFETY


def _fit(text: str, available_px: float, size: int = LABEL_SIZE) -> str:
    """Shorten a label so it cannot overflow its gutter and get clipped."""
    if _text_width(text, size) <= available_px:
        return text
    budget = available_px - _text_width("\u2026", size)
    kept = ""
    for char in text:
        if _text_width(kept + char, size) > budget:
            break
        kept += char
    return kept.rstrip() + "\u2026"


def _rounded_bar(x: float, y: float, width: float, height: float, radius: float, side: str) -> str:
    """A bar whose data-end is rounded and whose baseline end is square."""
    radius = max(0.0, min(radius, height / 2, width))
    if radius <= 0.1:
        return f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(width, 0):.1f}" height="{height:.1f}"/>'

    if side == "right":
        return (
            f'<path d="M{x:.1f},{y:.1f} H{x + width - radius:.1f} '
            f"A{radius:.1f},{radius:.1f} 0 0 1 {x + width:.1f},{y + radius:.1f} "
            f"V{y + height - radius:.1f} "
            f"A{radius:.1f},{radius:.1f} 0 0 1 {x + width - radius:.1f},{y + height:.1f} "
            f'H{x:.1f} Z"/>'
        )
    if side == "left":
        return (
            f'<path d="M{x + width:.1f},{y:.1f} H{x + radius:.1f} '
            f"A{radius:.1f},{radius:.1f} 0 0 0 {x:.1f},{y + radius:.1f} "
            f"V{y + height - radius:.1f} "
            f"A{radius:.1f},{radius:.1f} 0 0 0 {x + radius:.1f},{y + height:.1f} "
            f'H{x + width:.1f} Z"/>'
        )
    # "top": vertical column growing up from the baseline
    radius = max(0.0, min(radius, width / 2, height))
    return (
        f'<path d="M{x:.1f},{y + height:.1f} V{y + radius:.1f} '
        f"A{radius:.1f},{radius:.1f} 0 0 1 {x + radius:.1f},{y:.1f} "
        f"H{x + width - radius:.1f} "
        f"A{radius:.1f},{radius:.1f} 0 0 1 {x + width:.1f},{y + radius:.1f} "
        f'V{y + height:.1f} Z"/>'
    )


def _svg(width: int, height: int, body: str, label: str) -> str:
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" width="100%" '
        f'height="{height}" role="img" aria-label="{_esc(label)}" '
        f'preserveAspectRatio="xMinYMin meet" font-family="{FONT}">{body}</svg>'
    )


# --------------------------------------------------------------------------- bars


def hbar(
    items: list[tuple[str, float]],
    *,
    label: str,
    gutter: int = 210,
    row_height: int = 26,
    width: int = 720,
    value_format: str = "{:.0f}",
    series_var: str = "--series-1",
) -> str:
    """Horizontal bars for nominal categories.

    One hue for every bar: the categories (domains, documentation pages) have no
    natural order, so shading them by value would double-encode length as color.
    """
    if not items:
        return ""
    peak = max(value for _, value in items) or 1
    plot_width = width - gutter - 54
    height = row_height * len(items) + 8
    bar_height = row_height - 10

    parts = []
    for index, (name, value) in enumerate(items):
        y = index * row_height + 4
        bar_width = max(value / peak * plot_width, 1.5)
        parts.append(
            f'<g><title>{_esc(name)}: {_esc(value_format.format(value))}</title>'
            f'<text x="{gutter - 8}" y="{y + bar_height / 2 + 4}" text-anchor="end" '
            f'font-size="{LABEL_SIZE}" fill="var(--text-secondary)">'
            f"{_esc(_fit(name, gutter - 12))}</text>"
            f'<g fill="var({series_var})">'
            f"{_rounded_bar(gutter, y, bar_width, bar_height, 4, 'right')}</g>"
            f'<text x="{gutter + bar_width + 7}" y="{y + bar_height / 2 + 4}" '
            f'font-size="{VALUE_SIZE}" fill="var(--text-primary)" font-weight="600">'
            f"{_esc(value_format.format(value))}</text></g>"
        )
    return _svg(width, height, "".join(parts), label)


def stacked_bar(
    items: list[tuple[str, list[float]]],
    series_names: list[str],
    *,
    label: str,
    gutter: int = 210,
    row_height: int = 26,
    width: int = 720,
) -> str:
    """Part-to-whole per row. Series carry identity, so they take categorical slots."""
    if not items:
        return ""
    peak = max(sum(values) for _, values in items) or 1
    plot_width = width - gutter - 54
    height = row_height * len(items) + 8
    bar_height = row_height - 10

    parts = []
    for index, (name, values) in enumerate(items):
        y = index * row_height + 4
        x = float(gutter)
        total = sum(values)
        segments = []
        for slot, value in enumerate(values):
            if value <= 0:
                continue
            segment_width = value / peak * plot_width
            is_last = slot == max(
                (s for s, v in enumerate(values) if v > 0), default=0
            )
            # 2px surface gap separates touching segments.
            drawn = max(segment_width - (0 if is_last else 2), 1.0)
            segments.append(
                f'<g fill="var(--series-{slot + 1})">'
                f"<title>{_esc(name)} — {_esc(series_names[slot])}: {value:.0f}</title>"
                f"{_rounded_bar(x, y, drawn, bar_height, 4 if is_last else 0, 'right')}</g>"
            )
            x += segment_width
        parts.append(
            f'<g><text x="{gutter - 8}" y="{y + bar_height / 2 + 4}" text-anchor="end" '
            f'font-size="{LABEL_SIZE}" fill="var(--text-secondary)">'
            f"{_esc(_fit(name, gutter - 12))}</text>"
            + "".join(segments)
            + f'<text x="{gutter + total / peak * plot_width + 7}" '
            f'y="{y + bar_height / 2 + 4}" font-size="{VALUE_SIZE}" '
            f'fill="var(--text-primary)" font-weight="600">{total:.0f}</text></g>'
        )

    legend = " ".join(
        f'<span class="key"><i style="background:var(--series-{slot + 1})"></i>{_esc(name)}</span>'
        for slot, name in enumerate(series_names)
    )
    return f'<div class="legend">{legend}</div>' + _svg(width, height, "".join(parts), label)


def diverging_bar(
    items: list[tuple[str, float]],
    *,
    label: str,
    gutter: int = 60,
    row_height: int = 34,
    width: int = 720,
    value_format: str = "{:+.1f}%",
) -> str:
    """Signed deviation from a baseline: two opposed hues, neutral gray at zero."""
    if not items:
        return ""
    extent = max(abs(value) for _, value in items) or 1
    axis_x = gutter + (width - gutter - 40) / 2
    half = (width - gutter - 40) / 2 - 40
    height = row_height * len(items) + 22
    bar_height = row_height - 14

    parts = [
        f'<line x1="{axis_x}" y1="2" x2="{axis_x}" y2="{height - 18}" '
        f'stroke="var(--grid)" stroke-width="1"/>'
    ]
    for index, (name, value) in enumerate(items):
        y = index * row_height + 6
        span = abs(value) / extent * half
        positive = value >= 0
        x = axis_x if positive else axis_x - span
        color = "--diverge-pos" if positive else "--diverge-neg"
        text_x = axis_x + span + 8 if positive else axis_x - span - 8
        parts.append(
            f'<g><title>{_esc(name)}: {_esc(value_format.format(value))}</title>'
            f'<text x="{gutter - 12}" y="{y + bar_height / 2 + 4}" text-anchor="end" '
            f'font-size="{LABEL_SIZE}" fill="var(--text-secondary)">{_esc(name)}</text>'
            f'<g fill="var({color})">'
            f"{_rounded_bar(x, y, max(span, 1.5), bar_height, 4, 'right' if positive else 'left')}"
            f"</g>"
            f'<text x="{text_x}" y="{y + bar_height / 2 + 4}" font-size="{VALUE_SIZE}" '
            f'text-anchor="{"start" if positive else "end"}" fill="var(--text-primary)" '
            f'font-weight="600">{_esc(value_format.format(value))}</text></g>'
        )
    parts.append(
        f'<text x="{axis_x}" y="{height - 4}" text-anchor="middle" font-size="{TICK_SIZE}" '
        f'fill="var(--text-muted)">expected</text>'
    )
    return _svg(width, height, "".join(parts), label)


def histogram(
    bins: list[dict],
    *,
    label: str,
    width: int = 720,
    height: int = 190,
    series_var: str = "--series-1",
) -> str:
    """Distribution over ordered bins: columns, one hue, labelled selectively."""
    if not bins:
        return ""
    peak = max(b["count"] for b in bins) or 1
    left, right, top, bottom = 22, 10, 18, 32
    plot_width = width - left - right
    plot_height = height - top - bottom
    slot = plot_width / len(bins)
    bar_width = slot - 6  # the gap between columns is the surface spacer

    parts = [
        f'<line x1="{left}" y1="{top + plot_height}" x2="{width - right}" '
        f'y2="{top + plot_height}" stroke="var(--grid)" stroke-width="1"/>'
    ]
    for index, item in enumerate(bins):
        bar_height = item["count"] / peak * plot_height
        x = left + index * slot + 3
        y = top + plot_height - bar_height
        # An empty bin draws nothing: a 1px sliver would read as a real value.
        mark = (
            f'<g fill="var({series_var})">'
            f"{_rounded_bar(x, y, bar_width, bar_height, 4, 'top')}</g>"
            if item["count"] > 0
            else ""
        )
        parts.append(
            f'<g><title>{_esc(item["label"])} chars: {item["count"]} questions</title>'
            f"{mark}"
            + (
                f'<text x="{x + bar_width / 2}" y="{y - 5}" text-anchor="middle" '
                f'font-size="{VALUE_SIZE}" fill="var(--text-primary)" font-weight="600">'
                f'{item["count"]}</text>'
                if item["count"] > 0
                else ""
            )
            + f'<text x="{x + bar_width / 2}" y="{height - 12}" text-anchor="middle" '
            f'font-size="{TICK_SIZE}" fill="var(--text-muted)">{_esc(item["label"])}</text>'
            "</g>"
        )
    return _svg(width, height, "".join(parts), label)


# --------------------------------------------------------------------------- grids


def heatmap(
    rows: list[list[int]],
    row_labels: list[str],
    col_labels: list[str],
    *,
    label: str,
    gutter: int = 250,
    cell: int = 34,
    width: int = 720,
) -> str:
    """Domain x exam counts: continuous magnitude, so a single-hue sequential ramp.

    Every cell carries its number, which keeps the grid readable when a step sits
    close to the surface and satisfies the palette's relief requirement.
    """
    if not rows:
        return ""
    peak = max((value for row in rows for value in row), default=0) or 1
    cell_width = min(cell + 22, (width - gutter - 12) / max(len(col_labels), 1))
    top = 22
    height = top + cell * len(rows) + 10

    parts = []
    for index, name in enumerate(col_labels):
        parts.append(
            f'<text x="{gutter + index * cell_width + cell_width / 2}" y="14" '
            f'text-anchor="middle" font-size="{TICK_SIZE}" fill="var(--text-muted)">'
            f"{_esc(name.replace('exam-', ''))}</text>"
        )
    for row_index, (name, row) in enumerate(zip(row_labels, rows)):
        y = top + row_index * cell
        parts.append(
            f'<text x="{gutter - 10}" y="{y + cell / 2 + 4}" text-anchor="end" '
            f'font-size="{LABEL_SIZE}" fill="var(--text-secondary)">'
            f"{_esc(_fit(name, gutter - 14))}</text>"
        )
        for col_index, value in enumerate(row):
            x = gutter + col_index * cell_width
            # Step 0 is reserved for "none": an empty cell should recede, not read as data.
            step = 0 if value == 0 else 1 + round(value / peak * (SEQUENTIAL_STEPS - 1))
            step = min(step, SEQUENTIAL_STEPS)
            # Ink flips once the fill is heavy enough to need it. The two tokens are
            # per-mode: what counts as a "heavy" fill inverts between light and dark.
            ink = "var(--seq-ink-high)" if step >= 4 else "var(--seq-ink-low)"
            parts.append(
                f'<g><title>{_esc(name)} — {_esc(col_labels[col_index])}: {value}</title>'
                f'<rect x="{x + 1}" y="{y + 1}" width="{cell_width - 2}" height="{cell - 2}" '
                f'rx="3" fill="var(--seq-{step})"/>'
                f'<text x="{x + cell_width / 2}" y="{y + cell / 2 + 4}" text-anchor="middle" '
                f'font-size="{VALUE_SIZE}" fill="{ink}" font-weight="600">'
                f'{value if value else ""}</text></g>'
            )
    return _svg(width, height, "".join(parts), label)


def small_multiple(
    title: str,
    items: list[tuple[str, float]],
    *,
    slots: dict[str, int],
    value_format: str = "{:.0f}",
    width: int = 232,
    row_height: int = 30,
) -> str:
    """One panel of a small-multiples set.

    Each entity keeps the same categorical slot in every panel, so a reader who
    learned "realistic is orange" is never repainted by a different metric.
    """
    peak = max(value for _, value in items) or 1
    gutter, right = 96, 44
    plot_width = width - gutter - right
    height = row_height * len(items) + 26
    bar_height = row_height - 12

    parts = [
        f'<text x="0" y="12" font-size="{LABEL_SIZE}" fill="var(--text-primary)" '
        f'font-weight="600">{_esc(title)}</text>'
    ]
    for index, (name, value) in enumerate(items):
        y = 24 + index * row_height
        bar_width = max(value / peak * plot_width, 1.5)
        parts.append(
            f'<g><title>{_esc(name)} — {_esc(title)}: {_esc(value_format.format(value))}</title>'
            f'<text x="{gutter - 8}" y="{y + bar_height / 2 + 4}" text-anchor="end" '
            f'font-size="{TICK_SIZE}" fill="var(--text-secondary)">'
            f"{_esc(_fit(name, gutter - 10, TICK_SIZE))}</text>"
            f'<g fill="var(--series-{slots[name]})">'
            f"{_rounded_bar(gutter, y, bar_width, bar_height, 4, 'right')}</g>"
            f'<text x="{gutter + bar_width + 6}" y="{y + bar_height / 2 + 4}" '
            f'font-size="{TICK_SIZE}" fill="var(--text-primary)" font-weight="600">'
            f"{_esc(value_format.format(value))}</text></g>"
        )
    return _svg(width, height, "".join(parts), f"{title} by exam group")


# --------------------------------------------------------------------------- figures


def stat_tile(label: str, value: str, note: str = "") -> str:
    """The number is the chart. No plot, so no hover layer."""
    return (
        f'<div class="tile"><div class="tile-label">{_esc(label)}</div>'
        f'<div class="tile-value">{_esc(value)}</div>'
        + (f'<div class="tile-note">{_esc(note)}</div>' if note else "")
        + "</div>"
    )


def table(headers: list[str], rows: list[list[str]], *, caption: str = "") -> str:
    """The companion table view every chart ships with."""
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_esc(cell)}</td>" for cell in row) + "</tr>" for row in rows
    )
    return (
        f'<details class="table-view"><summary>{_esc(caption or "Table view")}</summary>'
        f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></details>"
    )
