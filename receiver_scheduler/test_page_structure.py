"""Structural guards on the operator page.

Written on 2026-08-25, before the page was moved out of its Python string into
static files, and deliberately before rather than after: a net woven once the
fall has started is not a net. Every test here passed on the page as it stood
that morning, so a failure means the refactor lost something, not that the page
was always like this.

They are structural, not behavioural - none of them can tell whether a function
does the right thing. What they catch is every *silent* way this refactor can
break, and silence is the whole problem: the page's failures do not announce
themselves. A stray apostrophe from a Python escape killed every handler on
2026-08-24 while the server went on answering in a millisecond, and a `\\n`
written into the same string did it again the following day. Both were found by
clicking, which is not a method.

There is no JavaScript toolchain on the observatory host - no node, so no lint
and no runnable golden harness - and these exist to be the honest substitute.
"""

import json
import os

import pytest

import h1_web_scheduler as scheduler
import page_sources as ps

BASELINE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "test_data", "page_baseline.json")


@pytest.fixture(scope="module")
def page():
    scheduler.app.config["TESTING"] = True
    with scheduler.app.test_client() as client:
        return ps.fetch_page(client)


@pytest.fixture(scope="module")
def baseline():
    with open(BASELINE) as fh:
        return json.load(fh)


def test_every_element_the_scripts_look_up_exists_in_the_markup(page):
    """A lookup with no element returns null, and the failure surfaces later.

    This is how a module split goes wrong: markup and script move apart, one
    of them loses an element or renames it, and nothing complains until someone
    presses the button that reads it.
    """
    html, js = page
    missing = sorted(ps.referenced_ids(js, html) - ps.element_ids(html, js))
    assert not missing, "getElementById with no matching id: %s" % missing


def test_every_inline_handler_can_actually_be_called(page):
    """The single most important guard in the page refactor.

    Inline `onclick="foo()"` resolves against the global scope. In a classic
    script that is where `function foo()` lands, so it works by accident; in an
    ES module it does not, so the handler finds nothing - and it finds nothing
    at *click* time, with no error at load, on a page that otherwise looks
    perfectly healthy. Fifty handlers were bound this way when the split began.

    So each one must be either a plain declaration or an explicit
    `window.foo = foo`, and this is what refuses to let one slip through.
    """
    html, js = page
    unreachable = sorted(ps.inline_handlers(html) - ps.reachable_functions(js))
    assert not unreachable, (
        "inline handlers with nothing to call: %s\n"
        "If the page is in modules, these need assigning to window in the "
        "entry module." % unreachable)


def test_every_api_the_page_calls_is_a_route_that_exists(page):
    """Catches a blueprint move that changes a URL.

    A fetch to a path Flask does not serve gets a 404 the page mostly swallows,
    so the tab quietly stops updating rather than reporting anything.
    """
    import re

    html, js = page
    rules = [r.rule for r in scheduler.app.url_map.iter_rules()]
    patterns = [re.compile(re.sub(r"<[^>]+>", "[^/]+", r) + r"\Z") for r in rules]
    missing = sorted(p for p in ps.fetch_paths(js, html)
                     if not any(pat.match(p) for pat in patterns))
    assert not missing, "fetch() paths with no Flask route: %s" % missing


def test_nothing_the_page_had_has_been_lost(page, baseline):
    """Against a snapshot taken before the refactor started.

    Additions are fine and expected. Losses are the thing: a function dropped
    while splitting a file, or an element that fell out of the markup between
    two tabs. Neither would fail any of the tests above if the *reference* to it
    went at the same time, which is exactly what happens when a whole feature is
    moved badly.

    Regenerate deliberately, never to make this pass:
        python test_page_structure.py --bless
    """
    html, js = page
    lost_fns = sorted(set(baseline["functions"]) - ps.reachable_functions(js))
    lost_ids = sorted(set(baseline["ids"]) - ps.element_ids(html, js))
    lost_api = sorted(set(baseline["fetch_paths"]) - ps.fetch_paths(js, html))
    assert not lost_fns, "functions that used to exist and now do not: %s" % lost_fns
    assert not lost_ids, "element ids no longer in the markup: %s" % lost_ids
    assert not lost_api, "API paths the page no longer calls: %s" % lost_api


def test_the_page_still_carries_all_nine_tabs(page):
    """The coarsest check there is, and it would have caught more than one
    refactor that quietly dropped a panel."""
    html, _ = page
    for tab in ("scheduler", "sunscan", "horizon", "rf", "camera",
                "simulator", "observe", "config", "log"):
        assert 'id="tab-%s"' % tab in html, "the %s tab is gone" % tab


def _bless():
    """Write the baseline from the page as it stands now."""
    scheduler.app.config["TESTING"] = True
    with scheduler.app.test_client() as client:
        html, js = ps.fetch_page(client)
    os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
    with open(BASELINE, "w") as fh:
        json.dump({
            "note": "Snapshot of the operator page before it was split out of "
                    "h1_web_scheduler.py. Guards against losing something in "
                    "the move; see test_page_structure.py.",
            "functions": sorted(ps.reachable_functions(js)),
            "ids": sorted(ps.element_ids(html, js)),
            "fetch_paths": sorted(ps.fetch_paths(js, html)),
        }, fh, indent=2)
    print("baseline written: %s" % BASELINE)


if __name__ == "__main__":
    import sys

    if "--bless" in sys.argv:
        _bless()
    else:
        print("run with --bless to regenerate the baseline")
