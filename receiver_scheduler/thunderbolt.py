#!/usr/bin/env python3
"""The Trimble Thunderbolt's serial status: the reference's own account of
whether it is locked to GPS.

The Thunderbolt disciplines the 10 MHz and the PPS that the B200 takes. Those
say nothing about their own health: a unit in holdover, or with its steering
voltage at the rail, goes on putting out a 10 MHz and a PPS that look perfect
and are wrong. Its serial port says which. It speaks TSIP (Trimble Standard
Interface Protocol), not NMEA, and sends two timing packets every second
unasked, so reading needs no polling:

  0x8F-AB  primary timing: the GPS time of the PPS edge just sent, the UTC
           offset, and whether the time is valid
  0x8F-AC  supplemental timing: disciplining mode (locked / holdover / ...),
           holdover duration, critical and minor alarms, its own estimates of
           the PPS offset (ns) and the 10 MHz offset (ppb), the DAC steering
           voltage, the temperature and the surveyed position

plus 0x6D (satellites in the solution) and 0x47 (signal levels) when enabled.

Framing: DLE (0x10), packet id, data with any 0x10 byte doubled, DLE ETX
(0x10 0x03). All numbers big-endian.

Written 2026-09-29 before the RS-232 adapter arrived, from the packet layouts
in Trimble's ThunderBolt user guide, and tested only against packets built
here from the same layouts. The first test on the unit is the check:

    python thunderbolt.py /dev/ttyUSB0            # decoded packets, one a line
    python thunderbolt.py /dev/ttyUSB0 --raw      # the bytes, if nothing decodes

Things to confirm then: the baud rate (9600 8-N-1 is the documented default),
that 0x8F-AB and 0x8F-AC both arrive once a second, and the minor-alarm bit
meanings (the table below is the documented one; bit 10 is not named in it).

Standard library only - radioconda has no pyserial, and 9600 baud raw needs
nothing termios does not already do.
"""
import collections
import os
import select
import struct
import sys
import threading
import time

DLE, ETX = 0x10, 0x03
BAUD = 9600
STALE_S = 10.0            # no packet for this long and the status is stale
RETRY_S = 30.0            # how often a missing device is looked for again
PARAMS_EVERY_S = 1800.0   # how often the disciplining parameters are asked for
LOG_EVERY_S = 600         # one summary row per this many seconds (reference_log/)
HISTORY_S = 6 * 3600      # status kept in memory for the panel's traces

RECEIVER_MODES = {0: "automatic 2D/3D", 1: "single satellite", 3: "2D", 4: "3D",
                  5: "DGPS reference", 6: "clock hold 2D", 7: "overdetermined clock"}
DISCIPLINING_MODES = {0: "normal", 1: "power-up", 2: "auto holdover", 3: "manual holdover",
                      4: "recovery", 5: "not used", 6: "disciplining disabled"}
DISCIPLINING_ACTIVITY = {0: "phase locking", 1: "oscillator warm-up", 2: "frequency locking",
                         3: "placing PPS", 4: "initialising loop filter", 5: "compensating OCXO",
                         6: "inactive", 7: "not used", 8: "recovery mode", 9: "calibration/control voltage"}
GPS_DECODING = {0x00: "doing fixes", 0x01: "no GPS time", 0x03: "PDOP too high",
                0x08: "no usable satellites", 0x09: "only 1 usable satellite",
                0x0A: "only 2 usable satellites", 0x0B: "only 3 usable satellites",
                0x0C: "chosen satellite unusable", 0x10: "TRAIM rejected the fix"}
CRITICAL_ALARMS = {0: "ROM checksum error", 1: "RAM check failed", 2: "power supply failure",
                   3: "FPGA check failed", 4: "control voltage at rail"}
