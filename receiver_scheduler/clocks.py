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
    estimator (Riley, NIST SP 1065 eq. 14) written as second differences of
    m-sample phase averages, the averages by cumulative sum so each tau costs
    O(N). A NaN in `x` is a missing sample: every term that would average
    over it is dropped and the rest are kept, so a gap costs only the terms
    that touch it. With no NaN it is exactly the textbook estimator."""
    import numpy as np
    x = np.asarray(x, float)
    n = len(x)
    ok = np.isfinite(x)
    v = np.where(ok, x - (np.mean(x[ok]) if ok.any() else 0.0), 0.0)
    cs = np.concatenate([[0.0], np.cumsum(v)])
    cn = np.concatenate([[0], np.cumsum(ok)])
    taus, devs, counts = [], [], []
    for m in ms:
        m = int(m)
        if m < 1 or n - 3 * m + 1 < 1:
            continue
        a = (cs[m:] - cs[:-m]) / m                              # n - m + 1 averages
        full = (cn[m:] - cn[:-m]) == m
        d = a[2 * m:] - 2 * a[m:-m] + a[:-2 * m]
        use = full[2 * m:] & full[m:-m] & full[:-2 * m]
        k = int(np.sum(use))
        if k < 1:
            continue
        tau = m * tau0
        taus.append(tau)
        devs.append(float(np.sqrt(np.sum(d[use] ** 2) / (2.0 * tau * tau * k))))
        counts.append(k)
    return taus, devs, counts


def mdev_errors(taus, devs, counts, tau0):
    """One-sigma error of each MDEV point: the terms overlap, so a point
    rests on about k/m independent ones (k terms, m = tau/tau0)."""
    import numpy as np
    return [d / np.sqrt(max(k / (t / tau0), 1.0)) for d, k, t in zip(devs, counts, taus)]


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


LOG_BLOCK_S = 600                # thunderbolt.LOG_EVERY_S
LOG_BLOCK_MIN_FILL = 0.9         # a block holding less of its seconds is a gap
LOG_SURVEY_BITS = (1 << 5) | (1 << 6)   # minor alarms: survey in progress, no stored position
# How long after the unit's configuration changes the record is left out.
# The 50.5 ns cable-delay step of 2026-09-30 17:00 UTC took 20-30 min to
# pull in (block means 31, 2.9, 1.3 ns against a normal scatter of 0.4); an
# hour is that with margin, and the changes are rare.
LOG_SETTLE_S = 3600


def read_log(log_dir):
    """The long-term log's rows, oldest first, as dicts with `start` (epoch
    s), `n_s`, `normal_s`, `minor_bits`, `pps_ns_mean` and the settings
    `time_constant_s`, `damping`, `cable_delay_ns` (strings, '' where the
    row predates the column or the unit had not reported it). A header line
    rebinds the columns, so a file whose columns grew mid-month reads
    whole. Rows that cannot be read are skipped; no directory is no rows."""
    import calendar
    import csv
    import glob
    import os
    out = []
    for path in sorted(glob.glob(os.path.join(log_dir, "thunderbolt_*.csv"))):
        try:
            with open(path, newline="") as fh:
                cols = None
                for row in csv.reader(fh):
                    if row and row[0] == "utc_start":
                        cols = row
                        continue
                    if cols is None:
                        continue
                    r = dict(zip(cols, row))
                    try:
                        out.append({
                            "start": float(calendar.timegm(time.strptime(r["utc_start"], "%Y-%m-%dT%H:%M:%SZ"))),
                            "n_s": int(r["n_s"]), "normal_s": int(r["normal_s"]),
                            "minor_bits": int(r.get("minor_bits") or "0", 16),
                            "pps_ns_mean": float(r["pps_ns_mean"]) if r.get("pps_ns_mean") else None,
                            "time_constant_s": r.get("time_constant_s") or "",
                            "damping": r.get("damping") or "",
                            "cable_delay_ns": r.get("cable_delay_ns") or ""})
                    except (KeyError, ValueError, TypeError):
                        continue
        except OSError:
            continue
    out.sort(key=lambda b: b["start"])
    return out


def log_run_start(blocks):
    """Where the run the stability plot may use begins: LOG_SETTLE_S after
    the last block in which the unit was surveying, or after the block in
    which its time constant, damping or cable delay last changed - so the
    curve describes one loop, settled, and no operator's step in the PPS is
    read as the clock. A setting the row does not carry ('', older rows)
    is unknown and never reads as a change. Returns (start epoch s, the
    settings in force)."""
    keys = ("time_constant_s", "damping", "cable_delay_ns")
    eff = [None] * len(keys)
    for i in range(len(blocks) - 1, -1, -1):
        b = blocks[i]
        if b["minor_bits"] & LOG_SURVEY_BITS:
            return b["start"] + LOG_BLOCK_S + LOG_SETTLE_S, dict(zip(keys, eff))
        cfg = [b[k] or None for k in keys]
        if any(c is not None and e is not None and c != e for c, e in zip(cfg, eff)):
            return blocks[i + 1]["start"] + LOG_BLOCK_S + LOG_SETTLE_S, dict(zip(keys, eff))
        eff = [e if e is not None else c for c, e in zip(cfg, eff)]
    return (blocks[0]["start"] if blocks else 0.0), dict(zip(keys, eff))


def log_stability(blocks, n_tau=14):
    """MDEV at tau = 10 min and beyond from the long-term log's block means.

    MDEV is built from the phase averaged over tau, and a 10-minute block's
    `pps_ns_mean` is exactly that average over 600 s, so the mean of M
    consecutive blocks is the phase average over M x 600 s and `mdev` of the
    block series at factor M is the one-second estimator at tau = M x 600 s,
    sampled at block-aligned starts only - fewer terms, but overlapping terms
    are nearly redundant anyway. Unlike the one-second record it survives
    scheduler restarts: a block that is missing, holds under 90% of its
    seconds (the one a restart cuts) or was not all in normal disciplining
    is a gap, and costs only the terms that touch it. Only the run from an
    hour after the last survey or change of settings is used
    (`log_run_start`). None when there are no usable blocks."""
    import numpy as np
    blocks = [b for b in blocks if b["start"] % LOG_BLOCK_S == 0]
    if not blocks:
        return None
    t_run, settings = log_run_start(blocks)
    run = [b for b in blocks if b["start"] >= t_run]
    if not run:
        return None
    t0 = run[0]["start"]
    n = int((run[-1]["start"] - t0) // LOG_BLOCK_S) + 1
    x = np.full(n, np.nan)
    for b in run:
        if (b["pps_ns_mean"] is not None and b["n_s"] >= LOG_BLOCK_MIN_FILL * LOG_BLOCK_S
                and b["normal_s"] == b["n_s"]):
            x[int((b["start"] - t0) // LOG_BLOCK_S)] = b["pps_ns_mean"] * 1e-9
    n_ok = int(np.sum(np.isfinite(x)))
    if n < 3:
        return None
    ms = np.unique(np.round(np.logspace(0, np.log10(max(1, n // 4)), n_tau)).astype(int))
    taus, devs, counts = mdev(x, float(LOG_BLOCK_S), ms)
    # as the one-second path: no point resting on less than one independent term
    keep = [i for i, (t, k) in enumerate(zip(taus, counts)) if k >= t / LOG_BLOCK_S]
    taus, devs, counts = ([v[i] for i in keep] for v in (taus, devs, counts))
    if not taus:
        return None
    return {"tau": taus, "mdev": devs, "err": mdev_errors(taus, devs, counts, float(LOG_BLOCK_S)),
            "since_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
            "span_s": n * LOG_BLOCK_S, "n_blocks": n_ok, "n_gaps": n - n_ok,
            "time_constant_s": settings["time_constant_s"], "damping": settings["damping"],
            "cable_delay_ns": settings["cable_delay_ns"]}


def stability(rows, n_tau=25, log_blocks=None):
    """The panel's stability plot from the monitor's rows (wall, osc_ppb,
    pps_ns, ...): measured MDEV with error bars and the transition. With the
    long-term log's rows (`read_log`) it adds `log`, the same quantity at
    tau >= 10 min over however long the log reaches (`log_stability`); the
    transition is then read off the one-second points below the log's first
    tau and the log's points from there, provided the log spans at least the
    one-second record - otherwise from the one-second points alone."""
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
    errs = mdev_errors(taus, devs, counts, 1.0)
    # The white-phase-noise expectation, MDEV = sqrt(3) sigma_x tau^-3/2 for
    # one-second samples, at the record's own scatter about a straight line:
    # measured, not fitted. The unit smooths what it reports, so the small-tau
    # points sit far below it; it describes them only once tau is past that.
    t = np.arange(n, dtype=float)
    sigma_x = float(np.std(x - np.polyval(np.polyfit(t, x, 1), t)))
    lg = log_stability(log_blocks) if log_blocks else None
    tt, td, te, source = taus, devs, errs, "one-second"
    if lg and lg["span_s"] >= n:
        cut = lg["tau"][0]
        below = [i for i, v in enumerate(taus) if v < cut]
        tt = [taus[i] for i in below] + lg["tau"]
        td = [devs[i] for i in below] + lg["mdev"]
        te = [errs[i] for i in below] + lg["err"]
        source = "one-second + log"
    return {"ok": True, "n_s": n, "gap_trimmed": i0, "tau": taus, "mdev": devs, "err": errs,
            "log": lg, "transition": transition(tt, td, te), "transition_from": source,
            "sigma_x_s": sigma_x, "reference": REFERENCE_TBOLT_VS_CAESIUM}
