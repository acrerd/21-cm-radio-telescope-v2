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


def test_no_escape_survived_the_move_out_of_the_python_string(page):
    """The bug the extraction itself introduced, on 2026-08-25.

    The page was lifted out by taking the *source text* between the triple
    quotes rather than the string Python built from it, so every escape came
    across un-collapsed. `'\\n'` in the source is a newline once Python has
    read it and that is what the browser used to receive; the extracted file
    kept both backslashes, which JavaScript reads as a backslash followed by an
    n. The Log tab stopped showing line breaks, the pass predictions printed
    \\u00b0 instead of a degree sign, and `tle.split()` stopped finding the
    satellite name.

    None of the other guards saw it: nothing was lost, no id went missing, no
    handler became unreachable. It is a content bug, and only the Log tab
    looking wrong revealed it.

    This codebase has no legitimate use for a doubled backslash in the page's
    JavaScript, so the presence of one means an escape has been mangled again.
    If a real one is ever needed, the right move is to think hard about why
    before editing this test.
    """
    _, js = page
    import re

    bad = sorted(set(re.findall(r'\\\\.', js)))
    assert not bad, (
        "doubled backslashes in the page JavaScript: %s\n"
        "These are almost certainly escapes that were not collapsed." % bad)


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


def _parse_javascript(path, module):
    """Parse a script with esprima, which stops at ES2019: the few newer
    forms these files use - optional chaining, nullish coalescing, the
    optional catch binding - are rewritten to their older equivalents first,
    so anything that still fails is a real syntax error."""
    import re
    esprima = pytest.importorskip("esprima")
    src = open(path).read()
    src = re.sub(r"\?\.\(", "(", src)
    src = re.sub(r"\?\.\[", "[", src)
    src = src.replace("?.", ".").replace("??", "||")
    src = re.sub(r"catch\s*\{", "catch (_e) {", src)
    (esprima.parseModule if module else esprima.parseScript)(src)


@pytest.mark.parametrize("name", sorted(
    f for f in os.listdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "js"))
    if f.endswith(".js")))
def test_every_page_script_parses(name):
    """A syntax error in one classic script kills every handler it defines
    while the rest of the page keeps working - the Save button of the
    schedule form, say - and nothing in the browser tells the operator.
    Twice on 2026-08-24/25 that took a day to find. Parse each file.
    """
    _parse_javascript(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "web", "js", name), module=False)


@pytest.mark.parametrize("name", sorted(
    f for f in os.listdir(os.path.join(scheduler.SIMULATOR_DIR, "js")) if f.endswith(".js")))
def test_every_simulator_script_parses(name):
    _parse_javascript(os.path.join(scheduler.SIMULATOR_DIR, "js", name), module=True)


def test_the_famous_targets_are_the_simulator_targets():
    """The schedule form's picker is a copy of the simulator's targets menu.

    A copy, deliberately: the web simulator must keep working with no
    scheduler present (it can be served from a static host), so it cannot
    fetch the list from here, and the scheduler page loads no ES modules so
    it cannot import the simulator's. Two copies are safe exactly as long as
    something holds them equal - which is this test, parsing both from
    source, so an entry added, dropped or moved on either side fails here
    with its name.

    The simulator's menu is TARGETS (the H I list) plus continuumSources()
    from ephemeris.js - Cyg A, Cas A, Tau A, the Sun and the Moon - and the
    first version of the picker copied only TARGETS, which is exactly the
    kind of drift this test exists to catch. The fixed trio's galactic
    coordinates are re-derived here from the RA/Dec in ephemeris.js, so a
    typo in the hand-converted numbers fails with the source's name.
    """
    import ast
    import math
    import os
    import re

    here = os.path.dirname(os.path.abspath(__file__))
    sim_js = os.path.join(here, "..", "astro_simulator", "web", "js")

    def parse_rows(path, name):
        src = open(path).read()
        block = re.search(name + r"\s*=\s*\[(.*?)\n\s*\];", src, re.S)
        assert block, "no %s list found in %s" % (name, path)
        rows = []
        for m in re.finditer(r'\[\s*"((?:[^"\\]|\\.)*)"\s*,\s*'
                             r'(?:([-\d.]+)|"(\w+)")\s*,\s*'
                             r'(?:([-\d.]+)|"(\w+)")', block.group(1)):
            rows.append((ast.literal_eval('"%s"' % m.group(1)),
                         float(m.group(2)) if m.group(2) else m.group(3),
                         float(m.group(4)) if m.group(4) else m.group(5)))
        return rows

    sim = parse_rows(os.path.join(sim_js, "ui.js"), "TARGETS")
    sched_list = parse_rows(
        os.path.join(here, "web", "js", "schedule.js"), "FAMOUS_TARGETS")
    assert len(sim) > 10, "the simulator list should be substantial"

    # The H I targets, verbatim and in order.
    assert sched_list[:len(sim)] == sim, (
        "the schedule form's famous-target list has drifted from the "
        "simulator's TARGETS - change both together")

    # Then the continuum sources. The trio's RA/Dec come from ephemeris.js
    # itself, converted here the same way the simulator converts them.
    extras = {row[0]: row for row in sched_list[len(sim):]}
    eph = open(os.path.join(sim_js, "ephemeris.js")).read()
    trio = dict(re.findall(
        r'\["(Cyg A|Cas A|Tau A)",\s*eqToGal\(([\d.]+,\s*[-\d.]+)\)', eph))
    assert len(trio) == 3, "ephemeris.js should define Cyg A, Cas A, Tau A"

    import ephem
    for name, radec in trio.items():
        ra, dec = (float(x) for x in radec.split(","))
        g = ephem.Galactic(ephem.Equatorial(
            math.radians(ra), math.radians(dec), epoch=ephem.J2000))
        assert name in extras, "%s is in the simulator menu but not the picker" % name
        assert extras[name][1] == pytest.approx(math.degrees(g.lon), abs=0.01), name
        assert extras[name][2] == pytest.approx(math.degrees(g.lat), abs=0.01), name

    for name, obj in (("Sun", "sun"), ("Moon", "moon")):
        assert extras.get(name) == (name, "object", obj), (
            "%s must save as an ephemeris object, not fixed coordinates" % name)

    assert len(extras) == 5, "unexpected extra rows beyond the continuum sources"
