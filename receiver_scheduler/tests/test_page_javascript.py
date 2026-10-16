#!/usr/bin/env python3
"""Check the page's embedded JavaScript actually parses.

The scheduler's whole UI is one <script> block inside a Python string, edited
from Python. A single stray quote in it is a parse error, and a parse error
means the browser binds *no* event handlers at all - every button on every tab
goes dead while the server stays perfectly healthy and answers every request in
a millisecond. That is a maddening thing to debug from the server side, and it
happened on 2026-08-24: a patch that meant to end `: '')` ended `: '')'`
instead, because the Python escape `\\'` before the closing triple quote emitted
an apostrophe nobody wanted.

There is no JavaScript engine on this host, so this is a tokeniser rather than a
parser: it walks the script tracking whether it is inside a string, a template
literal, or a comment, and reports anything left open. That is narrow, but it is
exactly the failure mode that has actually occurred, and bracket balancing - the
obvious check - did not catch it, because the stray quote left the brackets
perfectly balanced.
"""

import re

import pytest


def _script(html):
    blocks = re.findall(r"<script[^>]*>([\s\S]*?)</script>", html)
    assert blocks, "the page has no script block"
    return "\n".join(blocks)


def _starts_regex(js, i):
    """Whether the '/' at js[i] opens a regex literal rather than dividing:
    true after an operator, an opening bracket, a comma or semicolon, the
    start, or a keyword such as return - the places a value is expected."""
    k = i - 1
    while k >= 0 and js[k] in " \t\r\n":
        k -= 1
    if k < 0:
        return True
    if js[k] in "(,=:[!&|?{};+-*%<>~^":
        return True
    m = re.search(r"([A-Za-z_$][\w$]*)$", js[max(0, k - 12):k + 1])
    return bool(m) and m.group(1) in ("return", "typeof", "case", "in", "of", "void", "delete")


def scan_javascript(js):
    """Return a list of complaints. Empty means nothing obviously broken."""
    problems = []
    line_no = 1
    i = 0
    n = len(js)
    state = None          # None, "'", '"', '`', '//', '/*'
    opened_at = 0
    depth = {"(": 0, "[": 0, "{": 0}
    closer = {")": "(", "]": "[", "}": "{"}

    while i < n:
        c = js[i]
        nxt = js[i + 1] if i + 1 < n else ""
        if c == "\n":
            if state in ("'", '"'):
                problems.append(
                    "line %d: string opened with %s is not closed on its line"
                    % (opened_at, state))
                state = None
            elif state == "//":
                state = None
            line_no += 1
            i += 1
            continue

        if state in ("'", '"', "`"):
            if c == "\\":
                i += 2
                continue
            if c == state:
                state = None
            i += 1
            continue
        if state == "//":
            i += 1
            continue
        if state == "/*":
            if c == "*" and nxt == "/":
                state = None
                i += 2
                continue
            i += 1
            continue

        # not in a string or comment
        if c == "/" and nxt == "/":
            state = "//"
            i += 2
            continue
        if c == "/" and nxt == "*":
            state = "/*"
            i += 2
            continue
        if c == "/" and _starts_regex(js, i):
            # A regex literal, skipped to its closing slash on the same line:
            # its quotes are not strings. /filename\*?=(?:UTF-8'')?"?.../i in
            # observe.js (2026-10-04) read as two unclosed strings until this.
            j, in_class = i + 1, False
            while j < n and js[j] != "\n":
                if js[j] == "\\":
                    j += 2
                    continue
                if js[j] == "[":
                    in_class = True
                elif js[j] == "]":
                    in_class = False
                elif js[j] == "/" and not in_class:
                    break
                j += 1
            if j < n and js[j] == "/":
                i = j + 1
                continue
        if c in "'\"`":
            state = c
            opened_at = line_no
            i += 1
            continue
        if c in depth:
            depth[c] += 1
        elif c in closer:
            depth[closer[c]] -= 1
            if depth[closer[c]] < 0:
                problems.append("line %d: unmatched %s" % (line_no, c))
                depth[closer[c]] = 0
        i += 1

    if state in ("'", '"', "`"):
        problems.append("line %d: string opened with %s never closed"
                        % (opened_at, state))
    for opener, d in depth.items():
        if d:
            problems.append("%s unbalanced by %+d" % (opener, d))
    return problems


def test_the_page_javascript_parses():
    """Scans whatever the page actually loads, inline or as modules.

    Goes through page_sources rather than reading HTML_TEMPLATE directly, so
    this kept working when the page moved into static files on 2026-08-25 -
    and, more to the point, so it still covers the JavaScript afterwards. A
    guard that only understands the old arrangement stops guarding the moment
    the arrangement changes, and says nothing about it.
    """
    import h1_web_scheduler as sched
    import page_sources as ps

    sched.app.config["TESTING"] = True
    with sched.app.test_client() as client:
        _, js = ps.fetch_page(client)
    assert js.strip(), "no JavaScript was served with the page at all"
    problems = scan_javascript(js)
    assert not problems, "the page's JavaScript is broken:\n  " + "\n  ".join(problems)


def test_the_scanner_catches_the_bug_that_prompted_it():
    """The exact 2026-08-24 failure: a stray quote after a ternary's else."""
    broken = """
        function f(d) {
            g.innerHTML = 'a'
                + (d.x ? '<div>b</div>' : '')'
                + warn;
        }
    """
    assert scan_javascript(broken), "the scanner missed a stray quote"


def test_the_scanner_does_not_cry_wolf():
    fine = """
        function f(d) {
            // an apostrophe in a comment won't hurt
            const s = 'it\\'s fine';
            const t = "nor \\"this\\"";
            const u = `a template ${d.x} literal`;
            return s + t + u;   /* nor this */
        }
    """
    assert scan_javascript(fine) == []


def test_brackets_alone_would_have_missed_it():
    """Why this exists rather than a brace count: the brackets stayed balanced."""
    broken = "x = (d.y ? 'a' : '')'\n + z;"
    assert broken.count("(") == broken.count(")")
    assert scan_javascript(broken)


@pytest.mark.parametrize("snippet", [
    "const a = 'unterminated;",
    'const b = "also unterminated;',
    "const c = 'ok'; const d = 'not ok",
])
def test_unterminated_strings_are_reported(snippet):
    assert scan_javascript(snippet)
