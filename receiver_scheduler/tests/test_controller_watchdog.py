"""The scheduler's watch on the controller: a restart is logged with the
controller's own record of the previous boot, and a tracked observation whose
target the controller has lost is re-pointed (2026-09-30: tracking stopped at
14:09, the controller rebooted at 14:35, and a 'Sun track' recorded blank sky)."""
import logging

import pytest

import h1_web_scheduler as S


class _Alive:
    def poll(self):
        return None


@pytest.fixture
def watch(monkeypatch):
    calls = []
    state = {"status": {"uptime_s": 500, "boot": 3, "reset_reason": "power-on", "loop_age_ms": 5},
             "tracking": {"enabled": True},
             "diag": {"previous_boot": {"uptime_s": 1560, "last_stage": "tracking: sending the target",
                                        "events": ["13:09:01 Tracking enabled"]}}}

    def fake_api(endpoint, params=None, **kw):
        calls.append(endpoint)
        return {"/status": state["status"], "/tracking": state["tracking"], "/diag": state["diag"]}.get(endpoint)

    pointed = []
    monkeypatch.setattr(S, "srt_api_call", fake_api)
    monkeypatch.setattr(S, "srt_point_telescope", lambda obs: pointed.append(obs["name"]) or True)
    monkeypatch.setattr(S, "SRT_CONTROLLER_URL", "http://controller.test")
    monkeypatch.setattr(S, "_controller_watch", {"last": 0.0, "boot": None, "uptime": None, "stall_logged": False})
    monkeypatch.setattr(S, "current_process", _Alive())
    monkeypatch.setattr(S, "current_observation", {"name": "Sun monitor", "coord_system": "object"})

    def tick():
        S._controller_watch["last"] = 0.0
        S.controller_watchdog()
    return state, calls, pointed, tick


def test_a_restart_is_logged_with_the_previous_boot(watch, caplog):
    state, calls, pointed, tick = watch
    tick()
    state["status"] = dict(state["status"], uptime_s=20, boot=4, reset_reason="task watchdog")
    with caplog.at_level(logging.ERROR, logger="scheduler"):
        tick()
    text = caplog.text
    assert "restarted (reset by task watchdog" in text
    assert "last loop stage 'tracking: sending the target'" in text
    assert "Tracking enabled" in text
    assert "/diag" in calls


def test_a_lost_track_is_re_pointed_and_a_kept_one_is_not(watch):
    state, calls, pointed, tick = watch
    tick()
    assert pointed == []
    state["tracking"] = {"enabled": False}
    tick()
    assert pointed == ["Sun monitor"]


def test_a_drift_scan_is_never_re_pointed(watch, monkeypatch):
    state, calls, pointed, tick = watch
    monkeypatch.setattr(S, "current_observation", {"name": "Cas A drift", "coord_system": "altaz"})
    state["tracking"] = {"enabled": False}
    tick()
    assert pointed == [] and "/tracking" not in calls


def test_pulsar_mode_counts_as_tracked():
    assert S._observation_is_tracked({"coord_system": "pulsar"})
    assert S._observation_is_tracked({"coord_system": "radec"})
    assert not S._observation_is_tracked({"coord_system": "altaz"})
    assert not S._observation_is_tracked({"coord_system": "satellite"})
    assert not S._observation_is_tracked(None)


def test_a_stuck_loop_is_logged_once(watch, caplog):
    state, calls, pointed, tick = watch
    state["status"] = dict(state["status"], loop_age_ms=25000)
    with caplog.at_level(logging.ERROR, logger="scheduler"):
        tick()
        tick()
    assert caplog.text.count("loop has not run") == 1


def test_a_panic_backtrace_is_logged_even_without_an_elf(watch, caplog, tmp_path):
    state, calls, pointed, tick = watch
    tick()
    state["status"] = dict(state["status"], uptime_s=20, boot=4, reset_reason="panic (exception or abort)")
    state["diag"]["previous_boot"]["panic"] = {"reason": "LoadProhibited", "kind": "fault", "core": 1,
                                               "addr": "0x400d1234", "backtrace": ["0x400d1234", "0x400d5678"]}
    with caplog.at_level(logging.ERROR, logger="scheduler"):
        tick()
    assert "panic: LoadProhibited (fault, core 1) at 0x400d1234" in caplog.text
    assert "0x400d1234" in caplog.text and "0x400d5678" in caplog.text


def test_the_backtrace_decoder_says_when_it_cannot_decode(tmp_path):
    out = S.decode_controller_backtrace(["0x400d1234"], elf=str(tmp_path / "none.elf"))
    assert out == ["backtrace 0x400d1234 (no ELF to decode it against)"]
    assert S.decode_controller_backtrace([]) == []


def test_a_recovered_miss_on_the_alias_is_counted_not_logged(monkeypatch, caplog):
    """192.168.50.120 timing out and srt-controller.local answering is one
    controller by two names: nothing in the log, one retry counted."""
    import urllib.error

    class _Resp:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=3):
        if "192.168.50.120" in req.full_url:
            raise urllib.error.URLError("timed out")
        return _Resp(b'{"ok": true}')

    monkeypatch.setattr(S.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(S, "_controller_url_candidates",
                        lambda: ["http://192.168.50.120", "http://srt-controller.local"])
    monkeypatch.setattr(S, "SRT_CONTROLLER_URL", "http://192.168.50.120")
    monkeypatch.setattr(S, "_controller_link", {"down": False, "retries": 0, "hour": None})
    with caplog.at_level(logging.DEBUG, logger="scheduler"):
        assert S.srt_api_call("/status") == {"ok": True}
    assert "reachable" not in caplog.text and "SRT API error" not in caplog.text
    assert S._controller_link["retries"] == 1


def test_a_total_failure_warns_once_and_recovery_says_so(monkeypatch, caplog):
    import urllib.error
    state = {"up": False}

    class _Resp:
        def read(self): return b'{"ok": true}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=3):
        if not state["up"]:
            raise urllib.error.URLError("timed out")
        return _Resp()

    monkeypatch.setattr(S.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(S, "_controller_url_candidates", lambda: ["http://192.168.50.120"])
    monkeypatch.setattr(S, "SRT_CONTROLLER_URL", "http://192.168.50.120")
    monkeypatch.setattr(S, "_controller_link", {"down": False, "retries": 0, "hour": None})
    with caplog.at_level(logging.INFO, logger="scheduler"):
        assert S.srt_api_call("/status") is None
        assert S.srt_api_call("/status") is None
        state["up"] = True
        assert S.srt_api_call("/status") == {"ok": True}
    assert caplog.text.count("SRT connection error") == 1
    assert "reachable again at http://192.168.50.120" in caplog.text


def test_the_flashed_elf_is_kept_for_panic_decoding(tmp_path, monkeypatch):
    fw = tmp_path / "fw"
    (fw / ".pio" / "build" / "wt32-eth01").mkdir(parents=True)
    (fw / ".pio" / "build" / "wt32-eth01" / "firmware.elf").write_bytes(b"\x7fELF test")
    monkeypatch.setattr(S, "ESP32_FIRMWARE_DIR", str(fw))
    monkeypatch.setattr(S, "CONTROLLER_ELF", str(tmp_path / "kept" / "current.elf"))
    assert S.keep_controller_elf("wt32-eth01") == str(tmp_path / "kept" / "current.elf")
    assert (tmp_path / "kept" / "current.elf").read_bytes() == b"\x7fELF test"
    assert len(list((tmp_path / "kept").glob("firmware_*.elf"))) == 1
    assert S.keep_controller_elf("no-such-env") is None