MINOR_ALARMS = {0: "control voltage near rail", 1: "antenna open", 2: "antenna shorted",
                3: "not tracking satellites", 4: "not disciplining oscillator",
                5: "survey in progress", 6: "no stored position", 7: "leap second pending",
                8: "in test mode", 9: "position questionable", 11: "almanac not complete",
                12: "PPS not generated"}
# Minor alarms that mean the outputs cannot be trusted now, not merely
# "something to know": red on the panel.
MINOR_SERIOUS = {1, 2, 3, 4, 12}


def bits(value, table):
    """The names of the set bits, unknown ones as 'bit N'."""
    return [table.get(b, "bit %d" % b) for b in range(16) if value >> b & 1]


# ---------------------------------------------------------------------------
# framing


def encode(packet_id, payload=b""):
    """One TSIP packet, DLE-stuffed. For tests and for a simulated unit."""
    body = bytes([packet_id]) + bytes(payload)
    return bytes([DLE]) + body.replace(bytes([DLE]), bytes([DLE, DLE])) + bytes([DLE, ETX])


class Framer:
    """Bytes in, (id, payload) out, across arbitrary read boundaries.

    A packet starts at a DLE that is not followed by DLE or ETX; inside one,
    DLE DLE is a literal 0x10 and DLE ETX ends it. Anything before the first
    start - a read that began mid-packet - is thrown away, so the first
    packet after opening the port may be lost but none is ever misread."""

    MAX_LEN = 512

    def __init__(self):
        self.buf = bytearray()
        self.inside = False
        self.dle = False           # the previous byte was an unpaired DLE
        self.dropped = 0           # bytes discarded while looking for a start

    def feed(self, data):
        out = []
        for b in data:
            if not self.inside:
                if self.dle:
                    self.dle = False
                    if b not in (DLE, ETX):
                        self.inside = True
                        self.buf = bytearray([b])
                        continue
                    self.dropped += 1
                elif b == DLE:
                    self.dle = True
                else:
                    self.dropped += 1
                continue
            if self.dle:
                self.dle = False
                if b == DLE:
                    self.buf.append(DLE)
                elif b == ETX:
                    out.append((self.buf[0], bytes(self.buf[1:])))
                    self.inside = False
                else:
                    # DLE then a new id: the previous packet was cut short.
                    # Start again from here rather than glue two together.
                    self.dropped += len(self.buf)
                    self.buf = bytearray([b])
            elif b == DLE:
                self.dle = True
            else:
                self.buf.append(b)
                if len(self.buf) > self.MAX_LEN:
                    self.dropped += len(self.buf)
                    self.inside = False
        return out


# ---------------------------------------------------------------------------
# decoding


def decode_primary(p):
    """0x8F-AB, 17 bytes including the subcode."""
    if len(p) < 17 or p[0] != 0xAB:
        return None
    tow, week, utc_offset, flags, sec, mi, hr, day, mon, year = struct.unpack(">IHhBBBBBBH", p[1:17])
    return {"tow_s": tow, "week": week, "utc_offset_s": utc_offset, "timing_flags": flags,
            "utc": bool(flags & 0x01), "time_set": not (flags & 0x04), "utc_known": not (flags & 0x08),
            "time": "%04d-%02d-%02d %02d:%02d:%02d" % (year, mon, day, hr, mi, sec)}


