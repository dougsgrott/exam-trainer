"""The click the plan is actually worried about, in a real browser.

Integration fix 3 says the second click of a 2-of-4 lands on a node HTMX has
already detached, and the answer submits with one selection -- a question answered
correctly, marked wrong, with nothing in any log. `tests/test_runner.py` proves the
server can never cause that, because it never sends a body. This file proves the
browser does the right thing with what it gets: two clicks 200 ms apart, and
**both are in the database afterwards**.

The assertion is deliberately server-side. What the DOM looks like at the end is
not the bug; what got stored is.

The last test in this file reproduces the bug instead of describing it -- the form
is replaced mid-interaction, the second click lands on the detached node, and
exactly one label reaches the database. That is what makes the first test a test
with teeth rather than one that has never been seen to fail.

Setting up a fresh checkout takes two steps, the second of which needs root:

    uv run playwright install chromium
    sudo apt-get install libnspr4 libnss3 libasound2t64

Without them every test here skips with that message rather than failing, because a
red suite nobody can turn green stops being read.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from examkb import db as db_module
from examkb import status as status_module
from examkb.models import journal
from examkb.services import runner as runner_service

pytestmark = pytest.mark.slow

INSTALL = (
    "chromium cannot launch. `uv run playwright install chromium`, then "
    "`sudo apt-get install libnspr4 libnss3 libasound2t64`"
)


def chromium_or_skip():
    """The browser, or a skip that says exactly how to get one."""
    playwright = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
    manager = playwright.sync_playwright().start()
    try:
        browser = manager.chromium.launch()
    except Exception as error:  # the three missing .so files land here
        manager.stop()
        pytest.skip(f"{INSTALL} ({str(error).splitlines()[0][:80]})")
    return manager, browser


@pytest.fixture
def browser():
    manager, launched = chromium_or_skip()
    try:
        yield launched
    finally:
        launched.close()
        manager.stop()


@pytest.fixture
def server(real_db: Engine):
    """The real app on a real loopback socket, in a thread."""
    import uvicorn

    from examkb.web.app import create_app
    from examkb.web.serve import bind

    url = str(real_db.url)
    db_module.engine_for.cache_clear()
    app = create_app(
        database_url=url, status_provider=lambda: status_module.projection_status(url=url)
    )
    bound = bind("127.0.0.1", 0)
    config = uvicorn.Config(app, log_level="warning", access_log=False)
    instance = uvicorn.Server(config)
    thread = threading.Thread(
        target=instance.run, kwargs={"sockets": [bound.socket]}, daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 15
    while not instance.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert instance.started, "uvicorn did not start"
    try:
        yield bound.url, url
    finally:
        instance.should_exit = True
        thread.join(timeout=15)
        bound.close()
        db_module.engine_for.cache_clear()


def multi_select_position(url: str, attempt_id: int) -> int:
    page = runner_service.page(attempt_id, 0, url=url)
    for entry in page.map:
        if runner_service.page(attempt_id, entry.position, url=url).item.multi:
            return entry.position
    pytest.skip("this seed drew no multi-select question")


def stored(url: str, attempt_id: int, position: int) -> list[str]:
    with Session(db_module.engine_for(url)) as session:
        item = session.scalars(
            sa.select(journal.AttemptItem).where(
                journal.AttemptItem.attempt_id == attempt_id,
                journal.AttemptItem.position == position,
            )
        ).one()
        return list(item.selected_labels or [])


def test_two_clicks_200ms_apart_both_persist(browser, server) -> None:
    """The criterion, word for word, asserted against the database.

    With `hx-swap="outerHTML"` the second click would land on a detached node and
    only one label would be stored. Both are.
    """
    base, url = server
    attempt_id = runner_service.begin(certification_id="ccao-f", count=8, seed=7, url=url)
    position = multi_select_position(url, attempt_id)

    page = browser.new_page()
    page.goto(f"{base}/exam/{attempt_id}/q/{position}")
    boxes = page.locator('input[name="label"]')
    assert boxes.count() == 4

    first = boxes.nth(0)
    second = boxes.nth(2)

    first.click()
    page.wait_for_timeout(200)
    second.click()
    page.wait_for_selector('#save-state[data-state="saved"]', timeout=10_000)
    page.wait_for_timeout(300)

    labels = sorted(stored(url, attempt_id, position))
    assert len(labels) == 2, f"only {labels} reached the server"
    assert labels == sorted(
        [first.get_attribute("value"), second.get_attribute("value")]
    )
    page.close()


def test_the_inputs_are_the_same_nodes_after_a_save(browser, server) -> None:
    """The mechanism, not just its consequence: nothing was replaced."""
    base, url = server
    attempt_id = runner_service.begin(certification_id="ccao-f", count=5, seed=11, url=url)

    page = browser.new_page()
    page.goto(f"{base}/exam/{attempt_id}/q/0")
    page.evaluate(
        "document.querySelectorAll('input[name=label]')"
        ".forEach((el, i) => el.dataset.witness = 'node-' + i)"
    )

    page.locator('input[name="label"]').first.click()
    page.wait_for_selector('#save-state[data-state="saved"]', timeout=10_000)

    witnesses = page.eval_on_selector_all(
        'input[name="label"]', "els => els.map(el => el.dataset.witness)"
    )
    assert witnesses == ["node-0", "node-1", "node-2", "node-3"], "the controls were replaced"
    page.close()


def test_the_counter_and_map_update_without_a_reload(browser, server) -> None:
    base, url = server
    attempt_id = runner_service.begin(certification_id="ccao-f", count=5, seed=13, url=url)

    page = browser.new_page()
    page.goto(f"{base}/exam/{attempt_id}/q/0")
    assert page.inner_text("#answered") == "0"
    assert page.inner_text("#unanswered") == "5"

    page.locator('input[name="label"]').first.click()
    page.wait_for_selector('#save-state[data-state="saved"]', timeout=10_000)

    assert page.inner_text("#answered") == "1"
    assert page.inner_text("#unanswered") == "4"
    assert "answered" in (page.get_attribute('.qmap a[data-position="0"]', "class") or "")
    page.close()


def test_a_failed_save_says_so(browser, server) -> None:
    """The visible indicator when a write has not landed."""
    base, url = server
    attempt_id = runner_service.begin(certification_id="ccao-f", count=5, seed=17, url=url)

    page = browser.new_page()
    page.goto(f"{base}/exam/{attempt_id}/q/0")
    page.route("**/answer", lambda route: route.abort())

    page.locator('input[name="label"]').first.click()
    page.wait_for_selector('#save-state[data-state="failed"]', timeout=10_000)

    assert "not saved" in page.inner_text("#save-state")
    page.close()


def test_the_bug_this_all_exists_for_is_real(browser, server) -> None:
    """Reproduce integration fix 3's failure, so the test above is known to bite.

    `hx-swap="outerHTML"` replaces the element containing the inputs. Here that
    replacement is done directly in the page -- same effect, no need to ship a
    broken endpoint -- and then the second option is clicked on what is now a
    detached node.

    The assertion is the damage: **one** label reaches the database instead of two,
    silently, and the person sees a question they answered correctly marked wrong.
    If this ever stops happening, `test_two_clicks_200ms_apart_both_persist` has
    stopped proving anything and this test says so first.
    """
    base, url = server
    attempt_id = runner_service.begin(certification_id="ccao-f", count=8, seed=7, url=url)
    position = multi_select_position(url, attempt_id)

    page = browser.new_page()
    page.goto(f"{base}/exam/{attempt_id}/q/{position}")
    boxes = page.locator('input[name="label"]')

    first, second = boxes.nth(0), boxes.nth(2)
    first_value, second_value = first.get_attribute("value"), second.get_attribute("value")

    first.click()
    page.wait_for_selector('#save-state[data-state="saved"]', timeout=10_000)

    # What an outerHTML swap does to the form the person is still clicking in.
    page.evaluate(
        """() => {
            const form = document.getElementById('answer-form');
            const clone = form.cloneNode(true);
            form.replaceWith(clone);
        }"""
    )
    second.click(force=True, timeout=2000)
    page.wait_for_timeout(500)

    labels = stored(url, attempt_id, position)
    assert labels == [first_value], (
        f"expected the detached click to be lost, got {labels}"
    )
    assert second_value not in labels
    page.close()


# ------------------------------------------------------------------ 050: the theme


def theme_of(page) -> str:
    """What the page is actually painting, not what it was asked to paint."""
    return page.evaluate(
        "() => getComputedStyle(document.body).backgroundColor"
    )


def test_the_theme_toggle_switches_and_persists(browser, server) -> None:
    """050's criterion: it toggles, and the choice survives a reload."""
    base, _url = server
    page = browser.new_page(color_scheme="light")
    page.goto(f"{base}/")

    light = theme_of(page)
    assert page.get_attribute("html", "data-theme") is None, "nothing stored yet"

    page.click("#theme-toggle")
    dark = theme_of(page)

    assert page.get_attribute("html", "data-theme") == "dark"
    assert dark != light

    page.reload()
    assert page.get_attribute("html", "data-theme") == "dark"
    assert theme_of(page) == dark

    page.goto(f"{base}/browse")
    assert page.get_attribute("html", "data-theme") == "dark", "it survives a navigation too"
    page.close()


