"""The observing state, pinned before it is rearranged.

Written 2026-08-25, ahead of moving the scheduler's module-level state into one
object so the routes can be split into blueprints. The existing lifecycle tests
already check that starting an observation starts one; what none of them check
is that the half-dozen names describing *whether* an observation is running
agree with each other afterwards.

That agreement is exactly what a state move breaks. Convert a write and miss
the matching read and the two drift apart silently: `current_observation` says
a scan is running while `current_process` says nothing is, and the scheduler
either refuses to start anything ever again or starts a second observation on
top of the first. Neither announces itself, and both need the mount to notice.

So these assert the whole tuple at once, at each transition, rather than one
name at a time. The lesson is from earlier the same day: four structural guards
passed while the page's log tab was quietly broken, because they checked shape
and not meaning. These check meaning.
"""

from unittest.mock import MagicMock, patch

import pytest

import h1_web_scheduler as sched

# The names that together say whether an observation is running. Any refactor
# that separates them has broken something, whichever way round.
IDLE = {"current_process": None, "current_observation": None,
        "observation_end_time": None, "observation_starting": False,
        "starting_observation_name": ""}


def snapshot():
    return {name: getattr(sched, name) for name in IDLE}


def assert_idle(where):
    now = snapshot()
    assert now == IDLE, "%s: state should be fully idle, got %s" % (where, now)


def assert_running(name, where):
    now = snapshot()
    assert now["current_process"] is not None, "%s: no process" % where
    assert now["current_observation"] is not None, "%s: no observation" % where
    assert now["current_observation"]["name"] == name, where
    assert now["observation_end_time"] is not None, "%s: no end time" % where
    # The starting flags are a door, not a record: they guard the window
    # between claiming a start and completing it, and must be clear once the
    # observation is actually running or a second start is refused forever.
    assert now["observation_starting"] is False, "%s: still marked starting" % where
    assert now["starting_observation_name"] == "", "%s: stale starting name" % where


OBS = {"name": "State test", "coord_system": "altaz",
       "coord1_deg": 45, "coord1_min": 0, "coord1_sec": 0,
       "coord2_deg": 180, "coord2_min": 0, "coord2_sec": 0,
       "center_freq_mhz": 1420.405, "channels": 4096, "integration_time_s": 3.0,
       "sdr_type": "demo", "gain_db": 40, "bandwidth_mhz": 2.4,
       "calibrator": False, "duration_minutes": 10}


@pytest.fixture
def idle(tmp_path, monkeypatch):
    """Put the module in the idle state, and put it back afterwards.

    Also points the last-observation pointer at a temporary file. These tests
    run the real start_observation and stop_observation, and stopping one
    writes that pointer - so without this they overwrite the observatory's
    record of its own last run. They did, on 2026-08-25: the Observe tab was
    left offering a plot of "State test" at /tmp/state_test.h5, from a unit
    test, while the real solar track sat unreferenced on disk.

    Nothing under test writes anywhere else persistent, but this is the shape
    of thing to check before adding to this file.
    """
    monkeypatch.setattr(sched, "LAST_OBSERVATION_FILE",
                        str(tmp_path / "last_observation.json"))
    saved = snapshot()
    saved_last = sched.last_observation
    for name, value in IDLE.items():
        setattr(sched, name, value)
    yield
    for name, value in saved.items():
        setattr(sched, name, value)
    sched.last_observation = saved_last


@pytest.fixture
def running_proc():
    proc = MagicMock()
    proc.poll.return_value = None
    return proc


def test_every_name_agrees_that_nothing_is_running(idle):
    assert_idle("at rest")


def test_every_name_agrees_after_a_start(idle, running_proc):
    with patch.object(sched, "SRT_CONTROLLER_URL", None), \
         patch("subprocess.Popen", return_value=running_proc), \
         patch.object(sched, "generate_filename", return_value="/tmp/state_test.h5"):
        assert sched.start_observation(dict(OBS)) is True
    assert_running("State test", "after start")


def test_a_stopped_booking_marks_its_slot_finished(idle, running_proc):
    """Stopping a booked run retires its slot (issue #25): the scheduler
    thread must not start it again while the slot is still due. A Run Now
    observation carries no slot and marks nothing."""
    booked = dict(OBS, start_date="2026-08-26", start_time="14:00")
    sched.finished_slots.clear()
    try:
        with patch.object(sched, "SRT_CONTROLLER_URL", None), \
             patch("subprocess.Popen", return_value=running_proc), \
             patch.object(sched, "generate_filename", return_value="/tmp/state_test.h5"):
            assert sched.start_observation(booked) is True
        running_proc.poll.return_value = 0
        with patch.object(sched, "SRT_CONTROLLER_URL", None):
            sched.stop_observation()
        assert sched._slot_key(booked) in sched.finished_slots

        sched.finished_slots.clear()
        running_proc.poll.return_value = None
        with patch.object(sched, "SRT_CONTROLLER_URL", None), \
             patch("subprocess.Popen", return_value=running_proc), \
             patch.object(sched, "generate_filename", return_value="/tmp/state_test.h5"):
            assert sched.start_observation(dict(OBS)) is True      # no slot
        running_proc.poll.return_value = 0
        with patch.object(sched, "SRT_CONTROLLER_URL", None):
            sched.stop_observation()
        assert not sched.finished_slots
    finally:
        sched.finished_slots.clear()


