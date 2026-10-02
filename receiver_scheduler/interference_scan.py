"""Interference survey: the spectrum the front end passes, against azimuth.

A site survey to sit beside the horizon scan. The dish holds one altitude and
steps round in azimuth; at each step the B200 is stepped across everything the
front-end filters let through and the mean (and peak) power spectrum is kept.
The result is a picture of where every signal in the band comes from - the
mast's downlink at 1474-1490 MHz, whatever reaches the protected band, and
anything new - drawn as azimuth against frequency on a logarithmic colour
scale.

The band is the filters' passband, not a choice: the 700-2700 MHz survey of
2026-09-29 (data/interference/wide_survey.npz, gain 0 dB) puts clear sky above
the B200's own floor only over ~1370-1450 MHz, and the mast's signals above it
over 1340-1500 MHz with nothing outside. Seven 32 Msps tunings, each keeping
its central 25 MHz where the B200's analogue and decimation filters are flat,
cover 1332.5-1507.5 MHz with a margin both sides.

The gain is 0 dB by default because pointed at the mast the B200 clips at
20 dB from 1395 MHz up; every tuning's ADC peak is recorded so a clipped
column is marked rather than believed. The B200's own floor is read at
2500 MHz, outside the filters, at the start and the end: dividing by it takes
out the IF response of each tuning, and puts the plot in dB above the
receiver's own noise, which is what says whether a signal is there at all.

Captures are bursts of 2^20 samples, transformed and averaged between bursts,
so a 60 s dwell holds less than 60 s of samples - the effective integration
is recorded per tuning. A site survey wants the dwell, which catches signals
that come and go, more than the duty cycle.
"""
from __future__ import annotations

import io
import json
import logging
import os
import time
from datetime import datetime, timezone

import numpy as np

log = logging.getLogger("interference_scan")

SAMPLE_RATE_HZ = 32e6
NFFT = 4096                                    # 7.8 kHz bins at 32 Msps
KEEP_HZ = 25e6                                 # the flat middle of each tuning
LOS_HZ = tuple(1345e6 + 25e6 * i for i in range(7))     # 1332.5-1507.5 MHz
FLOOR_LO_HZ = 2500e6                           # outside every filter: the B200's own noise
BURST = 1 << 20                                # 33 ms of samples per capture
CLIP_LEVEL = 0.9                               # of the fc32 full scale of 1.0
UHD_ARGS = "type=b200,num_recv_frames=512"     # as every other source (overflows-were-usb-buffering)

DEFAULT_ALT = 10.0
DEFAULT_AZ_START = 5.0                         # true azimuth 0 is outside the mount's range
DEFAULT_AZ_END = 345.0                         # the cabling is strained beyond ~345; 355 hit the upper switch (2026-10-01)
DEFAULT_AZ_STEP = 5.0
DEFAULT_DWELL_S = 60.0                         # per azimuth, shared across the tunings
DEFAULT_GAIN_DB = 0.0
FLOOR_DWELL_S = 3.0

PLOT_BIN_HZ = 250e3                            # display resolution in frequency
# Lines drawn across the plot: the protected band and the UK mobile blocks
# nearest it (Ofcom; 1452-1472 Vodafone, 1472-1492 Three, both VodafoneThree).
BAND_EDGES_MHZ = ((1400.0, "1400 protected"), (1427.0, "1427 protected"),
                  (1452.0, "1452 mobile"), (1472.0, "1472 mobile"), (1492.0, "1492 mobile"))
HI_LINE_MHZ = 1420.405752


from sun_scan import ScanCancelled           # what _slew_to and the homing raise on a stop


def _cancelled(cancel_event):
    return cancel_event is not None and cancel_event.is_set()


def tuning_bins():
    """Which FFT bins of a tuning are kept, and their offsets from the LO (Hz)."""
    f = np.fft.fftshift(np.fft.fftfreq(NFFT, 1.0 / SAMPLE_RATE_HZ))
    keep = (f >= -KEEP_HZ / 2) & (f < KEEP_HZ / 2)     # 3200 bins, so tunings abut on one grid
    return keep, f[keep]


