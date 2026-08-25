#!/usr/bin/env python3
"""Collect the operator page and every script it loads, however it is served.

The page moved out of a Python string into static files on 2026-08-25. The
structural tests that guard that move have to run identically on both sides of
it, or they cannot be trusted to have guarded anything - so nothing here knows
whether the JavaScript arrived inline or as a module. It asks Flask for the
page, follows the local `<script src>` it finds, and hands back the markup and
the concatenated script text.

Used by test_page_structure.py and test_page_javascript.py, and importable from
a REPL when something on the page is behaving oddly.
"""

import re

_SCRIPT_TAG = re.compile(r'<script([^>]*)>(.*?)</script>', re.S)
_SRC = re.compile(r'\bsrc="([^"]+)"')


def fetch_page(client, path="/"):
    """(html, js) - the markup, and every script the page pulls in, in order.

    `client` is a Flask test client. Scripts are followed only when they are
    local paths; an external one would not be ours to check.
    """
    html = client.get(path).get_data(as_text=True)
    chunks = []
    for attrs, inline in _SCRIPT_TAG.findall(html):
        src = _SRC.search(attrs)
        if not src:
            chunks.append(inline)
            continue
        url = src.group(1)
        if url.startswith(("http://", "https://", "//")):
            continue
        resp = client.get(url if url.startswith("/") else "/" + url.lstrip("./"))
        if resp.status_code == 200:
            chunks.append(resp.get_data(as_text=True))
    return html, "\n".join(chunks)


def element_ids(html):
    """Every id the markup defines."""
    return set(re.findall(r'\bid="([^"]+)"', html))


def referenced_ids(js, html=""):
    """Every id the scripts look up by name."""
    return set(re.findall(r"""getElementById\(['"]([^'"]+)['"]\)""", js + html))


def inline_handlers(html):
    """Function names bound from on* attributes in the markup."""
    return set(re.findall(r'\son\w+="(\w+)\(', html))


def defined_functions(js):
    """Top-level function declarations in the scripts."""
    return set(re.findall(r'(?:^|\n)\s*function (\w+)\(', js))


def window_exports(js):
    """Names explicitly published to the global scope.

    Under ES modules a `function foo()` is private to its module, so an inline
    `onclick="foo()"` finds nothing and fails at click time - not at load, which
    is what makes it dangerous. Anything bound from the markup has to be
    assigned to `window` on purpose, and this is what finds those assignments.
    """
    names = set(re.findall(r'window\.(\w+)\s*=', js))
    # `Object.assign(window, { a, b, c })` - the compact form, one entry per line
    for block in re.findall(r'Object\.assign\(\s*window\s*,\s*\{(.*?)\}\s*\)', js, re.S):
        names.update(re.findall(r'(\w+)\s*[,:}]', block))
    return names


def reachable_functions(js):
    """Functions an inline handler could actually call.

    Inline handlers resolve against the global scope. A classic script puts its
    declarations there; a module does not, so there the only reachable names are
    the ones assigned to `window`.
    """
    return defined_functions(js) | window_exports(js)


def fetch_paths(js, html=""):
    """Local API paths the page calls, without query strings."""
    return set(re.findall(r"""fetch\(\s*['"](/[^'"?]+)""", js + html))