def decode_supplemental(p):
    """0x8F-AC, 68 bytes including the subcode."""
    if len(p) < 60 or p[0] != 0xAC:
        return None
    (rx_mode, disc_mode, survey, holdover, crit, minor, decoding, activity, _s1, _s2,
     pps_ns, osc_ppb, dac, dac_v, temp, lat, lon, alt) = struct.unpack(">BBBIHHBBBBffIffddd", p[1:60])
    import math
    return {"receiver_mode": rx_mode, "receiver_mode_text": RECEIVER_MODES.get(rx_mode, str(rx_mode)),
            "disciplining_mode": disc_mode,
            "disciplining_mode_text": DISCIPLINING_MODES.get(disc_mode, str(disc_mode)),
            "survey_pct": survey, "holdover_s": holdover,
            "critical_alarms": crit, "critical_alarms_text": bits(crit, CRITICAL_ALARMS),
            "minor_alarms": minor, "minor_alarms_text": bits(minor, MINOR_ALARMS),
            "gps_decoding": decoding, "gps_decoding_text": GPS_DECODING.get(decoding, hex(decoding)),
            "disciplining_activity": activity,
            "disciplining_activity_text": DISCIPLINING_ACTIVITY.get(activity, str(activity)),
            "pps_offset_ns": pps_ns, "osc_offset_ppb": osc_ppb, "dac_value": dac, "dac_v": dac_v,
            "temperature_c": temp, "lat_deg": math.degrees(lat), "lon_deg": math.degrees(lon), "alt_m": alt}


def decode_satellites(p):
    """0x6D: the satellites in the current solution."""
    if len(p) < 17:
        return None
    n = p[0] >> 4
    pdop, hdop, vdop, tdop = struct.unpack(">ffff", p[1:17])
    prns = [b if b < 128 else b - 256 for b in p[17:17 + n]]
    return {"n_sats": n, "prns": prns, "pdop": pdop, "tdop": tdop}


def decode_levels(p):
    """0x47: signal level per satellite."""
    if not p:
        return None
    n = p[0]
    levels = {}
    for i in range(n):
        chunk = p[1 + 5 * i:6 + 5 * i]
        if len(chunk) < 5:
            break
        prn, level = struct.unpack(">Bf", chunk)
        levels[prn] = level
    return {"levels": levels}


def decode_disciplining(p):
    """0x8F-A8, the reply to a 0x8E-A8 request (read only - a request carries
    just the type byte, so nothing is changed). Type 0: the loop's time
    constant and damping; type 1: the oscillator's steering gain and the DAC
    voltage range. Verified on the unit 2026-09-30: 60 s, damping 1.0;
    1.516 Hz/V over 0-5 V."""
    if len(p) < 2 or p[0] != 0xA8:
        return None
    if p[1] == 0 and len(p) >= 10:
        tc, damping = struct.unpack(">ff", p[2:10])
        # A zero time constant is not a setting the unit can hold: an all-zero
        # reply of this type arrived right after a set command on 2026-09-30,
        # while clean queries read the value just set. Refused, not shown.
        if not tc > 0:
            return None
        return {"type": 0, "time_constant_s": tc, "damping": damping}
    if p[1] == 1 and len(p) >= 14:
        gain, vmin, vmax = struct.unpack(">fff", p[2:14])
        return {"type": 1, "efc_gain_hz_per_v": gain, "dac_min_v": vmin, "dac_max_v": vmax}
    return None


def decode_pps_config(p):
    """0x8F-4A, the reply to a bare 0x8E-4A request: the PPS characteristics.
    The offset is the cable-delay compensation the unit applies (seconds in
    the packet; a negative value advances the PPS to make up for the antenna
    cable). Kept with its raw bytes until checked against the unit."""
    if len(p) < 16 or p[0] != 0x4A:
        return None
    enabled, _res, polarity, offset, bias_m = struct.unpack(">BBBdf", p[1:16])
    if not (offset == offset and abs(offset) < 1e-3):
        return None
    return {"pps_enabled": bool(enabled), "pps_polarity": int(polarity),
            "cable_delay_ns": offset * 1e9, "bias_threshold_m": bias_m, "raw": p.hex()}


def decode_survey_params(p):
    """0x8F-A9, the reply to a bare 0x8E-A9 request: whether self-survey is
    enabled, whether its result is saved to EEPROM, and its length in fixes."""
    if len(p) < 7 or p[0] != 0xA9:
        return None
    enabled, save, length = struct.unpack(">BBI", p[1:7])
    return {"survey_enabled": bool(enabled), "survey_saves_position": bool(save), "survey_length_fixes": length}


