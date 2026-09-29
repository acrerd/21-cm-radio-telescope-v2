"""The three clocks the telescope depends on, judged in one place for the
clock panel and the banner.

- The Thunderbolt: the 10 MHz and PPS the radio takes. Its frequency is the
  spectra's frequency axis; its PPS times pulsar TOAs (thunderbolt.py).
- This computer's clock: NTP through systemd-timesyncd. It names the whole
  second the radio's clock is set to, and times every recording made without
  the PPS.
- The telescope controller's clock: NTP through this computer's NAT on the
  private link. A time error there is a pointing error through GMST, and the
  link has been cut off from NTP before (August 2026) with nothing on the
  page to show it.

Each is judged as 'ok', 'warn', 'bad' or 'absent', with a line of text; the
banner shows the worst.
"""
import re
import subprocess
import time

LEVELS = ("ok", "absent", "warn", "stale", "bad")     # in increasing concern
ROOT_DISTANCE_WARN_MS = 50.0     # an NTP root distance past this is a poor sync
CONTROLLER_OFFSET_BAD_S = 2      # controller vs this computer; /time/status has 1 s resolution


def worst(levels):
    levels = [lv for lv in levels if lv]
    return max(levels, key=LEVELS.index) if levels else "absent"


def parse_timedatectl(show, timesync):
    """This computer's NTP state from `timedatectl show` and
    `timedatectl show-timesync --all` (key=value lines)."""
    kv = {}
    for text in (show, timesync):
        for line in (text or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
    out = {"synchronized": kv.get("NTPSynchronized") == "yes", "ntp_enabled": kv.get("NTP") == "yes",
           "server": kv.get("ServerName") or kv.get("ServerAddress") or "", "poll": kv.get("PollIntervalUSec", "")}
    msg = kv.get("NTPMessage", "")

    def field(name):
        m = re.search(r"\b%s=([^,}]+)" % name, msg)
        return m.group(1).strip() if m else None

    def ms(v):
        if v is None:
            return None
        m = re.match(r"([\d.]+)\s*(us|ms|s)", v)
        if not m:
            return None
        return float(m.group(1)) * {"us": 1e-3, "ms": 1.0, "s": 1e3}[m.group(2)]

    stratum = field("Stratum")
    out["stratum"] = int(stratum) if stratum and stratum.isdigit() else None
    delay, disp, jitter = ms(field("RootDelay")), ms(field("RootDispersion")), ms(field("Jitter"))
    out["root_delay_ms"], out["root_dispersion_ms"], out["jitter_ms"] = delay, disp, jitter
    # The standard bound on how far the clock can be from true time.
    out["root_distance_ms"] = (delay / 2 + disp) if delay is not None and disp is not None else None
    return out


def host_clock(run=subprocess.run):
    """This computer's NTP state, judged."""
    try:
        show = run(["timedatectl", "show"], capture_output=True, text=True, timeout=3).stdout
        ts = run(["timedatectl", "show-timesync", "--all"], capture_output=True, text=True, timeout=3).stdout
    except Exception as exc:                        # noqa: BLE001 - reported, never raised
        return {"level": "absent", "text": "cannot read timedatectl: %s" % exc}
    h = parse_timedatectl(show, ts)
    rd = h["root_distance_ms"]
    if not h["synchronized"]:
        h["level"], h["text"] = "bad", "not synchronised" + ("" if h["ntp_enabled"] else " (NTP off)")
    elif rd is not None and rd > ROOT_DISTANCE_WARN_MS:
        h["level"], h["text"] = "warn", "synchronised, but within %.0f ms at best" % rd
    else:
        h["level"] = "ok"
        h["text"] = "NTP, stratum %s" % h["stratum"] if h["stratum"] else "NTP synchronised"
        if rd is not None:
            h["text"] += ", within %.1f ms" % rd
    return h


def controller_clock(status, host_now=None):
    """The controller's /time/status, judged. `status` None: unreachable."""
    if not status:
        return {"level": "absent", "text": "controller not answering"}
    host_now = time.time() if host_now is None else host_now
    c = dict(status)
    state = c.get("sync_state", "")
    age = c.get("last_sync_age_s", -1)
    ts = c.get("timestamp")
    c["offset_s"] = (int(ts) - int(host_now)) if isinstance(ts, (int, float)) else None
    if state == "never":
        c["level"], c["text"] = "bad", "never synchronised"
    elif c["offset_s"] is not None and abs(c["offset_s"]) >= CONTROLLER_OFFSET_BAD_S:
        c["level"], c["text"] = "bad", "%+d s from this computer" % c["offset_s"]
    elif state == "unverified":
        c["level"], c["text"] = "warn", "set from a browser, not NTP"
    elif state == "stale":
        c["level"], c["text"] = "warn", "last NTP sync %.1f h ago" % (age / 3600.0)
    else:
        c["level"] = "ok"
        c["text"] = "%s, synced %s ago" % (c.get("source") or "NTP",
                                         ("%d min" % (age // 60)) if age >= 60 else ("%d s" % age))
    return c


def thunderbolt_clock(monitor):
    """The Thunderbolt monitor's status, in the same shape as the other two."""
    if monitor is None:
        return {"level": "absent", "text": "not configured"}
    s = monitor.status()
    level, text, locked = s["assessment"]
    if level == "absent":
        text = ("no status yet from %s" % s["device"]) if s["connected"] else \
               ("not connected (%s)" % (s["error"] or s["device"]))
    s.update({"level": level, "text": text, "locked": locked})
    return s