def test_the_stored_theme_is_on_the_document_before_the_first_paint(browser, server) -> None:
    """The criterion nobody sees when it works.

    Read at `DOMContentLoaded`, which is before any stylesheet-driven paint could
    have been corrected. A script loaded from a file would not have run yet.
    """
    base, _url = server
    page = browser.new_page(color_scheme="light")
    page.goto(f"{base}/")
    page.click("#theme-toggle")

    second = browser.new_page(color_scheme="light")
    second.add_init_script(
        "document.addEventListener('DOMContentLoaded',"
        " () => { window.__early = document.documentElement.getAttribute('data-theme'); });"
    )
    second.goto(f"{base}/")
    second.evaluate("() => localStorage.setItem('kb-theme', 'dark')")
    second.reload()

    assert second.evaluate("() => window.__early") == "dark"
    page.close()
    second.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_with_nothing_stored_it_follows_the_operating_system(browser, server, scheme) -> None:
    """The criterion, both ways -- which is only testable in a real browser."""
    base, _url = server
    page = browser.new_page(color_scheme=scheme)
    page.goto(f"{base}/")

    assert page.get_attribute("html", "data-theme") is None
    background = theme_of(page)
    page.close()

    other = browser.new_page(color_scheme="dark" if scheme == "light" else "light")
    other.goto(f"{base}/")
    assert theme_of(other) != background, "the OS preference changed nothing"
    other.close()


