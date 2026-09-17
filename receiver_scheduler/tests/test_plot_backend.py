"""Choosing a matplotlib backend without stealing it from a notebook.

matplotlib has **one backend per process**, so a module that calls
`matplotlib.use("Agg")` on import forces it on whatever imported it. The
scheduler needs Agg — the observatory is worked over ssh and an import that
wants a display dies. A Jupyter kernel needs its own inline backend, and losing
it is silent: figures are drawn, rendered nowhere, and no error is raised.

That is not hypothetical. On 2026-09-17 a notebook cell plotting the tracking
scallop's pointing error printed its numbers and drew nothing, while the two
plotting cells above it were fine. `import scallop` in the cell between them
was the cause: scallop -> drift_park -> sun_scan -> matplotlib.use("Agg").
Every plot from there to the end of the notebook was lost.

These guard both halves: headless still gets Agg, a kernel keeps what it had.
"""
import subprocess
import sys

import pytest

import plot_backend

RS = __import__("os").path.dirname(__import__("os").path.dirname(
    __import__("os").path.abspath(__file__)))


class TestDetectingAKernel:
    def test_no_ipython_at_all_is_not_a_kernel(self, monkeypatch):
        monkeypatch.delitem(sys.modules, "IPython", raising=False)
        assert plot_backend.in_notebook_kernel() is False

    def test_ipython_imported_but_no_shell_is_not_a_kernel(self, monkeypatch):
        """A library may import IPython without one running - a script, or
        pytest itself. Nothing owns the display in that case."""
        fake = type(sys)("IPython")
        fake.get_ipython = lambda: None
        monkeypatch.setitem(sys.modules, "IPython", fake)
        assert plot_backend.in_notebook_kernel() is False

    def test_a_terminal_shell_is_not_a_kernel(self, monkeypatch):
        """Plain `ipython` at a prompt has no inline backend to protect, so it
        should get the headless default like any other script."""
        fake = type(sys)("IPython")
        fake.get_ipython = lambda: type("TerminalInteractiveShell", (), {})()
        monkeypatch.setitem(sys.modules, "IPython", fake)
        assert plot_backend.in_notebook_kernel() is False

    def test_a_zmq_shell_is_a_kernel(self, monkeypatch):
        """Jupyter, JupyterLab and the VS Code notebook editor all run this."""
        fake = type(sys)("IPython")
        fake.get_ipython = lambda: type("ZMQInteractiveShell", (), {})()
        monkeypatch.setitem(sys.modules, "IPython", fake)
        assert plot_backend.in_notebook_kernel() is True

    def test_a_broken_get_ipython_is_not_a_kernel(self, monkeypatch):
        """Never raise out of a backend choice - it would break the import of
        every plotting module for the sake of a diagnostic."""
        fake = type(sys)("IPython")
        def boom():
            raise RuntimeError("no")
        fake.get_ipython = boom
        monkeypatch.setitem(sys.modules, "IPython", fake)
        assert plot_backend.in_notebook_kernel() is False


class TestUseHeadless:
    def test_it_declines_inside_a_kernel(self, monkeypatch):
        monkeypatch.setattr(plot_backend, "in_notebook_kernel", lambda: True)
        assert plot_backend.use_headless() is False

    def test_it_acts_outside_one(self, monkeypatch):
        monkeypatch.setattr(plot_backend, "in_notebook_kernel", lambda: False)
        assert plot_backend.use_headless() is True


# Subprocesses, because a backend is process-wide and these have to observe the
# state at import time, which cannot be undone inside the test process.
def _run(code):
    return subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, cwd=RS, timeout=180)


HEADLESS = """
import sys; sys.path.insert(0, %r)
import %s, matplotlib
print(matplotlib.get_backend())
"""

KERNEL = """
import sys; sys.path.insert(0, %r)
import matplotlib
matplotlib.use('module://matplotlib_inline.backend_inline')
import types
fake = types.ModuleType('IPython')
fake.get_ipython = lambda: type('ZMQInteractiveShell', (), {})()
sys.modules['IPython'] = fake
import %s, matplotlib
print(matplotlib.get_backend())
"""


@pytest.mark.parametrize("module", ["observation_plot", "sun_scan"])
def test_a_plain_script_still_gets_agg(module):
    """The observing path must never need a display (the scheduler runs over
    ssh), so this half must not regress while fixing the other."""
    out = _run(HEADLESS % (RS, module))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().lower() == "agg", out.stdout


@pytest.mark.parametrize("module", ["observation_plot", "sun_scan"])
def test_a_notebook_keeps_its_own_backend(module):
    """Importing anything from the reduction must not take the display away
    from the kernel that imported it."""
    out = _run(KERNEL % (RS, module))
    if "matplotlib_inline" in out.stderr:
        pytest.skip("matplotlib_inline not installed in this interpreter")
    assert out.returncode == 0, out.stderr
    assert "matplotlib_inline" in out.stdout, out.stdout


def test_the_chain_the_notebook_actually_walks():
    """scallop -> drift_park -> sun_scan, which is how it was found."""
    code = KERNEL % (RS, "scallop") + """
import numpy as np, scallop
scallop.drive_demand({'site_lat_deg': 55.9, 'site_lon_deg': -4.3,
                      'site_height_m': 50.0, 'object_name': 'sun'},
                     np.array([1789650000.0, 1789650003.0]), {'IE': -1.4})
print(matplotlib.get_backend())
"""
    out = _run(code)
    if "matplotlib_inline" in out.stderr or "ephem" in out.stderr:
        pytest.skip("matplotlib_inline or ephem not installed in this interpreter")
    assert out.returncode == 0, out.stderr
    assert all("matplotlib_inline" in line for line in out.stdout.split()), out.stdout
