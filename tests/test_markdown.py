"""The measured Markdown subset, enforced rather than assumed.

The corpus obeys one subset across all 5490 fields -- bold, italic, inline code
and links, with 0 headings, 0 lists and 0 fences. That is a measurement, and this
file is what turns it into a guarantee: the renderer refuses anything else, and
the guard is swept over the whole corpus to prove the refusal costs nothing today.

The interesting case is not the bulleted list. It is `` `<instructions>` ``:
fourteen explanations discuss XML-style tags inside code spans, and a raw-HTML
check that does not mask code spans first rejects every one of them. A guard that
fails on the corpus it guards is worse than no guard, because the first thing
anybody does with it is turn it off.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from markupsafe import Markup

from examkb.compat import markdown_violations, mask_code_spans
from examkb.web.markdown import MarkdownOutsideSubset, check, plain, render

REAL_KB = REPO_ROOT / "kb"


def corpus_fields() -> list[tuple[str, str, str]]:
    """Every indexable text field in `kb/`, as `(question id, where, text)`."""
    found: list[tuple[str, str, str]] = []
    for line in (REAL_KB / "questions.jsonl").read_text(encoding="utf-8").splitlines():
        question = json.loads(line)
        found.append((question["id"], "prompt_md", question["prompt_md"]))
        if question.get("overall_explanation_md"):
            found.append((question["id"], "overall", question["overall_explanation_md"]))
        for option in question["options"]:
            found.append((question["id"], f"option {option['label']}", option["text_md"]))
            if option.get("explanation_md"):
                found.append(
                    (question["id"], f"option {option['label']} why", option["explanation_md"])
                )
    return found


FIELDS = corpus_fields()


# ------------------------------------------------------------------------- the subset


@pytest.mark.parametrize(
    "text",
    [
        "**bold**",
        "*italic*",
        "`inline code`",
        "[a link](https://docs.claude.com/en/docs/x)",
        "Plain prose with no markup at all.",
        "**bold** and *italic* and `code` and [link](https://x.test/y), together.",
        "Two paragraphs.\n\nSeparated by a blank line.",
        r"An escaped \*asterisk\* stays literal.",
        # The case the whole masking step exists for.
        "XML-style tags such as `<instructions>` and `<examples>` help.",
        "A span with a backtick inside: ``code with ` inside``.",
    ],
)
def test_the_subset_is_accepted(text: str) -> None:
    assert markdown_violations(text) == []
    assert isinstance(render(text), Markup)


@pytest.mark.parametrize(
    ("text", "what"),
    [
        ("# Heading", "heading"),
        ("### Deeper", "heading"),
        ("Title\n=====", "setext heading"),
        ("- a bullet", "bullet list"),
        ("* another bullet", "bullet list"),
        ("+ a third", "bullet list"),
        ("1. ordered", "ordered list"),
        ("2) also ordered", "ordered list"),
        ("```python\nx = 1\n```", "code fence"),
        ("~~~\nfenced\n~~~", "code fence"),
        ("> a quotation", "blockquote"),
        ("| a | b |", "table"),
        ("![alt](https://x.test/i.png)", "image"),
        ("<div>raw html</div>", "raw html"),
        ("<br/>", "raw html"),
        ("prose\n\n- then a list", "bullet list"),
    ],
)
def test_everything_outside_the_subset_raises(text: str, what: str) -> None:
    with pytest.raises(MarkdownOutsideSubset) as raised:
        render(text)
    assert what in str(raised.value)
    assert [name for name, _offset in raised.value.violations] == [what] or what in [
        name for name, _offset in raised.value.violations
    ]


def test_the_error_says_where_and_points_at_the_rule() -> None:
    with pytest.raises(MarkdownOutsideSubset) as raised:
        render("- a bullet", where="question.overall_explanation_md")
    message = str(raised.value)
    assert "question.overall_explanation_md" in message
    assert "issues/032" in message
    assert "bold, italic, inline code and links" in message


def test_a_closed_fence_is_not_mistaken_for_a_code_span() -> None:
    """Three backticks closed by three backticks is a well-formed span to a masker.

    Which is why the fence check looks at the raw text. Without that, the one
    construct most likely to arrive from a language model walks straight through.
    """
    fenced = "```\nnot allowed\n```"
    assert mask_code_spans(fenced).strip() == ""  # the masker does swallow it
    assert markdown_violations(fenced) == [("code fence", 0)]


def test_masking_preserves_offsets() -> None:
    """032 reports the offending span; that only works if positions still line up."""
    text = "before `code here` after"
    masked = mask_code_spans(text)
    assert len(masked) == len(text)
    assert masked.index("after") == text.index("after")


# --------------------------------------------------------------------- the corpus


def test_there_is_a_corpus_to_sweep() -> None:
    assert len(FIELDS) == 5490


def test_the_guard_finds_nothing_in_the_whole_corpus() -> None:
    """The criterion: 0 hits over all 5490 fields. Any hit is a false positive."""
    offenders = [
        (question_id, where, markdown_violations(text), text[:80])
        for question_id, where, text in FIELDS
        if markdown_violations(text)
    ]
    assert offenders == []


def test_every_corpus_field_renders() -> None:
    for question_id, where, text in FIELDS:
        render(text, where=f"{question_id} {where}")


def test_the_fields_that_contain_angle_brackets_render_them_escaped() -> None:
    """`<instructions>` is text, not a tag, and has to reach the page as text."""
    with_brackets = [text for _id, _where, text in FIELDS if "<" in text]
    assert len(with_brackets) == 14

    for text in with_brackets:
        html = str(render(text))
        assert "&lt;" in html
        assert "<instructions>" not in html or "<code>" in html
        # Nothing the corpus wrote became a tag.
        assert "<div" not in html and "<script" not in html


# --------------------------------------------------------------------- rendering


def test_render_escapes_html_outside_code_spans() -> None:
    html = str(render("a < b and 5 > 3 & more"))
    assert "&lt;" in html and "&gt;" in html and "&amp;" in html


def test_links_open_elsewhere_without_handing_over_the_window() -> None:
    html = str(render("[docs](https://docs.claude.com/en/docs/x)"))
    assert 'href="https://docs.claude.com/en/docs/x"' in html
    assert 'rel="noopener"' in html
    assert 'target="_blank"' in html


def test_render_returns_markup_so_a_template_does_not_double_escape() -> None:
    assert isinstance(render("**bold**"), Markup)
    assert "<strong>bold</strong>" in str(render("**bold**"))


def test_empty_text_is_empty_markup_not_an_error() -> None:
    assert render(None) == Markup("")
    assert render("") == Markup("")
    assert check(None) is None


def test_plain_strips_the_markup_for_titles_and_lists() -> None:
    assert plain("**bold** and *italic* and `code`") == "bold and italic and code"
    assert plain("[a link](https://x.test/y)") == "a link"
    assert plain(None) == ""
