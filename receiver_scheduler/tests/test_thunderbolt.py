"""The Thunderbolt's TSIP status and the clock panel, before the unit is
connected: packets are built here from the documented layouts, and a
pseudo-terminal stands in for the serial port."""
import math
import os
import struct
import time
from unittest.mock import patch

import pytest

import clocks
import h1_web_scheduler as sched
import thunderbolt as tb


@pytest.fixture
def client():
    sched.app.config["TESTING"] = True
    with sched.app.test_client() as c:
        yield c


def primary(time_set=True, utc=True):
    flags = (0x01 if utc else 0) | (0x02 if utc else 0) | (0 if time_set else 0x04)
    return tb.encode(0x8F, bytes([0xAB]) + struct.pack(">IHhBBBBBBH", 216000, 2386, 18, flags,
                                                         5, 4, 3, 29, 9, 2026))


def supplemental(mode=0, crit=0, minor=0, holdover=0, osc=-0.012, pps=3.5, dac_v=0.1234, temp=31.5,
                 activity=0):
    body = struct.pack(">BBBIHHBBBBffIffddd", 7, mode, 100, holdover, crit, minor, 0, activity, 0, 0,
                       pps, osc, 32768, dac_v, temp, math.radians(55.9024), math.radians(-4.3079), 52.0)
    return tb.encode(0x8F, bytes([0xAC]) + body + bytes(8))


def satellites(prns=(3, 7, 16, 21)):
    return tb.encode(0x6D, bytes([len(prns) << 4 | 4]) + struct.pack(">ffff", 1.9, 1.1, 1.5, 0.9) + bytes(prns))


# ---------------------------------------------------------------------------
# framing


def test_a_packet_with_dle_bytes_in_it_comes_back_whole():
    payload = bytes([0xAC, 0x10, 0x00, 0x10, 0x10, 0x03, 0x10])
    wire = tb.encode(0x8F, payload)
    assert wire.count(b"\x10\x10") >= 3                  # every 0x10 in the data is doubled
    assert tb.Framer().feed(wire) == [(0x8F, payload)]


def test_packets_split_anywhere_across_reads_are_reassembled():
    wire = primary() + supplemental() + satellites()
    for cut in range(1, len(wire)):
        f = tb.Framer()
        got = f.feed(wire[:cut]) + f.feed(wire[cut:])
        assert [p[0] for p in got] == [0x8F, 0x8F, 0x6D], cut


def test_starting_mid_packet_loses_that_packet_and_misreads_nothing():
    """The port is opened at an arbitrary moment: the tail of a packet is
    garbage to be skipped, not the start of a packet."""
    wire = supplemental()
    f = tb.Framer()
    got = f.feed(wire[20:] + primary())
    assert [(pid, p[0]) for pid, p in got] == [(0x8F, 0xAB)]
    assert f.dropped > 0


def test_a_packet_that_lost_its_end_is_not_glued_to_the_next():
    wire = supplemental()[:-2] + primary()               # DLE ETX of the first lost
    got = tb.Framer().feed(wire)
    assert [(pid, p[0]) for pid, p in got] == [(0x8F, 0xAB)]


# ---------------------------------------------------------------------------
# decoding and judging


def _one(wire):
    (pid, payload), = tb.Framer().feed(wire)
    return tb.decode(pid, payload)


def test_the_primary_timing_packet_decodes():
    kind, f = _one(primary())
    assert kind == "primary"
    assert f["time"] == "2026-09-29 03:04:05" and f["utc_offset_s"] == 18 and f["utc"] and f["time_set"]


def test_the_supplemental_timing_packet_decodes():
    kind, f = _one(supplemental(minor=1 << 7, holdover=0))
    assert kind == "supplemental"
    assert f["disciplining_mode_text"] == "normal" and f["receiver_mode_text"] == "overdetermined clock"
    assert f["osc_offset_ppb"] == pytest.approx(-0.012) and f["pps_offset_ns"] == pytest.approx(3.5)
    assert f["dac_v"] == pytest.approx(0.1234) and f["temperature_c"] == pytest.approx(31.5)
    assert f["lat_deg"] == pytest.approx(55.9024) and f["lon_deg"] == pytest.approx(-4.3079)
    assert f["minor_alarms_text"] == ["leap second pending"]