def test_every_name_agrees_again_after_a_stop(idle, running_proc):
    """The one that matters most.

    A stop that clears some names and not others is how the scheduler ends up
    unable to start anything: current_observation still holds the finished
    observation, so every later start is refused as a clash, and the only
    symptom is a night with no data.
    """
    with patch.object(sched, "SRT_CONTROLLER_URL", None), \
         patch("subprocess.Popen", return_value=running_proc), \
         patch.object(sched, "generate_filename", return_value="/tmp/state_test.h5"):
        sched.start_observation(dict(OBS))
    assert_running("State test", "after start")

    running_proc.poll.return_value = 0
    with patch.object(sched, "SRT_CONTROLLER_URL", None):
        sched.stop_observation()
    assert_idle("after stop")


def test_a_start_and_stop_cycle_leaves_no_residue(idle, running_proc):
    """Twice round, because state that leaks does it cumulatively."""
    for run in range(3):
        running_proc.poll.return_value = None
        with patch.object(sched, "SRT_CONTROLLER_URL", None), \
             patch("subprocess.Popen", return_value=running_proc), \
             patch.object(sched, "generate_filename", return_value="/tmp/state_test.h5"):
            assert sched.start_observation(dict(OBS)) is True, "cycle %d" % run
        assert_running("State test", "cycle %d" % run)
        running_proc.poll.return_value = 0
        with patch.object(sched, "SRT_CONTROLLER_URL", None):
            sched.stop_observation()
        assert_idle("cycle %d" % run)


def test_a_failed_start_leaves_the_state_idle(idle):
    """A start that raises must not leave the door flag set.

    observation_starting is what stops two observations beginning at once. If
    an exception on the way up leaves it True, the scheduler is wedged: every
    later start sees a start already in progress and declines, forever, with
    nothing running.
    """
    with patch.object(sched, "SRT_CONTROLLER_URL", None), \
         patch("subprocess.Popen", side_effect=OSError("no receiver")), \
         patch.object(sched, "generate_filename", return_value="/tmp/state_test.h5"):
        try:
            sched.start_observation(dict(OBS))
        except OSError:
            pass
    assert sched.observation_starting is False, \
        "a failed start left the scheduler unable to start anything again"
    assert sched.starting_observation_name == ""


def test_the_state_names_are_all_present(idle):
    """Guards the move itself.

    If the state is relocated into another module, these names must either
    follow or be deliberately retired - what must not happen is one of them
    quietly remaining here as a stale copy that nothing updates any more. A
    read of a stale module global is silent; that is the whole hazard.
    """
    for name in IDLE:
        assert hasattr(sched, name), (
            "%s has gone. If the state moved, this test should move with it - "
            "not be deleted." % name)


def test_a_due_booking_is_not_mistaken_for_its_running_namesake():
    """The scheduler thread's "already running" test, by identity.

    The simulator names entries by target, so a scan started now and one due
    later share a name. Matched by name, the due one was taken to be already
    running and never started.
    """
    running = {"name": "Drift scan l=184.6 b=-5.8",
               "start_date": "2026-08-26", "start_time": "11:39"}
    twin = {"name": "Drift scan l=184.6 b=-5.8",
            "start_date": "2026-08-27", "start_time": "06:31"}
    assert sched._same_booking(running, running) is True
    assert sched._same_booking(running, twin) is False, "a different booking"
    assert sched._same_booking(dict(running, start_date="", start_time=""), twin) is True, \
        "a run that recorded no start can only be matched by name"
    assert sched._same_booking(running, {"name": "Other"}) is False


def test_a_manual_start_while_recording_is_refused_and_says_what_is_in_the_way(idle, running_proc):
    """Refused, not queued, not preempting - and the message names the run."""
    with patch.object(sched, "SRT_CONTROLLER_URL", None), \
         patch("subprocess.Popen", return_value=running_proc), \
         patch.object(sched, "generate_filename", return_value="/tmp/state_test.h5"):
        assert sched.start_observation(dict(OBS)) is True
    try:
        sched.app.config["TESTING"] = True
        with sched.app.test_client() as client:
            r = client.post("/api/start", json=dict(OBS, name="Second"))
        assert r.status_code == 409
        assert "State test" in r.get_json()["error"]
        assert "already recording" in r.get_json()["error"]
        assert_running("State test", "the first run must be untouched")
    finally:
        running_proc.poll.return_value = 0
        with patch.object(sched, "SRT_CONTROLLER_URL", None):
            sched.stop_observation()