PARAM_REQUESTS = (encode(0x8E, bytes([0xA8, 0])), encode(0x8E, bytes([0xA8, 1])), encode(0x8E, bytes([0x4A])),
                  encode(0x8E, bytes([0xA9])))
START_SURVEY = encode(0x8E, bytes([0xA6, 0x00]))   # restart the self-survey
SAVE_SETTINGS = encode(0x8E, bytes([0x26]))         # write the current settings to EEPROM


def set_cable_delay_packet(current, delay_ns):
    """0x8E-4A setting the PPS offset to -delay_ns (Trimble: "Negative values
    advance the 1 PPS and compensate for cable delay"; ThunderBolt E guide
    p. 17, 65), with every other field as the unit last reported it."""
    raw = bytes.fromhex(current["raw"])
    bias = struct.unpack(">f", raw[12:16])[0]
    return encode(0x8E, raw[0:4] + struct.pack(">df", -float(delay_ns) * 1e-9, bias))


def decode(packet_id, payload):
    """(kind, fields) or None for a packet we do not use."""
    if packet_id == 0x8F and payload:
        if payload[0] == 0xAB:
            return "primary", decode_primary(payload)
        if payload[0] == 0xAC:
            return "supplemental", decode_supplemental(payload)
        if payload[0] == 0xA8:
            got = decode_disciplining(payload)
            return ("loop" if got and got["type"] == 0 else "oscillator"), got
        if payload[0] == 0x4A:
            return "pps_config", decode_pps_config(payload)
        if payload[0] == 0xA9:
            return "survey_params", decode_survey_params(payload)
    elif packet_id == 0x6D:
        return "satellites", decode_satellites(payload)
    elif packet_id == 0x47:
        return "levels", decode_levels(payload)
    return None


# ---------------------------------------------------------------------------
# what it all means


def active_warnings(sup, primary=None):
    """Plain-language warnings that are true NOW, and nothing else: an empty
    list means there is nothing to show. The timing ones matter most here - a
    leap second is the one event that would upset pulsar timing and every
    recording's timestamps - and a surveying or holdover unit is putting out a
    reference that is not what it usually is."""
    out = []
    if sup:
        for b, text in sorted(MINOR_ALARMS.items()):
            if sup["minor_alarms"] >> b & 1:
                out.append(text)
        for b, text in sorted(CRITICAL_ALARMS.items()):
            if sup["critical_alarms"] >> b & 1:
                out.append("CRITICAL: " + text)
        if sup.get("holdover_s"):
            out.append("in holdover for %d s: the 10 MHz and PPS are free-running" % sup["holdover_s"])
        if sup.get("disciplining_mode") not in (0, None):
            out.append("disciplining: " + sup["disciplining_mode_text"])
        if sup.get("gps_decoding") not in (0, None):
            out.append("GPS: " + sup["gps_decoding_text"])
        if sup.get("receiver_mode_text") not in ("overdetermined clock", None):
            out.append("receiver mode: " + sup["receiver_mode_text"])
    if primary:
        if not primary.get("time_set", True):
            out.append("GPS time not set")
        if not primary.get("utc_known", True):
            out.append("UTC offset not yet known")
        if not primary.get("utc", True):
            out.append("reporting GPS time, not UTC")
    return out