def test_satellites_decode():
    kind, f = _one(satellites())
    assert kind == "satellites" and f["n_sats"] == 4 and f["prns"] == [3, 7, 16, 21]


@pytest.mark.parametrize("kw, level, locked, words", [
    (dict(), "ok", True, "locked"),
    (dict(minor=1 << 7), "warn", True, "leap second pending"),
    (dict(minor=1 << 0), "warn", True, "control voltage near rail"),
    (dict(mode=2, holdover=5400), "warn", False, "auto holdover 1h30m"),
    (dict(mode=1), "warn", False, "power-up"),
    (dict(minor=1 << 1), "bad", False, "antenna open"),
    (dict(minor=1 << 4, mode=0), "bad", False, "not disciplining"),
    (dict(crit=1 << 4), "bad", False, "control voltage at rail"),
])
def test_the_assessment_says_whether_to_trust_the_outputs(kw, level, locked, words):
    _, f = _one(supplemental(**kw))
    got = tb.assess(f)
    assert got[0] == level and got[2] is locked and words in got[1]


def test_silence_is_stale_and_nothing_is_absent():
    _, f = _one(supplemental())
    assert tb.assess(f, age_s=tb.STALE_S + 1)[0] == "stale"
    assert tb.assess(None)[0] == "absent"


# ---------------------------------------------------------------------------
# the monitor


def _fed(*wires, now=100.0, wall=1.79e9):
    m = tb.Monitor("/dev/null")
    for w in wires:
        for pid, payload in tb.Framer().feed(w):
            m.handle(pid, payload, now=now, wall=wall)
    return m


def test_the_monitor_reports_and_notes_changes():
    changes = []
    m = tb.Monitor("/dev/null", on_change=lambda lv, text: changes.append(lv))
    for w, t in ((supplemental(), 1), (supplemental(), 2), (supplemental(mode=2, holdover=60), 3),
                 (supplemental(), 4)):
        for pid, payload in tb.Framer().feed(w):
            m.handle(pid, payload, now=t, wall=1.79e9 + t)
    assert changes == ["warn", "ok"]                     # the first state is not a change
    assert m.status(now=4.5)["assessment"][0] == "ok"
    assert m.status(now=4 + tb.STALE_S + 1)["assessment"][0] == "stale"
    assert len(m.history_points()) == 4


def test_a_recording_carries_the_reference_state():
    m = _fed(supplemental(osc=0.002), satellites())
    a = tb.recording_attrs(m)
    s = m.status(now=100.5)
    assert s["assessment"][0] == "ok"
    with patch.object(time, "monotonic", return_value=100.5):
        a = tb.recording_attrs(m)
    assert a["reference_state"] == "ok" and a["reference_locked"] is True
    assert a["reference_osc_offset_ppb"] == pytest.approx(0.002) and a["reference_n_sats"] == 4
    assert all(isinstance(v, (bool, int, float, str)) for v in a.values())   # plain, for HDF5 attrs
    assert tb.recording_attrs(None) == {"reference_state": "absent"}


def test_the_monitor_reads_a_serial_port():
    """End to end through open_port and the reader thread, a pseudo-terminal
    standing in for the RS-232 adapter."""
    master, slave = os.openpty()
    path = os.ttyname(slave)
    m = tb.Monitor(path).start()
    try:
        deadline = time.time() + 5
        while not m.connected and time.time() < deadline:
            time.sleep(0.05)
        assert m.connected
        os.write(master, primary() + supplemental() + satellites())
        while m.packets < 3 and time.time() < deadline:
            time.sleep(0.05)
        s = m.status()
        assert s["assessment"][0] == "ok" and s["satellites"]["n_sats"] == 4
    finally:
        m.stop()
        os.close(master)
        os.close(slave)


