"""Keep the test suite out of the observatory's records.

Importing h1_web_scheduler attaches its console and file handlers to the
scheduler, sun_scan and horizon_scan loggers, so any test that runs a demo scan
writes "Az 40 deg: edge ..." lines straight into the live scheduler.log,
interleaved with real telescope operations. That has already cost time twice:
once diagnosing a phantom "scheduled observation preempts" during a real run,
and once when a progress monitor read the tests' demo azimuths as measurements
and reported 41 fitted edges out of 20.

The log is an operational record of what the telescope did. Tests do not
belong in it.

The same goes for last_observation.json. Several tests run the real
stop_observation, which writes that pointer, so a test run left the Observe tab
offering a plot of "StopMe" or "State test" from /tmp while the observatory's
actual last run sat unreferenced on disk. Found on 2026-08-25, after a solar
track, by the operator wondering why the Last Observation panel was showing
something else.
"""
import logging
import os
import sys

import pytest

# The suite lives in tests/ but the modules under test are its parent
# (receiver_scheduler/). Put that on the path before any test imports them, so
# `import h1_web_scheduler` resolves under pytest and when a file is run
# directly. conftest is imported before the test modules in this directory, so
# this runs first.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True, scope="session")
def _keep_tests_out_of_the_scheduler_log():
    silenced = []
    for name in ("scheduler", "sun_scan", "horizon_scan"):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            silenced.append((logger, handler))
        logger.addHandler(logging.NullHandler())
    yield
    for logger, handler in silenced:
        logger.addHandler(handler)


@pytest.fixture(autouse=True, scope="session")
def _keep_tests_out_of_the_last_observation_pointer(tmp_path_factory):
    """Redirect the last-observation pointer for the whole session.

    Session-scoped and autouse rather than left to each test to remember,
    because the tests that write it do so through stop_observation rather than
    by touching the file - so it is not obvious from reading one that it needs
    isolating, and the next person to add such a test would not know either.
    """
    import h1_web_scheduler as scheduler

    real = scheduler.LAST_OBSERVATION_FILE
    scheduler.LAST_OBSERVATION_FILE = str(
        tmp_path_factory.mktemp("pointer") / "last_observation.json")
    yield
    scheduler.LAST_OBSERVATION_FILE = real


@pytest.fixture(autouse=True, scope="session")
def _keep_tests_out_of_the_horizon_partials(tmp_path_factory):
    """Redirect the horizon scan's partial saves for the whole session.

    A scan writes a partial profile after every strip, and the demo and
    stubbed scans in the tests do exactly what a real one does - so every
    pytest run left one or two horizon_partial_*.json in the real data
    folder. By 2026-08-26 there were 149, of which one came from the
    telescope. Same reasoning as the two fixtures above: the tests that write
    them do so through the scan, not by touching a file.
    """
    import horizon_scan

    # _partial_path builds its folder from horizon_scan._SCRIPT_DIR at call
    # time, and that name feeds nothing else after import - so pointing it at
    # a temporary folder moves the partials and nothing more. Tests that set
    # it themselves (monkeypatch) still win, since theirs is applied later.
    real = horizon_scan._SCRIPT_DIR
    horizon_scan._SCRIPT_DIR = str(tmp_path_factory.mktemp("horizon_partials"))
    yield
    horizon_scan._SCRIPT_DIR = real
