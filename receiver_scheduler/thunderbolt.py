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


def decode(packet_id, payload):
    """(kind, fields) or None for a packet we do not use."""
    if packet_id == 0x8F and payload:
        if payload[0] == 0xAB:
            return "primary", decode_primary(payload)
        if payload[0] == 0xAC:
            return "supplemental", decode_supplemental(payload)
    elif packet_id == 0x6D:
        return "satellites", decode_satellites(payload)
    elif packet_id == 0x47:
        return "levels", decode_levels(payload)
    return None


# ---------------------------------------------------------------------------
# what it all means


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


class Monitor:
    """Reads the unit in a thread and keeps the latest of each packet and a
    history of the supplemental status. A missing device is looked for every
    RETRY_S, so plugging the adapter in needs no restart; `on_change(level,
    text)` is called when the assessment changes (for the log)."""

    def __init__(self, device, baud=BAUD, on_change=None):
        self.device, self.baud, self.on_change = device, baud, on_change
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
            if kind == "supplemental":
                self.history.append((wall, fields["osc_offset_ppb"], fields["pps_offset_ns"],
                                     fields["dac_v"], fields["temperature_c"], fields["disciplining_mode"]))
                while self.history and self.history[0][0] < wall - HISTORY_S:
                    self.history.popleft()
        if kind == "supplemental":
            self._note_level(now)

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
        out["assessment"] = assess(out["supplemental"], out["primary"], age)
        return out

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
            try:
                while not self._stop.is_set():
                    ready, _, _ = select.select([fd], [], [], 1.0)
                    if not ready:
                        continue
                    data = os.read(fd, 1024)
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