def test_a_missing_device_is_reported_not_raised():
    with patch.object(tb, "RETRY_S", 0.05):
        m = tb.Monitor("/dev/no-such-thunderbolt").start()
        time.sleep(0.3)
        m.stop()
    c = clocks.thunderbolt_clock(m)
    assert c["level"] == "absent" and "no-such-thunderbolt" in c["text"]


# ---------------------------------------------------------------------------
# the other two clocks, and the endpoint

SHOW = "NTP=yes\nNTPSynchronized=yes\nTimezone=Europe/London\n"
TIMESYNC = ("ServerName=ntp.ubuntu.com\nPollIntervalUSec=34min 8s\n"
            "NTPMessage={ Leap=0, Version=4, Mode=4, Stratum=2, Precision=-25, RootDelay=6.500ms, "
            "RootDispersion=213us, Reference=1D586304, PacketCount=1727, Jitter=469us }\n")


def test_this_computers_ntp_state_is_read_from_timedatectl():
    h = clocks.parse_timedatectl(SHOW, TIMESYNC)
    assert h["synchronized"] and h["stratum"] == 2 and h["server"] == "ntp.ubuntu.com"
    assert h["root_distance_ms"] == pytest.approx(3.25 + 0.213)
    assert h["jitter_ms"] == pytest.approx(0.469)


def test_this_computer_unsynchronised_is_bad():
    class R:
        def __init__(self, out): self.stdout = out
    got = clocks.host_clock(run=lambda cmd, **kw: R(SHOW.replace("NTPSynchronized=yes", "NTPSynchronized=no")
                                                     if cmd[1] == "show" else TIMESYNC))
    assert got["level"] == "bad"


@pytest.mark.parametrize("status, level", [
    (dict(sync_state="ok", last_sync_age_s=600, timestamp=1000, source="NTP", sync_count=4), "ok"),
    (dict(sync_state="stale", last_sync_age_s=20000, timestamp=1000), "warn"),
    (dict(sync_state="unverified", last_sync_age_s=-1, timestamp=1000), "warn"),
    (dict(sync_state="never", last_sync_age_s=-1, timestamp=1000), "bad"),
    (dict(sync_state="ok", last_sync_age_s=60, timestamp=1005), "bad"),     # 5 s out
    (None, "absent"),
])
def test_the_controller_clock_is_judged(status, level):
    assert clocks.controller_clock(status, host_now=1000.4)["level"] == level


def test_the_clock_endpoint(client):
    import h1_web_scheduler as sched
    ok_host = {"level": "ok", "text": "NTP"}
    sched._clock_cache.clear()
    m = _fed(supplemental(mode=2, holdover=120))
    with patch.object(sched, "_thunderbolt", m), \
         patch.object(clocks, "host_clock", return_value=ok_host), \
         patch.object(sched, "srt_api_call", return_value=dict(sync_state="ok", last_sync_age_s=30,
                                                               timestamp=int(time.time()))), \
         patch.object(time, "monotonic", return_value=100.5):
        d = client.get("/api/clock?history=1").get_json()
    sched._clock_cache.clear()
    assert d["thunderbolt"]["level"] == "warn" and "holdover" in d["thunderbolt"]["text"]
    assert d["overall"] == "warn" and len(d["history"]) == 1
    # Not connected is a fact, not a fault: it does not colour the banner.
    with patch.object(sched, "_thunderbolt", None), \
         patch.object(clocks, "host_clock", return_value=ok_host), \
         patch.object(sched, "srt_api_call", return_value=dict(sync_state="ok", last_sync_age_s=30,
                                                               timestamp=int(time.time()))):
        d = client.get("/api/clock").get_json()
    sched._clock_cache.clear()
    assert d["thunderbolt"]["level"] == "absent" and d["overall"] == "ok"
