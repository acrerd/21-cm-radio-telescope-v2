"""Only one thing may own the SDR and the mount at a time.

Five subsystems compete for the same hardware: a scheduled observation, a Sun
scan, a calibration day, a horizon scan and an RF calibration. Each start path
grew its own list of what it refuses to run alongside, one addition at a time,
and the lists drifted apart - so whether two of them can run together depended
on which was started first, and nobody had ever written the matrix down.

The gaps found on 2026-08-25, all of them one-directional:

    a Sun scan would start while a horizon scan was running
    a Sun scan would start while an RF calibration was running
    a calibration day would start while either was running
    a horizon scan would start while an RF calibration was running

The reverse was refused in every case, which is why none of this had shown up:
starting them in the habitual order works. The horizon scan drives the mount
for two hours, so a Sun scan begun alongside it would raster wherever the
horizon scan had just moved to, and both would claim the B210. The Sun scan
would fail on the device, the horizon scan would record whatever the mount
happened to be pointing at, and the profile would be quietly wrong.

This tests the matrix as a matrix, so a new subsystem has one obvious place to
declare itself rather than four lists to be added to and one to be forgotten.
"""

from unittest.mock import MagicMock, patch

import pytest

import h1_web_scheduler as sched

# Every way of claiming the hardware: how to mark it busy, and how to ask for it.
SUBSYSTEMS = {
    "sun scan":        {"flag": "sun_scan_state",
                        "start": ("/api/sunscan/start", {"n": 5, "grid_spacing_deg": 1.5})},
    "calibration day": {"flag": "cal_day_state",
                        "start": ("/api/calday/start", {"n": 5, "grid_spacing_deg": 1.5,
                                                        "interval_minutes": 30})},
    "horizon scan":    {"flag": "horizon_state", "start": ("/api/horizon/start", {})},
    "RF calibration":  {"flag": "rf_state", "start": ("/api/rf/run", {"job": "gain"})},
}


@pytest.fixture
def client():
    sched.app.config["TESTING"] = True
    with sched.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def quiet_hardware():
    """Nothing in this file may start a thread, a process or a slew.

    A missing guard means the endpoint runs on rather than refusing, so without
    this the test for the bug would be the bug: a real Sun scan thread, driving
    a real mount, from a unit test.
    """
    saved = {name: dict(getattr(sched, name)) for name in
             ("sun_scan_state", "cal_day_state", "horizon_state", "rf_state")}
    THREADS = ("sun_scan_thread", "cal_day_thread", "horizon_thread", "rf_thread")
    saved_threads = {n: getattr(sched, n, None) for n in THREADS}
    saved_proc = sched.current_process
    saved_obs = sched.current_observation
    for name in saved:
        getattr(sched, name)["running"] = False
    for name in THREADS:
        setattr(sched, name, None)
    sched.current_process = None
    sched.current_observation = None

    with patch.object(sched, "threading", MagicMock(wraps=sched.threading)) as th, \
         patch.object(sched, "receiver_status_snapshot",
                      return_value={"running": False, "source": None}):
        th.Thread = MagicMock()
        # A mocked thread must look *stopped*. Left as a bare MagicMock its
        # is_alive() returns a truthy mock, which trips the "already running"
        # guard in several start paths - so an endpoint that should have been
        # allowed through is refused, and a test looking for a refusal passes
        # without ever reaching the guard it means to check. That masked three
        # real holes on the first run of this file.
        th.Thread.return_value.is_alive.return_value = False
        th.Lock = sched.threading.Lock
        th.Event = sched.threading.Event
        yield
    for name, value in saved.items():
        getattr(sched, name).clear()
        getattr(sched, name).update(value)
    for name, value in saved_threads.items():
        setattr(sched, name, value)
    sched.current_process = saved_proc
    sched.current_observation = saved_obs


def _refused(response):
    """True if the endpoint declined to take the hardware."""
    if response.status_code >= 400:
        return True
    body = response.get_json() or {}
    return body.get("success") is False


@pytest.mark.parametrize("busy", sorted(SUBSYSTEMS))
@pytest.mark.parametrize("asking", sorted(SUBSYSTEMS))
def test_one_subsystem_at_a_time(client, busy, asking, quiet_hardware):
    """Every pair, both ways round.

    Including a subsystem against itself, which every start path already
    handled - it is the cross terms that had drifted.
    """
    getattr(sched, SUBSYSTEMS[busy]["flag"])["running"] = True
    path, payload = SUBSYSTEMS[asking]["start"]
    resp = client.post(path, json=payload)
    assert _refused(resp), (
        "%s was allowed to start while a %s was running; both would own the "
        "B210 and the mount" % (asking, busy))


@pytest.mark.parametrize("asking", sorted(SUBSYSTEMS))
def test_nothing_starts_while_an_observation_holds_the_receiver(client, asking,
                                                                quiet_hardware):
    """A scheduled observation outranks all four.

    It is the only one with data someone is waiting for, and the only one whose
    slot cannot simply be re-run later.
    """
    proc = MagicMock()
    proc.poll.return_value = None
    sched.current_process = proc
    sched.current_observation = {"name": "Holding the receiver"}
    path, payload = SUBSYSTEMS[asking]["start"]
    assert _refused(client.post(path, json=payload)), (
        "%s was allowed to start while an observation was recording" % asking)


def test_each_subsystem_can_start_when_nothing_else_holds_the_hardware(client,
                                                                      quiet_hardware):
    """The other half of the invariant.

    A guard that refuses everything satisfies the tests above perfectly and is
    useless, so this is here to keep them honest.
    """
    for name, spec in sorted(SUBSYSTEMS.items()):
        for other in SUBSYSTEMS.values():
            getattr(sched, other["flag"])["running"] = False
        path, payload = spec["start"]
        resp = client.post(path, json=payload)
        assert not _refused(resp), (
            "%s refused to start with nothing else running: %s"
            % (name, (resp.get_json() or {}).get("error")))
