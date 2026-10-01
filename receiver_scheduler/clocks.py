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
# loop time constant, mid-1e-14 at a day (T. Van Baak,
# leapsecond.com/pages/tbolt-8d).
#
# What is wanted from it is the TRANSITION: the averaging time at which the
# curve stops falling and turns up, beyond which averaging longer buys
# nothing. It is read off the measured points and nothing else. Until
# 2026-09-30 the record was also split into GPS noise (phase-noise slopes) and
# output noise (frequency-noise slopes) by a fit, and the split cannot be
# made from this record: the unit smooths the offset it reports, and smoothed
# phase noise takes the slopes of frequency noise. On the first record (flat
# ~1e-10 to 30 s, then falling) the fit called the plateau "output", drew it
# 50x above the long-tau points and put the crossover at 1 s; a fitted filter
# time mended that record and failed on simulated first-order smoothing.


# Expectation, not measurement: a Thunderbolt's own MDEV, separated from an
# HP 5071A caesium standard and a third GPSDO by three-cornered hat over a
# 3-day run (John Miles, KE5FX, March 2015, ke5fx.com/gpscomp.htm, plot
# mhat.png). Read off the plot by eye, good to ~15%; 1.26e-12 at 100 s is
# the value printed on it. Stops at 5000 s, beyond which the hat goes
# negative. That unit was modified - an HP 10811 oscillator, "10811 TBolt" -
# so ours, on the stock oscillator, should do no better. It is the 10 MHz
# against caesium, where our points are the unit's PPS against GPS.
REFERENCE_TBOLT_VS_CAESIUM = {
    "label": "Thunderbolt vs caesium (KE5FX 2015, 10811 OCXO)",
    "source": "http://www.ke5fx.com/gpscomp.htm",
    "tau_s": [0.1, 0.2, 1.0, 3.0, 10.0, 27.0, 100.0, 1000.0, 3000.0, 5000.0],
    "mdev": [4.2e-13, 2.9e-13, 4.5e-13, 5.6e-13, 7.9e-13, 1.08e-12, 1.26e-12, 1.2e-12, 1.6e-12, 1.7e-12],
}


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


def transition(taus, devs, errs):
    """Where the measured MDEV stops falling: the lowest point, accepted as a
    turn only if the points beyond it rise - a weighted log-log slope over
    them above zero by 2 sigma, each point's log error err/mdev (floored at
    5%). `span_s` is the run of taus around it whose points are within their
    joint error of the lowest, which is how well the data place it. With no
    significant rise, found is False and the curve is still falling (or
    flat) at the longest tau: the transition is beyond the record."""
    import numpy as np
    t, d, e = (np.asarray(v, float) for v in (taus, devs, errs))
    i = int(np.argmin(d))
    lo = hi = i
    while lo > 0 and d[lo - 1] - e[lo - 1] <= d[i] + e[i]:
        lo -= 1
    while hi < len(t) - 1 and d[hi + 1] - e[hi + 1] <= d[i] + e[i]:
        hi += 1
    out = {"found": False, "tau_s": float(t[i]), "mdev": float(d[i]),
           "span_s": [float(t[lo]), float(t[hi])], "tau_max_s": float(t[-1])}
    if len(t) - i >= 3:
        x, y = np.log(t[i:]), np.log(d[i:])
        w = 1.0 / np.maximum(e[i:] / d[i:], 0.05) ** 2
        xm = np.sum(w * x) / np.sum(w)
        sxx = np.sum(w * (x - xm) ** 2)
        slope = float(np.sum(w * (x - xm) * (y - np.sum(w * y) / np.sum(w))) / sxx)
        sigma = float(1.0 / np.sqrt(sxx))
        out.update(rise_slope=slope, rise_sigma=sigma, found=bool(slope > 2 * sigma))
    return out


def contiguous_tail(times, tol=0.5, unit_s=None):
    """Index where the last unbroken run of one-second samples starts.

    Judged by the unit's own seconds (`unit_s`, None where a row has none)
    wherever both rows of a step have them: a missing second is a break, a
    packet the host handled late is not. Elsewhere by the host's receipt
    times, where any stall longer than `tol` reads as a break."""
    import numpy as np
    t = np.asarray(times, float)
    if len(t) < 2:
        return 0
    bad = np.abs(np.diff(t) - 1.0) > tol
    if unit_s is not None:
        du = np.diff(np.array([np.nan if u is None else u for u in unit_s], float))
        bad = np.where(np.isfinite(du), du != 1.0, bad)
    bad = np.nonzero(bad)[0]
    return int(bad[-1] + 1) if len(bad) else 0


def stability(rows, n_tau=25):
    """The panel's stability plot from the monitor's rows (wall, osc_ppb,
    pps_ns, ...): measured MDEV with error bars and the transition."""
    import numpy as np
    if len(rows) < 30:
        return {"ok": False, "error": "only %d s of Thunderbolt record; need at least 30" % len(rows)}
    wall = [r[0] for r in rows]
    i0 = contiguous_tail(wall, unit_s=[r[6] if len(r) > 6 else None for r in rows])
    x = np.array([r[2] for r in rows[i0:]], float) * 1e-9            # PPS offset, ns -> s
    n = len(x)
    if n < 30:
        return {"ok": False, "error": "the last unbroken run is only %d s; need at least 30" % n}
    # to a quarter of the record, not the estimator's limit of a third: there
    # the last point rests on a single term and can land anywhere
    ms = np.unique(np.round(np.logspace(0, np.log10(max(1, n // 4)), n_tau)).astype(int))
    taus, devs, counts = mdev(x, 1.0, ms)
    errs = [d / np.sqrt(max(k / (t / taus[0]), 1.0)) for d, k, t in zip(devs, counts, taus)]
    # The white-phase-noise expectation, MDEV = sqrt(3) sigma_x tau^-3/2 for
    # one-second samples, at the record's own scatter about a straight line:
    # measured, not fitted. The unit smooths what it reports, so the small-tau
    # points sit far below it; it describes them only once tau is past that.
    t = np.arange(n, dtype=float)
    sigma_x = float(np.std(x - np.polyval(np.polyfit(t, x, 1), t)))
    return {"ok": True, "n_s": n, "gap_trimmed": i0, "tau": taus, "mdev": devs, "err": errs,
            "transition": transition(taus, devs, errs),
            "sigma_x_s": sigma_x, "reference": REFERENCE_TBOLT_VS_CAESIUM}