def assess(sup, primary=None, age_s=None):
    """(level, text, locked) from the latest supplemental packet.

    level: 'ok' (locked, no alarm that matters), 'warn' (holdover, recovery,
    survey, steering near the rail: the outputs are still good but not
    disciplined as normal), 'bad' (a critical alarm, antenna fault, not
    tracking or not disciplining: do not trust the outputs), 'stale' (the
    unit has gone quiet), 'absent' (never heard from)."""
    if sup is None:
        return "absent", "no Thunderbolt status", False
    if age_s is not None and age_s > STALE_S:
        return "stale", "no status for %.0f s" % age_s, False
    crit, minor, mode = sup["critical_alarms"], sup["minor_alarms"], sup["disciplining_mode"]
    serious = [MINOR_ALARMS.get(b, "bit %d" % b) for b in sorted(MINOR_SERIOUS) if minor >> b & 1]
    if crit:
        return "bad", "critical: " + ", ".join(sup["critical_alarms_text"]), False
    if serious:
        return "bad", ", ".join(serious), False
    if primary is not None and not primary.get("time_set", True):
        return "bad", "time not set", False
    locked = mode == 0
    if not locked:
        text = sup["disciplining_mode_text"]
        if mode in (2, 3):
            text += " %s" % _duration(sup["holdover_s"])
        return "warn", text, False
    others = [t for t in sup["minor_alarms_text"] if t not in serious]
    if others:
        return "warn", "locked; " + ", ".join(others), True
    return "ok", "locked", True


def _duration(s):
    s = int(s)
    return "%dh%02dm" % (s // 3600, s % 3600 // 60) if s >= 3600 else "%dm%02ds" % (s // 60, s % 60)


# ---------------------------------------------------------------------------
# the serial port and the monitor thread


def open_port(path, baud=BAUD):
    """The device raw at `baud`, 8-N-1, non-blocking. Raises OSError."""
    import termios
    import tty
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        tty.setraw(fd)
        attrs = termios.tcgetattr(fd)
        speed = getattr(termios, "B%d" % baud)
        attrs[4] = attrs[5] = speed                       # ispeed, ospeed
        attrs[2] = (attrs[2] & ~(termios.PARENB | termios.CSTOPB | termios.CSIZE | termios.CRTSCTS)
                    ) | termios.CS8 | termios.CLOCAL | termios.CREAD
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIFLUSH)
    except Exception:
        os.close(fd)
        raise
    return fd


LOG_COLUMNS = ("utc_start", "utc_end", "n_s", "normal_s", "holdover_s_max",
               "critical_alarm_s", "serious_minor_alarm_s", "critical_bits", "minor_bits",
               "osc_ppb_mean", "osc_ppb_std", "pps_ns_mean", "pps_ns_std", "pps_ns_min", "pps_ns_max",
               "dac_v_mean", "dac_v_min", "dac_v_max", "temp_c_mean", "temp_c_min", "temp_c_max",
               "sats_mean", "sats_min", "level_mean", "level_top4", "time_constant_s", "damping")


class Summary:
    """Statistics of the unit over one interval, for the long-term log: what
    shows the reference degrading. The steering voltage walks as the
    oscillator ages (x EFC gain = an ageing rate); the strongest satellites'
    levels, the satellite count and the PPS scatter fall or rise as an
    antenna, its cable or its preamp fails, long before the antenna alarms."""

    def __init__(self, start):
        self.start = start
        self.n = self.normal = self.crit_s = self.minor_s = 0
        self.crit_bits = self.minor_bits = 0
        self.holdover = 0
        self.vals = {"osc": [], "pps": [], "dac": [], "temp": []}
        self.sats, self.level_mean, self.level_top = [], [], []

    def add_supplemental(self, f):
        self.n += 1
        self.normal += f["disciplining_mode"] == 0
        self.holdover = max(self.holdover, int(f["holdover_s"]))
        self.crit_bits |= f["critical_alarms"]
        self.minor_bits |= f["minor_alarms"]
        self.crit_s += bool(f["critical_alarms"])
        self.minor_s += any(f["minor_alarms"] >> b & 1 for b in MINOR_SERIOUS)
        for key, field in (("osc", "osc_offset_ppb"), ("pps", "pps_offset_ns"),
                           ("dac", "dac_v"), ("temp", "temperature_c")):
            self.vals[key].append(float(f[field]))

    def add_satellites(self, f):
        self.sats.append(int(f["n_sats"]))

    def add_levels(self, f):
        lv = sorted((v for v in f["levels"].values() if v > 0), reverse=True)
        if lv:
            self.level_mean.append(sum(lv) / len(lv))
            self.level_top.append(sum(lv[:4]) / len(lv[:4]))

    def row(self, end, loop=None):
        import statistics as st
        def stats(v):
            if not v:
                return ("",) * 4
            return (st.fmean(v), st.pstdev(v), min(v), max(v))
        def iso(t):
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
        o, p, d, t = (stats(self.vals[k]) for k in ("osc", "pps", "dac", "temp"))
        fmt = lambda x, n: "" if x == "" else ("%.*f" % (n, x))
        return [iso(self.start), iso(end), self.n, self.normal, self.holdover, self.crit_s, self.minor_s,
                "0x%04x" % self.crit_bits, "0x%04x" % self.minor_bits,
                fmt(o[0], 4), fmt(o[1], 4), fmt(p[0], 2), fmt(p[1], 2), fmt(p[2], 2), fmt(p[3], 2),
                fmt(d[0], 5), fmt(d[2], 5), fmt(d[3], 5), fmt(t[0], 3), fmt(t[2], 3), fmt(t[3], 3),
                fmt(sum(self.sats) / len(self.sats), 2) if self.sats else "", min(self.sats) if self.sats else "",
                fmt(sum(self.level_mean) / len(self.level_mean), 2) if self.level_mean else "",
                fmt(sum(self.level_top) / len(self.level_top), 2) if self.level_top else "",
                fmt(loop["time_constant_s"], 1) if loop else "", fmt(loop["damping"], 3) if loop else ""]