def test_a_forced_theme_beats_the_operating_system(browser, server) -> None:
    """A person in a dark OS who wants light must get light."""
    base, _url = server
    page = browser.new_page(color_scheme="dark")
    page.goto(f"{base}/")
    dark_by_os = theme_of(page)

    page.click("#theme-toggle")

    assert page.get_attribute("html", "data-theme") == "light"
    assert theme_of(page) != dark_by_os
    page.close()


def test_the_results_chart_follows_the_theme(browser, server) -> None:
    """The SVG is not re-rendered; its colours are tokens, so it just follows."""
    base, url = server
    attempt_id = runner_service.begin(certification_id="ccao-f", count=4, seed=51, url=url)
    with Session(db_module.engine_for(url)) as session:
        from examkb.services.attempts import answer, submit

        for position in range(4):
            answer(session, attempt_id, position, ["A"])
        submit(session, attempt_id)
        session.commit()

    page = browser.new_page(color_scheme="light")
    page.goto(f"{base}/results/{attempt_id}")
    light = page.evaluate(
        "() => getComputedStyle(document.querySelector('svg.chart text')).fill"
    )

    page.click("#theme-toggle")
    dark = page.evaluate(
        "() => getComputedStyle(document.querySelector('svg.chart text')).fill"
    )

    assert light != dark, "the chart did not follow the toggle"
    page.close()
