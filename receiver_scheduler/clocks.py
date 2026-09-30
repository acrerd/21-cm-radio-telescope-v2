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


# ---------------------------------------------------------------------------
# Stability of the reference: modified Allan deviation of the Thunderbolt's
# own PPS-offset record (its oscillator-derived PPS against its GPS solution,
# one reading a second). That is how closely the unit tracks GPS, not the
# stability of the 10 MHz, which needs an independent reference: against a
# hydrogen maser a Thunderbolt reads ~1e-12 at 1 s, a hump to ~1e-11 near its
# loop time constant (this unit's is 60 s), mid-1e-14 at a day (T. Van Baak,
# leapsecond.com/pages/tbolt-8d). On short timescales the record is GPS
# measurement noise as filtered inside the unit - the first 40 s on
# 2026-09-30 read a flat ~1e-10 rather than falling as tau^-3/2, so the
# reported offset is smoothed. The fit separates phase-noise terms (GPS) from
# frequency-noise terms (the output) by slope.

# MVAR(tau) power laws: white PM tau^-3, flicker PM tau^-2 (GPS measurement
# noise); white FM tau^-1, flicker FM tau^0, random-walk FM tau^+1 (the
# output: oscillator and steering).
MVAR_TERMS = (("white PM", -3, "gps"), ("flicker PM", -2, "gps"),
              ("white FM", -1, "output"), ("flicker FM", 0, "output"), ("random-walk FM", 1, "output"))


def mdev(x, tau0, ms):
    """Modified Allan deviation of phase data `x` (seconds, one sample every
    `tau0` s) at averaging factors `ms`: (taus, mdevs, n_terms). The standard
    estimator (Riley, NIST SP 1065 eq. 14), with the inner sums by cumulative
    sum so each tau costs O(N)."""
    import numpy as np
    x = np.asarray(x, float)
    n = len(x)
    taus, devs, counts = [], [], []
    for m in ms:
        m = int(m)
        k = n - 3 * m + 1
        if m < 1 or k < 1:
            continue
        s = x[2 * m:] - 2 * x[m:n - m] + x[:n - 2 * m]          # N - 2m second differences
        c = np.concatenate([[0.0], np.cumsum(s)])
        w = c[m:m + k] - c[:k]                                   # k sums of m consecutive
        tau = m * tau0
        taus.append(tau)
        devs.append(float(np.sqrt(np.sum(w * w) / (2.0 * m * m * tau * tau * k))))
        counts.append(k)
    return taus, devs, counts


def fit_mvar(taus, devs, counts):
    """Non-negative least-squares fit of MVAR as a sum of MVAR_TERMS, each
    point weighted by its approximate relative error (~1/sqrt(k/m) independent
    samples) times the FITTED value, iterated: weighting by the measured value
    lets one long-tau point that happens to land low (a single estimator term
    can read 10x under the curve) carry an enormous weight and drive every
    output term to zero. Returns {term name: coefficient}."""
    import numpy as np
    from scipy.optimize import nnls
    t = np.asarray(taus, float)
    v = np.asarray(devs, float) ** 2
    m = t / t[0]
    rel = 1.0 / np.sqrt(np.maximum(np.asarray(counts, float) / m, 1.0))
    P = np.stack([t ** p for _, p, _ in MVAR_TERMS], axis=1)
    level = np.exp(np.polyval(np.polyfit(np.log(t), np.log(v), 2), np.log(t)))   # smooth first guess
    for _ in range(4):
        sig = np.maximum(2 * rel * level, 1e-300)
        coef, _ = nnls(P / sig[:, None], v / sig)
        level = np.maximum(P @ coef, 1e-300)
    return {name: float(c) for (name, _, _), c in zip(MVAR_TERMS, coef)}


def contiguous_tail(times, tol=0.5):
    """Index where the last unbroken run of one-second samples starts."""
    import numpy as np
    t = np.asarray(times, float)
    if len(t) < 2:
        return 0
    bad = np.nonzero(np.abs(np.diff(t) - 1.0) > tol)[0]
    return int(bad[-1] + 1) if len(bad) else 0


def stability(rows, n_tau=25):
    """The panel's stability plot from the monitor's rows (wall, osc_ppb,
    pps_ns, ...): measured MDEV with error bars, and the fitted GPS and
    output components on a fine tau grid."""
    import numpy as np
    if len(rows) < 30:
        return {"ok": False, "error": "only %d s of Thunderbolt record; need at least 30" % len(rows)}
    wall = [r[0] for r in rows]
    i0 = contiguous_tail(wall)
    x = np.array([r[2] for r in rows[i0:]], float) * 1e-9            # PPS offset, ns -> s
    n = len(x)
    if n < 30:
        return {"ok": False, "error": "the last unbroken run is only %d s; need at least 30" % n}
    # to a quarter of the record, not the estimator's limit of a third: there
    # the last point rests on a single term and can land anywhere
    ms = np.unique(np.round(np.logspace(0, np.log10(max(1, n // 4)), n_tau)).astype(int))
    taus, devs, counts = mdev(x, 1.0, ms)
    coef = fit_mvar(taus, devs, counts)
    grid = np.logspace(0, np.log10(taus[-1]), 60)
    part = {"gps": np.zeros_like(grid), "output": np.zeros_like(grid)}
    for name, p, group in MVAR_TERMS:
        part[group] += coef[name] * grid ** p
    gps, out = np.sqrt(part["gps"]), np.sqrt(part["output"])
    # The crossover from the MEASURED points against the fitted GPS part:
    # the first tau where the measured MVAR reaches twice it, i.e. the output
    # contributes as much as GPS. The GPS terms are pinned by many short-tau
    # points; the output terms rest on the few longest ones and trade off
    # against each other (white against flicker plus random-walk FM), so a
    # crossing of the two fitted curves is the less reliable of the two.
    # Even so it is uncertain by about a factor of two with a few hours of
    # record (tests/test_clock_stability.py).
    t = np.asarray(taus, float)
    g_at = sum(coef[name] * t ** p for name, p, group in MVAR_TERMS if group == "gps")
    cross = None
    if np.all(g_at > 0):
        r = np.log(np.maximum(np.asarray(devs) ** 2 - g_at, 1e-300) / g_at)
        above = np.nonzero(r > 0)[0]
        if len(above):
            j = above[0]
            cross = float(t[0]) if j == 0 else float(np.exp(np.interp(0.0, [r[j - 1], r[j]],
                                                                      np.log(t[j - 1:j + 1]))))
    errs = [d / np.sqrt(max(k / (t / taus[0]), 1.0)) for d, k, t in zip(devs, counts, taus)]
    return {"ok": True, "n_s": n, "gap_trimmed": i0, "tau": taus, "mdev": devs, "err": errs,
            "fit_tau": grid.tolist(), "fit_gps": gps.tolist(), "fit_output": out.tolist(),
            "fit_total": np.sqrt(part["gps"] + part["output"]).tolist(),
            "coefficients": coef, "crossover_s": cross}
