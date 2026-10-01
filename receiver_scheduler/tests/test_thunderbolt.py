"""The Thunderbolt's TSIP status and the clock panel, before the unit is
connected: packets are built here from the documented layouts, and a
pseudo-terminal stands in for the serial port."""
import math
import os
import struct
import time
from unittest.mock import patch

import numpy as np
import pytest

import clocks
import h1_web_scheduler as sched
import thunderbolt as tb


@pytest.fixture
def client():
    sched.app.config["TESTING"] = True
    with sched.app.test_client() as c:
        yield c


def primary(time_set=True, utc=True, tow=216000):
    flags = (0x01 if utc else 0) | (0x02 if utc else 0) | (0 if time_set else 0x04)
    return tb.encode(0x8F, bytes([0xAB]) + struct.pack(">IHhBBBBBBH", tow, 2386, 18, flags,
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


def test_each_row_carries_the_units_own_second():
    """The 0x8F-AB just before a status packet gives that row the unit's
    second; a status packet with no AB of its own second gets None, never
    the previous second's."""
    m = tb.Monitor("/dev/null")
    for t, wires in ((1.0, (primary(tow=100), supplemental())),
                     (2.0, (supplemental(),)),
                     (3.0, (primary(tow=102), supplemental()))):
        for w in wires:
            for pid, payload in tb.Framer().feed(w):
                m.handle(pid, payload, now=t, wall=1.79e9 + t)
    assert [r[6] for r in m.rows()] == [2386 * 604800 + 100, None, 2386 * 604800 + 102]


def test_a_late_packet_does_not_break_the_stability_record():
    """2026-10-01 07:50: one packet handled 0.8 s late cut 5.6 h from the
    stability plot. The unit's seconds say nothing was missing; a second
    really missing is still a break."""
    wall = [1.79e9 + i for i in range(3000)]
    wall[1000] += 0.8
    unit = [5e5 + i for i in range(3000)]
    pps = np.random.default_rng(1).normal(0.0, 2.0, 3000)
    rows = [(w, 0.0, float(p), 2.11, 43.5, 0, u) for w, p, u in zip(wall, pps, unit)]
    assert clocks.contiguous_tail(wall) == 1001                       # by receipt time: a break
    assert clocks.stability(rows)["gap_trimmed"] == 0
    lost = rows[:2000] + [(r[0] + 1, *r[1:6], r[6] + 1) for r in rows[2000:]]   # one second never sent
    assert clocks.stability(lost)["gap_trimmed"] == 2000
    no_unit = [r[:6] for r in rows]                                   # rows from before the unit's second
    assert clocks.stability(no_unit)["gap_trimmed"] == 1001


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


def test_the_disciplining_parameters_decode_as_the_unit_sent_them():
    """The two replies the unit gave on 2026-09-30, byte for byte."""
    import thunderbolt as T
    kind, loop = T.decode(0x8F, bytes.fromhex("a800427000003f800000"))
    assert kind == "loop" and loop["time_constant_s"] == 60.0 and loop["damping"] == 1.0
    kind, osc = T.decode(0x8F, bytes.fromhex("a8013fc1fdb80000000040a00000"))
    assert kind == "oscillator" and abs(osc["efc_gain_hz_per_v"] - 1.516) < 1e-3
    assert (osc["dac_min_v"], osc["dac_max_v"]) == (0.0, 5.0)


def test_a_request_carries_only_the_type_byte():
    """A read-only query: 0x8E-A8 with the type and no values, which would set them."""
    import thunderbolt as T
    assert T.PARAM_REQUESTS == (bytes([0x10, 0x8E, 0xA8, 0x00, 0x10, 0x03]),
                                bytes([0x10, 0x8E, 0xA8, 0x01, 0x10, 0x03]),
                                bytes([0x10, 0x8E, 0x4A, 0x10, 0x03]),
                                bytes([0x10, 0x8E, 0xA9, 0x10, 0x03]))


def test_an_all_zero_loop_reply_is_refused():
    import thunderbolt as T
    assert T.decode_disciplining(bytes.fromhex("a8000000000000000000")) is None
    assert T.decode_disciplining(bytes.fromhex("a80042c800003f800000"))["time_constant_s"] == 100.0


def test_the_long_term_log_writes_one_row_per_interval(tmp_path):
    """Packets across two 10-minute boundaries give two rows, each with the
    interval's statistics, in the month's CSV under a header."""
    import csv
    import thunderbolt as T
    m = T.Monitor("/dev/null", log_dir=str(tmp_path))
    t0 = 1_790_000_400.0                                  # a 600 s boundary
    sup = {"disciplining_mode": 0, "holdover_s": 0, "critical_alarms": 0, "minor_alarms": 0,
           "osc_offset_ppb": 0.1, "pps_offset_ns": 1.0, "dac_v": 2.111, "temperature_c": 43.5}
    for i in range(1300):                                 # 21.7 minutes, one packet a second
        f = dict(sup, dac_v=2.111 + 1e-5 * (i // 600), pps_offset_ns=(-1.0) ** i)
        m._log("supplemental", f, t0 + i, {"time_constant_s": 100.0, "damping": 1.0})
        if i % 10 == 0:
            m._log("levels", {"levels": {1: 40.0, 2: 44.0, 3: 46.0, 4: 42.0, 5: 30.0}}, t0 + i, None)
            m._log("satellites", {"n_sats": 8}, t0 + i, None)
    files = list(tmp_path.glob("thunderbolt_*.csv"))
    assert len(files) == 1
    rows = list(csv.DictReader(open(files[0])))
    assert len(rows) == 2                                 # the third interval is still open
    r = rows[0]
    assert r["n_s"] == "600" and r["normal_s"] == "600" and r["time_constant_s"] == "100.0"
    assert float(r["pps_ns_std"]) == 1.0 and float(r["dac_v_mean"]) == 2.111
    assert float(r["level_top4"]) == 43.0 and float(r["sats_mean"]) == 8.0
    assert float(rows[1]["dac_v_mean"]) == 2.11101


def test_a_serious_minor_alarm_is_counted_and_leap_second_is_not(tmp_path):
    import csv
    import thunderbolt as T
    m = T.Monitor("/dev/null", log_dir=str(tmp_path))
    t0 = 1_790_000_400.0
    base = {"disciplining_mode": 0, "holdover_s": 0, "critical_alarms": 0, "osc_offset_ppb": 0.0,
            "pps_offset_ns": 0.0, "dac_v": 2.1, "temperature_c": 40.0}
    for i in range(601):
        minor = (1 << 1) if i < 5 else (1 << 7)           # antenna open for 5 s, then leap second pending
        m._log("supplemental", dict(base, minor_alarms=minor), t0 + i, None)
    r = list(csv.DictReader(open(next(tmp_path.glob("*.csv")))))[0]
    assert r["serious_minor_alarm_s"] == "5" and r["minor_bits"] == "0x0082"


def _sup(**kw):
    base = {"minor_alarms": 0, "critical_alarms": 0, "holdover_s": 0, "disciplining_mode": 0,
            "disciplining_mode_text": "normal", "gps_decoding": 0, "gps_decoding_text": "doing fixes",
            "receiver_mode_text": "overdetermined clock"}
    base.update(kw)
    return base


def test_warnings_are_only_what_is_active_now():
    import thunderbolt as T
    ok_primary = {"time_set": True, "utc_known": True, "utc": True}
    assert T.active_warnings(_sup(), ok_primary) == []
    w = T.active_warnings(_sup(minor_alarms=1 << 7, holdover_s=40), dict(ok_primary, utc=False))
    assert "leap second pending" in w and any("holdover for 40 s" in x for x in w)
    assert "reporting GPS time, not UTC" in w
    assert any(x.startswith("CRITICAL") for x in T.active_warnings(_sup(critical_alarms=1 << 4)))


def test_the_pps_configuration_decodes_the_cable_delay():
    import struct
    import thunderbolt as T
    p = bytes([0x4A, 1, 0, 0]) + struct.pack(">df", -120e-9, 300.0)
    got = T.decode_pps_config(p)
    assert got["pps_enabled"] and abs(got["cable_delay_ns"] + 120.0) < 1e-6
    assert T.decode(0x8F, p)[0] == "pps_config"
    assert T.decode_pps_config(bytes([0x4A, 1, 0, 0]) + struct.pack(">df", 5.0, 0.0)) is None   # not a delay


def test_the_survey_settings_decode_and_commands_queue_only_when_connected():
    import struct
    import thunderbolt as T
    p = bytes([0xA9, 1, 1]) + struct.pack(">II", 2000, 0)
    assert T.decode(0x8F, p) == ("survey_params", {"survey_enabled": True, "survey_saves_position": True,
                                                   "survey_length_fixes": 2000})
    m = T.Monitor("/dev/null")
    assert m.send(T.START_SURVEY) is False          # nothing connected: refused, not queued
    m.connected = True
    assert m.send(T.START_SURVEY) and list(m._outbox) == [bytes([0x10, 0x8E, 0xA6, 0x00, 0x10, 0x03])]


def test_the_cable_delay_packet_advances_the_pps_and_keeps_the_rest():
    """-50.5 ns for 10 m of RG-58, the enable, reserved and polarity bytes and
    the bias threshold exactly as the unit reported them (its reply of
    2026-09-30)."""
    import thunderbolt as T
    cur = T.decode_pps_config(bytes.fromhex("4a010100000000000000000043960000"))
    pkt = T.set_cable_delay_packet(cur, 50.5)
    body = pkt[1:-2].replace(b"\x10\x10", b"\x10")
    assert body[0] == 0x8E and body[1:5] == bytes.fromhex("4a010100")
    got = T.decode_pps_config(body[1:])
    assert abs(got["cable_delay_ns"] + 50.5) < 1e-9 and got["bias_threshold_m"] == 300.0
