"""Keep the test suite out of the observatory's log.

Importing h1_web_scheduler attaches its console and file handlers to the
scheduler, sun_scan and horizon_scan loggers, so any test that runs a demo scan
writes "Az 40 deg: edge ..." lines straight into the live scheduler.log,
interleaved with real telescope operations. That has already cost time twice:
once diagnosing a phantom "scheduled observation preempts" during a real run,
and once when a progress monitor read the tests' demo azimuths as measurements
and reported 41 fitted edges out of 20.

The log is an operational record of what the telescope did. Tests do not
belong in it.
"""
import logging

import pytest


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
