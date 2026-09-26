"""Rendering the corpus's Markdown, and refusing to render anything else.

The corpus obeys one measured subset -- **bold**, *italic*, `inline code` and
links -- across all 5490 fields: 0 headings, 0 lists, 0 fences. That is a fact
about the data, and this module turns it into a rule about the code.

Two things follow from it:

1. **There is one renderer, and it is not this file.** `tools/_shared.py`'s
   `md_to_html` is what built `kb/study/**.md` and `kb/reports/browse.html`; the
   app reaches it through `examkb/compat.py` (002's bridge rule). A second
   implementation here would be two renderers that agree until the day they do
   not, on a corpus where the static reports are meant to stay valid throughout
   V1a.
2. **Anything outside the subset raises.** `md_to_html` renders inline constructs
   and wraps paragraphs; handed a bulleted list it emits the bullets as literal
   text, which looks like a rendering bug rather than what it is -- content that
   should never have reached the corpus. 032's generator validators are the place
   that failure gets *caught*; this is the place it gets *noticed*, and it has to
   be noticed in phase 1 because phase 6 is where content starts being generated.

`Markup` is returned rather than `str`, so a template writes `{{ text | md }}` and
Jinja does not escape the tags this produced -- while everything that went through
`escape()` inside the renderer stays escaped.
"""

from __future__ import annotations

from markupsafe import Markup

from examkb.compat import markdown_violations, md_to_html, strip_markdown


class MarkdownOutsideSubset(ValueError):
    """Text using a construct the corpus does not contain and this app will not render."""

    def __init__(self, violations: list[tuple[str, int]], text: str, where: str = "") -> None:
        self.violations = violations
        self.text = text
        self.where = where
        what = ", ".join(f"{name} at {offset}" for name, offset in violations)
        subject = f"{where}: " if where else ""
        super().__init__(
            f"{subject}{what}. The corpus subset is bold, italic, inline code and links "
            f"-- see issues/032. Offending text: {text[:120]!r}"
        )


def check(text: str | None, *, where: str = "") -> None:
    """Raise unless `text` is inside the subset."""
    violations = markdown_violations(text or "")
    if violations:
        raise MarkdownOutsideSubset(violations, text or "", where)


def render(text: str | None, *, where: str = "") -> Markup:
    """The corpus's Markdown as HTML, or `MarkdownOutsideSubset`."""
    if not text:
        return Markup("")
    check(text, where=where)
    return Markup(md_to_html(text))


def plain(text: str | None) -> str:
    """The same text with its Markdown removed. For titles and `<meta>`."""
    if not text:
        return ""
    return strip_markdown(text)