def append_log_row(log_dir, row):
    """One row into the month's CSV, with a header if the file is new."""
    import csv
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "thunderbolt_%s.csv" % row[0][:7])
    new = not os.path.exists(path)
    with open(path, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(LOG_COLUMNS)
        w.writerow(row)
    return path


class Monitor:
    """Reads the unit in a thread and keeps the latest of each packet and a
    history of the supplemental status. A missing device is looked for every
    RETRY_S, so plugging the adapter in needs no restart; `on_change(level,
    text)` is called when the assessment changes (for the log)."""

    def __init__(self, device, baud=BAUD, on_change=None, log_dir=None):
        self.device, self.baud, self.on_change = device, baud, on_change
        self.log_dir = log_dir           # the long-term summary log (reference_log/), or None
        self._summary = None
        self._outbox = collections.deque()  # commands to write, from the reader thread only
        self.lock = threading.Lock()
        self.latest = {}                 # kind -> (monotonic, unix, fields)
        self.history = collections.deque()
        self.error = ""
        self.connected = False
        self.packets = 0
        self._level = None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="thunderbolt", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def handle(self, packet_id, payload, now=None, wall=None):
        """One packet in. Public so tests and a replay can drive it."""
        got = decode(packet_id, payload)
        if not got or got[1] is None:
            return
        kind, fields = got
        now = time.monotonic() if now is None else now
        wall = time.time() if wall is None else wall
        with self.lock:
            self.packets += 1
            self.latest[kind] = (now, wall, fields)
            loop = self.latest.get("loop")
        if self.log_dir:
            self._log(kind, fields, wall, loop[2] if loop else None)
        with self.lock:
            if kind == "supplemental":
                # The unit's own second, from the 0x8F-AB sent just before
                # this packet for the same PPS: what the stability plot
                # judges continuity by, since the host's receipt time breaks
                # at any stall (07:50 on 2026-10-01 cut 5.6 h for one late
                # packet). None when that packet is missing or not this
                # second's.
                pri = self.latest.get("primary")
                gps_s = (pri[2]["week"] * 604800 + pri[2]["tow_s"]
                         if pri and 0.0 <= now - pri[0] < 0.5 else None)
                self.history.append((wall, fields["osc_offset_ppb"], fields["pps_offset_ns"],
                                     fields["dac_v"], fields["temperature_c"], fields["disciplining_mode"],
                                     gps_s))
                while self.history and self.history[0][0] < wall - HISTORY_S:
                    self.history.popleft()
        if kind == "supplemental":
            self._note_level(now)

    def _log(self, kind, fields, wall, loop):
        """Accumulate the interval's statistics; write a row as each
        LOG_EVERY_S boundary passes. Only from the reader thread."""
        slot = int(wall // LOG_EVERY_S) * LOG_EVERY_S
        if self._summary is not None and slot > self._summary.start and self._summary.n:
            try:
                append_log_row(self.log_dir, self._summary.row(min(wall, self._summary.start + LOG_EVERY_S), loop))
            except OSError as exc:
                self.error = "log: %s" % exc
        if self._summary is None or slot > self._summary.start:
            self._summary = Summary(slot)
        if kind == "supplemental":
            self._summary.add_supplemental(fields)
        elif kind == "satellites":
            self._summary.add_satellites(fields)
        elif kind == "levels":
            self._summary.add_levels(fields)

    def _note_level(self, now):
        level, text, _ = self.status(now)["assessment"]
        if level != self._level:
            if self._level is not None and self.on_change:
                self.on_change(level, text)
            self._level = level

    def status(self, now=None):
        """Everything the panel and a recording need, as plain values."""
        now = time.monotonic() if now is None else now
        with self.lock:
            sup = self.latest.get("supplemental")
            pri = self.latest.get("primary")
            sat = self.latest.get("satellites")
            out = {"device": self.device, "connected": self.connected, "error": self.error,
                   "packets": self.packets}
            age = (now - sup[0]) if sup else None
            out["age_s"] = age
            out["supplemental"] = sup[2] if sup else None
            out["primary"] = pri[2] if pri else None
            out["satellites"] = sat[2] if sat else None
            loop, osc = self.latest.get("loop"), self.latest.get("oscillator")
            out["loop"] = loop[2] if loop else None
            out["oscillator"] = osc[2] if osc else None
            ppsc, lev = self.latest.get("pps_config"), self.latest.get("levels")
            out["pps_config"] = ppsc[2] if ppsc else None
            sv = self.latest.get("survey_params")
            out["survey_params"] = sv[2] if sv else None
            out["levels"] = lev[2] if lev else None
        out["assessment"] = assess(out["supplemental"], out["primary"], age)
        out["warnings"] = active_warnings(out["supplemental"], out["primary"])
        return out

    def send(self, packet):
        """Queue a TSIP packet for the unit. Written by the reader thread, the
        port's only user: a second process on the port splits the replies
        (2026-09-30). Returns False when no unit is connected."""
        if not self.connected:
            return False
        self._outbox.append(bytes(packet))
        return True

    def rows(self):
        """The whole kept history at full resolution, one row a second:
        (wall, osc_ppb, pps_ns, dac_v, temp_c, mode, gps_s). For the stability plot."""
        with self.lock:
            return list(self.history)

    def history_points(self, max_points=720):
        """The kept history, thinned evenly to at most `max_points`."""
        with self.lock:
            h = list(self.history)
        step = max(1, len(h) // max_points)
        return [dict(zip(("t", "osc_ppb", "pps_ns", "dac_v", "temp_c", "mode"), row)) for row in h[::step]]

    def _run(self):
        while not self._stop.is_set():
            try:
                fd = open_port(self.device, self.baud)
            except OSError as exc:
                self.connected, self.error = False, "%s: %s" % (self.device, exc.strerror or exc)
                self._stop.wait(RETRY_S)
                continue
            self.connected, self.error = True, ""
            framer = Framer()
            asked = 0.0
            try:
                while not self._stop.is_set():
                    if time.monotonic() - asked > PARAMS_EVERY_S:
                        for req in PARAM_REQUESTS:
                            os.write(fd, req)
                        asked = time.monotonic()
                    while self._outbox:
                        os.write(fd, self._outbox.popleft())
                        asked = time.monotonic() - PARAMS_EVERY_S + 3.0   # re-read the settings in 3 s
                    ready, _, _ = select.select([fd], [], [], 1.0)
                    if not ready:
                        continue
                    try:
                        data = os.read(fd, 1024)
                    except BlockingIOError:              # another reader took the bytes
                        continue
                    if not data:
                        raise OSError("device closed")
                    for pid, payload in framer.feed(data):
                        self.handle(pid, payload)
            except OSError as exc:
                self.error = "%s: %s" % (self.device, exc)
            finally:
                self.connected = False
                os.close(fd)
            self._stop.wait(RETRY_S)


def recording_attrs(monitor):
    """The reference's state for a recording's attributes, flat, prefixed
    `reference_`. Always present, so a file says 'absent' rather than
    nothing when no unit was being read."""
    if monitor is None:
        return {"reference_state": "absent"}
    s = monitor.status()
    level, text, locked = s["assessment"]
    out = {"reference_state": level, "reference_status": text, "reference_locked": bool(locked)}
    sup = s["supplemental"]
    if sup and level != "stale":
        out.update({"reference_disciplining_mode": sup["disciplining_mode_text"],
                    "reference_holdover_s": int(sup["holdover_s"]),
                    "reference_critical_alarms": int(sup["critical_alarms"]),
                    "reference_minor_alarms": int(sup["minor_alarms"]),
                    "reference_osc_offset_ppb": float(sup["osc_offset_ppb"]),
                    "reference_pps_offset_ns": float(sup["pps_offset_ns"]),
                    "reference_dac_v": float(sup["dac_v"]),
                    "reference_temperature_c": float(sup["temperature_c"])})
    if s["satellites"]:
        out["reference_n_sats"] = int(s["satellites"]["n_sats"])
    return out


# ---------------------------------------------------------------------------
# the first test on the unit


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Read a Trimble Thunderbolt's TSIP status")
    ap.add_argument("device", nargs="?", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=BAUD)
    ap.add_argument("--raw", action="store_true", help="print the bytes as hex instead of decoding")
    ap.add_argument("--seconds", type=float, default=0, help="stop after this long (0: run until Ctrl-C)")
    a = ap.parse_args()
    fd = open_port(a.device, a.baud)
    framer, t_end = Framer(), (time.time() + a.seconds if a.seconds else None)
    print("reading %s at %d 8-N-1; Ctrl-C to stop" % (a.device, a.baud))
    try:
        while t_end is None or time.time() < t_end:
            ready, _, _ = select.select([fd], [], [], 1.0)
            if not ready:
                continue
            data = os.read(fd, 1024)
            if a.raw:
                print(data.hex(" "))
                continue
            for pid, payload in framer.feed(data):
                got = decode(pid, payload)
                if got and got[1] is not None:
                    kind, f = got
                    if kind == "supplemental":
                        level, text, _ = assess(f)
                        print("AC  %-5s %-28s holdover %6ds  osc %+8.3f ppb  pps %+8.1f ns  DAC %.4f V  %.1f C"
                              % (level, text, f["holdover_s"], f["osc_offset_ppb"], f["pps_offset_ns"],
                                 f["dac_v"], f["temperature_c"]))
                    elif kind == "primary":
                        print("AB  %s %s  week %d tow %d  UTC-GPS %d s" % (f["time"], "UTC" if f["utc"] else "GPS",
                                                                     f["week"], f["tow_s"], f["utc_offset_s"]))
                    else:
                        print("%02X  %s" % (pid, f))
                else:
                    print("%02X  (%d bytes, not decoded)%s" % (pid, len(payload),
                          " sub %02X" % payload[0] if pid == 0x8F and payload else ""))
    except KeyboardInterrupt:
        pass
    finally:
        os.close(fd)
    if framer.dropped:
        print("bytes discarded while finding packet starts: %d" % framer.dropped)


if __name__ == "__main__":
    sys.exit(main())
