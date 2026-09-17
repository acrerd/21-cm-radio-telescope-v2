#!/usr/bin/env python3
"""Choosing a matplotlib backend without stealing it from a notebook.

The scheduler and the receiver run **headless** — the observatory is normally
worked over ssh — so anything in the observing path has to select a
non-interactive backend before it touches pyplot, or an import dies with
"could not connect to display".

The blunt way to do that is `matplotlib.use("Agg")` at module scope, and it has
a side effect nobody sees until they meet it: **matplotlib has one backend per
process**, so a module that forces Agg on import forces it on whatever imported
it. In a Jupyter kernel that silently replaces the inline backend, and every
figure drawn afterwards is rendered to a file nobody asked for and displayed
nowhere. No error, no warning, no output.

Found on 2026-09-17: a notebook cell that plots the tracking scallop's pointing
error produced its print output and no figure, while the two plotting cells
above it were fine. The difference was `import scallop` in the cell between
them — scallop pulls in `drift_park`, which pulls in `sun_scan`, which forced
Agg. Every plot from that cell to the end of the notebook was lost.

So: force the backend only when nothing else owns it.
"""
import sys


def in_notebook_kernel():
    """True inside a Jupyter/IPython kernel, which manages its own backend.

    `ZMQInteractiveShell` is the kernel behind Jupyter, JupyterLab and the
    VS Code notebook editor. A plain IPython terminal (`TerminalInteractiveShell`)
    and every ordinary script fall through to the headless default, which is
    what they want.
    """
    ipython = sys.modules.get("IPython")
    if ipython is None:                      # never imported: not a kernel
        return False
    try:
        shell = ipython.get_ipython()
    except Exception:                        # noqa: BLE001 - never worth raising for
        return False
    return shell is not None and type(shell).__name__ == "ZMQInteractiveShell"


def use_headless(default="Agg"):
    """Select `default` unless a notebook kernel is already driving the display.

    Returns True if the backend was changed. Call this *before* importing
    `matplotlib.pyplot`, the same as a bare `matplotlib.use` — the point is
    only that it declines to act when it would take the display away from
    somebody.
    """
    import matplotlib

    if in_notebook_kernel():
        return False
    matplotlib.use(default)
    return True