def survey_frequencies():
    """The frequency axis of a whole survey, tuning after tuning (Hz)."""
    _, off = tuning_bins()
    return np.concatenate([lo + off for lo in LOS_HZ])


class _B200Spectrometer:
    """One B200 session for the whole survey: tune, capture bursts, average."""

    def __init__(self, gain_db):
        import uhd
        self.uhd = uhd
        self.usrp = uhd.usrp.MultiUSRP(UHD_ARGS)
        self.usrp.set_rx_antenna("RX2", 0)
        self.usrp.set_rx_rate(SAMPLE_RATE_HZ, 0)
        self.usrp.set_rx_bandwidth(SAMPLE_RATE_HZ, 0)
        self.usrp.set_rx_gain(gain_db, 0)
        args = uhd.usrp.StreamArgs("fc32", "sc16")
        args.channels = [0]
        self.streamer = self.usrp.get_rx_stream(args)
        self.chunk = np.empty((1, self.streamer.get_max_num_samps()), np.complex64)
        self.overflows = 0
        self.window = np.blackman(NFFT).astype(np.float32)
        log.info("B200 ready for the interference survey: %.0f Msps, gain %.1f dB",
                 SAMPLE_RATE_HZ / 1e6, gain_db)

    def _burst(self, n):
        uhd = self.uhd
        cmd = uhd.types.StreamCMD(uhd.types.StreamMode.num_done)
        cmd.num_samps = n
        cmd.stream_now = True
        self.streamer.issue_stream_cmd(cmd)
        buf = np.empty(n, np.complex64)
        md = uhd.types.RXMetadata()
        got = 0
        while got < n:
            k = self.streamer.recv(self.chunk, md, 1.0)
            if md.error_code == uhd.types.RXMetadataErrorCode.overflow:
                self.overflows += 1
                continue
            if md.error_code != uhd.types.RXMetadataErrorCode.none:
                break
            take = min(k, n - got)
            buf[got:got + take] = self.chunk[0, :take]
            got += take
        return buf[:got]

    def set_pointing(self, alt, az):
        pass

    def measure(self, lo_hz, dwell_s, cancel_event=None):
        """Mean and peak power spectra (kept bins, linear counts), the seconds
        of samples they hold, and the largest ADC sample seen."""
        self.usrp.set_rx_freq(self.uhd.types.TuneRequest(lo_hz), 0)
        time.sleep(0.05)
        self._burst(BURST // 4)                   # the retune's transient
        keep, _ = tuning_bins()
        total = np.zeros(int(keep.sum()))
        peak = np.zeros_like(total)
        n, samples, adc_peak = 0, 0, 0.0
        t_end = time.monotonic() + dwell_s
        while time.monotonic() < t_end and not _cancelled(cancel_event):
            x = self._burst(BURST)
            m = len(x) // NFFT
            if m == 0:
                continue
            adc_peak = max(adc_peak, float(np.max(np.abs(x.view(np.float32)))))
            s = np.fft.fftshift(np.abs(np.fft.fft(x[:m * NFFT].reshape(m, NFFT) * self.window, axis=1)) ** 2,
                                axes=1).mean(axis=0)[keep]
            total += s
            np.maximum(peak, s, out=peak)
            n += 1
            samples += m * NFFT
        mean = total / max(n, 1)
        return mean, peak, samples / SAMPLE_RATE_HZ, adc_peak

    def close(self):
        self.streamer = None
        self.usrp = None


class _DemoSpectrometer:
    """A synthetic site for tests and for trying the page with no radio: the
    filters' passband over the floor, and a mast at azimuth 86 whose downlink
    (1474-1490) and protected-band blocks (1391-1429) rise with the beam."""

    def __init__(self, gain_db, seed=0):
        self.rng = np.random.default_rng(seed)
        self.alt, self.az = 90.0, 0.0
        self.overflows = 0

    def set_pointing(self, alt, az):
        self.alt, self.az = float(alt), float(az)

    def measure(self, lo_hz, dwell_s, cancel_event=None):
        _, off = tuning_bins()
        f = (lo_hz + off) / 1e6
        if lo_hz == FLOOR_LO_HZ:
            p = np.ones_like(f)
        else:
            sky = 30.0 / (1 + np.exp(-(f - 1372) / 2)) / (1 + np.exp((f - 1448) / 2))
            d = abs((self.az - 86.0 + 180) % 360 - 180)
            beam = np.exp(-0.5 * (d / 1.9) ** 2) * np.exp(-0.5 * ((self.alt - 10) / 4) ** 2)
            mast = beam * (400 * ((f > 1474) & (f < 1490)) + 100 * ((f > 1391) & (f < 1429)))
            p = 1.0 + sky + mast
        p = p * (1 + 0.01 * self.rng.standard_normal(len(f)))
        return p, p * 1.05, min(dwell_s, 0.01), 0.1

    def close(self):
        pass


def open_spectrometer(sdr_type, gain_db):
    if sdr_type == "demo":
        return _DemoSpectrometer(gain_db)
    if sdr_type in ("b210", "b200"):
        return _B200Spectrometer(gain_db)
    raise ValueError("the interference survey needs the B200 or the demo SDR, not %r" % sdr_type)


def interference_scan(alt=DEFAULT_ALT, az_start=DEFAULT_AZ_START, az_end=DEFAULT_AZ_END,
                      az_step=DEFAULT_AZ_STEP, dwell_s=DEFAULT_DWELL_S, gain_db=DEFAULT_GAIN_DB,
                      sdr_type="b210", srt_url="", home_first=True, out_dir=".",
                      slew_timeout=150, position_tolerance=0.5,
                      progress_callback=None, cancel_event=None):
    """Run the survey and save it; returns the path of the saved file.

    The file is written whether the survey completes or is stopped, with
    `complete` saying which: an hour of measurements is worth keeping even
    when the last azimuths were never reached."""
    from sun_scan import _slew_to, _srt_api
    base_url = (srt_url or "").rstrip("/")
    demo = sdr_type == "demo"
    if not demo and not base_url:
        raise RuntimeError("No SRT controller URL configured")
    azimuths = [float(a) for a in np.arange(az_start, az_end + 1e-6, az_step)]
    if not azimuths:
        raise ValueError("no azimuths between %.1f and %.1f" % (az_start, az_end))
    per_tuning = float(dwell_s) / len(LOS_HZ)
    started = datetime.now(timezone.utc)
    rows = {k: [] for k in ("az", "mean", "peak", "eff_s", "adc_peak", "true_alt", "true_az",
                            "drive_alt", "drive_az", "utc")}
    floors = []
    spec = open_spectrometer(sdr_type, gain_db)
    complete = False
    try:
        floors.append(spec.measure(FLOOR_LO_HZ, FLOOR_DWELL_S, cancel_event)[0])
        if home_first and not demo:
            from horizon_scan import _home_and_wait
            _home_and_wait(base_url, cancel_event=cancel_event)
        for i, az in enumerate(azimuths):
            if _cancelled(cancel_event):
                raise ScanCancelled("interference survey stopped")
            if progress_callback:
                progress_callback(i, len(azimuths), {"az": az, "alt": alt, "stage": "slewing"})
            status = {}
            if not demo:
                _slew_to(base_url, alt, az, slew_timeout=slew_timeout,
                         position_tolerance=position_tolerance, cancel_event=cancel_event)
                status = _srt_api(base_url, "/status")
            spec.set_pointing(alt, az)
            means, peaks, effs, adcs = [], [], [], []
            for j, lo in enumerate(LOS_HZ):
                if progress_callback:
                    progress_callback(i, len(azimuths), {"az": az, "alt": alt, "stage": "measuring",
                                                         "tuning": j + 1, "of": len(LOS_HZ)})
                m, p, e, a = spec.measure(lo, per_tuning, cancel_event)
                means.append(m)
                peaks.append(p)
                effs.append(e)
                adcs.append(a)
            if _cancelled(cancel_event):
                raise ScanCancelled("interference survey stopped")
            rows["az"].append(az)
            rows["mean"].append(np.concatenate(means))
            rows["peak"].append(np.concatenate(peaks))
            rows["eff_s"].append(effs)
            rows["adc_peak"].append(adcs)
            for key, field, default in (("true_alt", "true_alt", alt), ("true_az", "true_az", az),
                                        ("drive_alt", "alt", alt), ("drive_az", "az", az)):
                try:
                    rows[key].append(float(status.get(field, default)))
                except (TypeError, ValueError):
                    rows[key].append(float("nan"))
            rows["utc"].append(datetime.now(timezone.utc).isoformat())
            if progress_callback:
                clipped = max(adcs) >= CLIP_LEVEL
                progress_callback(i + 1, len(azimuths), {"az": az, "alt": alt, "stage": "done",
                                                         "clipped": clipped})
        complete = True
    except ScanCancelled:
        log.info("Interference survey stopped after %d of %d azimuths", len(rows["az"]), len(azimuths))
    finally:
        try:
            if not _cancelled(cancel_event) or rows["az"]:
                floors.append(spec.measure(FLOOR_LO_HZ, FLOOR_DWELL_S)[0])
        except Exception as exc:                      # noqa: BLE001
            log.warning("Could not read the closing B200 floor: %s", exc)
        overflows = getattr(spec, "overflows", 0)
        spec.close()
        path = None
        if rows["az"]:
            path = save_scan(out_dir, started, rows, floors, {
                "alt_deg": float(alt), "az_step_deg": float(az_step), "dwell_s": float(dwell_s),
                "gain_db": float(gain_db), "sdr_type": sdr_type, "complete": complete,
                "n_planned": len(azimuths), "overflows": int(overflows),
                "sample_rate_hz": SAMPLE_RATE_HZ, "nfft": NFFT, "keep_hz": KEEP_HZ,
                "los_hz": list(LOS_HZ), "floor_lo_hz": FLOOR_LO_HZ, "clip_level": CLIP_LEVEL,
                "home_first": bool(home_first)})
    if not complete and not rows["az"]:
        raise ScanCancelled("interference survey stopped before any azimuth was measured")
    return path


def scan_name(started):
    return "interference_%s" % started.strftime("%Y%m%dT%H%M%SZ")


def save_scan(out_dir, started, rows, floors, meta):
    os.makedirs(out_dir, exist_ok=True)
    meta = dict(meta, started_utc=started.isoformat(),
                finished_utc=datetime.now(timezone.utc).isoformat(), n_azimuths=len(rows["az"]))
    path = os.path.join(out_dir, scan_name(started) + ".npz")
    floor = np.mean(floors, axis=0) if floors else np.ones(int(tuning_bins()[0].sum()))
    np.savez_compressed(
        path, freq_hz=survey_frequencies(), az_deg=np.array(rows["az"]),
        mean=np.array(rows["mean"], np.float32), peak=np.array(rows["peak"], np.float32),
        floor_tuning=np.asarray(floor, np.float64), floor_each=np.array(floors, np.float64),
        eff_s=np.array(rows["eff_s"]), adc_peak=np.array(rows["adc_peak"]),
        true_alt=np.array(rows["true_alt"]), true_az=np.array(rows["true_az"]),
        drive_alt=np.array(rows["drive_alt"]), drive_az=np.array(rows["drive_az"]),
        utc=np.array(rows["utc"]), meta=json.dumps(meta))
    log.info("Interference survey saved: %s (%d azimuths%s)", path, len(rows["az"]),
             "" if meta.get("complete") else ", incomplete")
    return path


def load_scan(path):
    with np.load(path, allow_pickle=False) as d:
        out = {k: d[k] for k in d.files}
    out["meta"] = json.loads(str(out["meta"]))
    return out


def list_scans(out_dir):
    """Saved surveys, newest first: name, start, altitude, azimuths, complete."""
    out = []
    if not os.path.isdir(out_dir):
        return out
    for fn in sorted(os.listdir(out_dir), reverse=True):
        if not (fn.startswith("interference_") and fn.endswith(".npz")):
            continue
        try:
            with np.load(os.path.join(out_dir, fn), allow_pickle=False) as d:
                meta = json.loads(str(d["meta"]))
        except Exception:                             # noqa: BLE001
            continue
        out.append({"name": fn[:-4], "started_utc": meta.get("started_utc"),
                    "alt_deg": meta.get("alt_deg"), "n_azimuths": meta.get("n_azimuths"),
                    "n_planned": meta.get("n_planned"), "complete": meta.get("complete"),
                    "sdr_type": meta.get("sdr_type"), "gain_db": meta.get("gain_db"),
                    "dwell_s": meta.get("dwell_s")})
    return out


def scan_path(out_dir, name):
    """The file for a survey name, refusing anything that is not one."""
    base = os.path.basename(str(name))
    if base != name or not base.startswith("interference_"):
        raise ValueError("not an interference survey name: %r" % name)
    return os.path.join(out_dir, base + ".npz")


def _binned(freq_hz, power, how):
    """Average (or take the max of) native bins into PLOT_BIN_HZ bins."""
    k = max(1, int(round(PLOT_BIN_HZ / (freq_hz[1] - freq_hz[0]))))
    n = power.shape[-1] // k * k
    p = power[..., :n].reshape(power.shape[:-1] + (n // k, k))
    f = freq_hz[:n].reshape(n // k, k).mean(axis=1)
    return f, (p.max(axis=-1) if how == "max" else p.mean(axis=-1))


def relative_db(scan, mode="floor", stat="mean"):
    """(az, freq_hz, dB) for the plot. mode 'floor': power over the B200's own
    floor, so 0 dB is nothing there. mode 'median': over each frequency's
    median across azimuth, so what is the same in every direction (the
    passband, the sky) cancels and a directional source stands out."""
    power = np.asarray(scan["peak" if stat == "peak" else "mean"], float)
    floor = np.tile(np.asarray(scan["floor_tuning"], float), len(scan["meta"]["los_hz"]))
    f, p = _binned(scan["freq_hz"], power, "max" if stat == "peak" else "mean")
    _, fl = _binned(scan["freq_hz"], floor[None, :], "mean")
    if mode == "median":
        ref = np.median(p, axis=0, keepdims=True)
    else:
        ref = fl
    return np.asarray(scan["az_deg"], float), f, 10 * np.log10(np.maximum(p, 1e-30) / np.maximum(ref, 1e-30))


def plot_scan(scan, mode="floor", stat="mean", horizon_floor=None):
    """The survey as a PNG (bytes): azimuth across, frequency up, dB in colour,
    with the band power of the protected band and the mobile blocks above.
    `horizon_floor(az)` (optional) marks azimuths where the measured horizon
    is above the survey's altitude."""
    from plot_backend import use_headless
    use_headless()
    import matplotlib.pyplot as plt
    from sun_scan import _style_dark
    meta = scan["meta"]
    az, f, db = relative_db(scan, mode, stat)
    fm = f / 1e6
    step = float(meta.get("az_step_deg") or (az[1] - az[0] if len(az) > 1 else 5.0))
    edges_az = np.concatenate([az - step / 2, [az[-1] + step / 2]])
    df = fm[1] - fm[0]
    edges_f = np.concatenate([fm - df / 2, [fm[-1] + df / 2]])

    fig = plt.figure(figsize=(12, 8))
    gs = fig.add_gridspec(2, 2, height_ratios=[1, 3.4], width_ratios=[40, 1], hspace=0.08, wspace=0.03)
    ax_top = fig.add_subplot(gs[0, 0])
    ax = fig.add_subplot(gs[1, 0], sharex=ax_top)
    cax = fig.add_subplot(gs[1, 1])

    if mode == "median":
        vmin, vmax, label = -2.0, max(10.0, float(np.nanpercentile(db, 99.8))), "dB over the median across azimuth"
    else:
        vmin, vmax, label = 0.0, max(10.0, float(np.nanpercentile(db, 99.8))), "dB over the B200's own floor"
    mesh = ax.pcolormesh(edges_az, edges_f, db.T, cmap="magma", vmin=vmin, vmax=vmax, shading="flat")
    cb = fig.colorbar(mesh, cax=cax)
    cb.set_label(label)
    for mhz, text in BAND_EDGES_MHZ:
        if fm[0] < mhz < fm[-1]:
            protected = "protected" in text
            ax.axhline(mhz, color="#00d4ff" if protected else "#bbbbbb", lw=0.8, ls="--", alpha=0.8)
            ax.text(0.004, mhz, text, transform=ax.get_yaxis_transform(), fontsize=7.5,
                    color="#00d4ff" if protected else "#bbbbbb", va="bottom", ha="left")
    ax.axhline(HI_LINE_MHZ, color="white", lw=0.6, ls=":", alpha=0.7)
    ax.set_xlabel("azimuth (deg, true)")
    ax.set_ylabel("frequency (MHz)")
    ax.set_xlim(edges_az[0], edges_az[-1])
    ax.set_ylim(edges_f[0], edges_f[-1])
    ticks = np.arange(0, 361, 30)
    ax.set_xticks(ticks[(ticks >= edges_az[0]) & (ticks <= edges_az[-1])])

    # Above: band-mean power against azimuth, on the same reference
    lin = 10 ** (db / 10)
    for (lo, hi), colour, name in (((1400, 1427), "#00d4ff", "1400-1427 protected"),
                                   ((1452, 1492), "#eda100", "1452-1492 mobile"),
                                   ((1350, 1400), "#9be37a", "1350-1400")):
        sel = (fm >= lo) & (fm < hi)
        if sel.any():
            ax_top.plot(az, 10 * np.log10(lin[:, sel].mean(axis=1)), color=colour, lw=1.4, label=name)
    clipped = np.asarray(scan["adc_peak"]).max(axis=1) >= float(meta.get("clip_level", CLIP_LEVEL))
    if clipped.any():
        ax_top.plot(az[clipped], np.full(clipped.sum(), ax_top.get_ylim()[1]), "v", color="#ff4757",
                    ms=6, label="ADC clipped", clip_on=False)
    if horizon_floor is not None:
        blocked = np.array([horizon_floor(a) >= meta["alt_deg"] for a in az])
        if blocked.any():
            ax_top.plot(az[blocked], np.full(blocked.sum(), ax_top.get_ylim()[0]), "s", color="#888888",
                        ms=3, label="horizon above this altitude", clip_on=False)
    ax_top.set_ylabel("band mean (dB)")
    ax_top.legend(fontsize=8, loc="upper right", ncol=4, framealpha=0.3)
    plt.setp(ax_top.get_xticklabels(), visible=False)

    when = str(meta.get("started_utc", ""))[:16].replace("T", " ")
    eff = float(np.median(np.asarray(scan["eff_s"]).sum(axis=1)))
    fig.suptitle("Interference survey %s UTC - alt %.0f deg, gain %.0f dB, %.0f s per azimuth (%.1f s of samples)%s%s\n"
                 "%s power, %s" % (
                     when, meta["alt_deg"], meta["gain_db"], meta["dwell_s"], eff,
                     "" if meta.get("complete") else " - incomplete (%d of %d azimuths)"
                     % (meta.get("n_azimuths", len(az)), meta.get("n_planned", len(az))),
                     " - DEMO" if meta.get("sdr_type") == "demo" else "",
                     "peak" if stat == "peak" else "mean",
                     "relative to each frequency's median across azimuth" if mode == "median"
                     else "relative to the B200's own floor"), fontsize=11)
    _style_dark(fig)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
