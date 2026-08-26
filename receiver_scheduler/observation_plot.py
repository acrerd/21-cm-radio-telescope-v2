"""Plot a finished observation's HDF5 file.

The receiver draws the spectrum in its own Qt window on the observatory
console, which is no use over a tunnel and no use at all once the run has
ended. This renders the same two views from the file the run left behind, for
the scheduler's Observe tab.

Finished, deliberately. The receiver writes without SWMR, so a second process
opening the file while it is being written hits HDF5's file lock - the same
lock two receiver instances already fight over. `plot_observation` refuses a
file that is still open rather than returning a confusing h5py error. Live
display belongs to the receiver rewrite (issue #15).

The two views are chosen to match what each observation type is for:

  spectrum  the time-averaged spectrum against frequency, with a second axis
            in Doppler velocity. That axis is in **LSR** whenever the direction
            and epoch can be worked out, which is what H I is quoted in
            everywhere; the correction reaches ~30 km/s and changes with the
            direction and the date, so without it nothing recorded here could
            be compared with published data or with the simulator. It is
            applied at the observation's own epoch and subtracted for display
            only - the recorded file stays raw and can be re-reduced.

            Where the direction is unknown the axis stays topocentric and says
            so, because a velocity axis silently in the wrong frame is worse
            than one honestly labelled.

  drift     band power against time, which for a drift scan is the whole
            result: the source enters, peaks and leaves. The expected transit
            is marked, since the scan is laid out so the source crosses the
            beam centre at the mid-point, and whether the peak lands there is
            the thing worth seeing.
"""

import logging
import math
import os
from datetime import datetime, timezone

import numpy as np

import bandpass
from observatory import SITE_HEIGHT_M, SITE_LAT_DEG, SITE_LON_DEG

SIMULATOR_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "astro_simulator")

log = logging.getLogger("scheduler")

try:
    import h5py
    H5PY_AVAILABLE = True
except ImportError:            # pragma: no cover - environment dependent
    H5PY_AVAILABLE = False

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except ImportError:            # pragma: no cover - environment dependent
    MATPLOTLIB_AVAILABLE = False

# Matched to sun_scan's plots so the tabs look like one application.
_PLOT_FIG_BG = "#0f0f23"
_PLOT_AXES_BG = "#16162e"
_PLOT_FG = "#c8c8d8"
_PLOT_GRID = "#333355"
_ACCENT = "#00d4ff"
_MARK = "#ffa502"

H1_REST_FREQ_HZ = 1420.405752e6
C_KM_S = 299792.458


def _style_dark(fig):
    """Make a figure readable against the scheduler's dark page."""
    fig.patch.set_facecolor(_PLOT_FIG_BG)
    for ax in fig.get_axes():
        ax.set_facecolor(_PLOT_AXES_BG)
        for spine in ax.spines.values():
            spine.set_color(_PLOT_GRID)
        ax.tick_params(colors=_PLOT_FG, which="both")
        ax.xaxis.label.set_color(_PLOT_FG)
        ax.yaxis.label.set_color(_PLOT_FG)
        ax.title.set_color(_PLOT_FG)
        ax.grid(True, color=_PLOT_GRID, alpha=0.4)
        ax.set_axisbelow(True)
        legend = ax.get_legend()
        if legend is not None:
            legend.get_frame().set_facecolor(_PLOT_AXES_BG)
            legend.get_frame().set_edgecolor(_PLOT_GRID)
            for text in legend.get_texts():
                text.set_color(_PLOT_FG)


def _is_open_for_writing(path):
    """True if some process still holds the file open for writing.

    Asked through /proc rather than by trying to open it: an h5py open that
    fails on the lock can leave the caller unable to tell "still recording"
    from "corrupt", and those want different messages.
    """
    try:
        target = os.path.realpath(path)
    except OSError:
        return False
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:            # not Linux; fall back to allowing the read
        return False
    for pid in pids:
        fd_dir = f"/proc/{pid}/fd"
        try:
            for fd in os.listdir(fd_dir):
                try:
                    if os.path.realpath(os.path.join(fd_dir, fd)) == target:
                        return True
                except OSError:
                    continue
        except OSError:
            continue           # process gone, or not ours to look at
    return False


def read_observation(path):
    """Load the spectra, timestamps and frequency axis from a finished file."""
    if not H5PY_AVAILABLE:
        raise RuntimeError("h5py is not installed, so the file cannot be read")
    if not os.path.exists(path):
        raise FileNotFoundError(f"No such observation file: {path}")
    if _is_open_for_writing(path):
        raise RuntimeError(
            "The observation is still recording - its plot can be drawn once "
            "it has finished")
    with h5py.File(path, "r") as hf:
        freq_hz = np.asarray(hf["frequency_hz"][:], dtype=float)
        # Recordings are stored in kelvin when the instrument was calibrated
        # for their tuning, and in counts when it was not. This always hands
        # back *counts*, reversing the calibration where one was applied.
        #
        # That is not a wasted round trip. Storing kelvin is for whoever opens
        # the file in a notebook or another language; this pipeline has to work
        # in counts, because fitting a gain from spectra that a gain has
        # already been applied to would be circular - reduce_for_fit would
        # dutifully return unity and a system temperature of zero.
        if "spectra_kelvin" in hf:
            spectra = np.asarray(hf["spectra_kelvin"][:], dtype=float)
            correction = np.asarray(hf["bandpass_correction"][:], dtype=float)
            spectra = ((spectra + float(hf.attrs["applied_t_sys_k"]))
                       * float(hf.attrs["applied_gain_counts_per_k"]) * correction)
        else:
            spectra = np.asarray(hf["spectra_linear"][:], dtype=float)
        stamps = np.asarray(hf["timestamps"][:], dtype=float)
        # How long each record integrated for. Their sum is the total
        # integration, which is what the averaged spectrum's noise corresponds
        # to - the per-record value is only a recording granularity.
        if "integration_times" in hf:
            taus = np.asarray(hf["integration_times"][:], dtype=float)
        else:
            taus = np.array([], dtype=float)
        header = dict(hf.attrs)
    if spectra.ndim != 2 or spectra.shape[0] == 0:
        raise ValueError("The observation file holds no spectra")
    return freq_hz, spectra, stamps, taus, header


def requested_band(header):
    """The band that was asked for, which is what should be drawn.

    The receiver tunes the LO off the line and widens the sample rate to suit,
    so the recorded band is wider than the requested one and is centred
    somewhere else entirely. None of that is the observer's business: they
    asked for a bandwidth around a frequency, the simulator that set the
    observation up drew exactly that, and the plot should match it. Trimming
    also drops the LO artefact and the filter roll-off, so the vertical scale
    belongs to the part of the spectrum that matters.

    Returns (low_hz, high_hz), or None for a file that predates the offset.
    """
    centre = header.get("sky_center_freq_hz")
    width = header.get("sample_rate_requested_hz")
    if centre is None or width is None:
        return None
    centre, width = float(centre), float(width)
    if not (np.isfinite(centre) and np.isfinite(width)) or width <= 0:
        return None
    return centre - width / 2.0, centre + width / 2.0


def patch_dc_artefact(freq_hz, spectra, header, half_window=48):
    """Interpolate across the LO's DC artefact, for display only.

    The receiver tunes the LO away from the line, so the artefact lands in a
    part of the band carrying nothing but smooth bandpass - and interpolating
    across a few channels of that invents nothing. Doing the same thing while
    tuned at the line would have been inventing the measurement, which is why
    the offset had to come first.

    The recorded spectra are never modified. This returns a copy for drawing,
    and the count of channels patched so the plot can say so: a patched
    spectrum that does not announce itself is worse than a visible defect.

    The width is found rather than assumed. It depends on the channel width -
    the same defect spanned three channels at 6.1 kHz and far fewer at 0.5 kHz -
    so a fixed channel count would be wrong at every resolution but one.
    """
    where = header.get("dc_artefact_freq_hz")
    if where is None or spectra.size == 0:
        return spectra, 0, None
    where = float(where)
    if not (freq_hz[0] <= where <= freq_hz[-1]):
        return spectra, 0, None            # outside the drawn band already

    mean = spectra.mean(axis=0)
    k = int(np.argmin(np.abs(freq_hz - where)))
    lo = max(0, k - half_window)
    hi = min(len(mean), k + half_window + 1)
    # A baseline from the window's outer thirds, which the artefact does not
    # reach, and a scatter to judge the inner channels against.
    edge = np.concatenate([mean[lo:k - 6], mean[k + 7:hi]])
    if edge.size < 8:
        return spectra, 0, None
    baseline = float(np.median(edge))
    scatter = float(np.median(np.abs(edge - baseline))) * 1.4826
    if scatter <= 0:
        return spectra, 0, None

    bad = np.zeros(len(mean), dtype=bool)
    for i in range(max(lo, k - 12), min(hi, k + 13)):
        if abs(mean[i] - baseline) > 6.0 * scatter:
            bad[i] = True
    if not bad.any():
        return spectra, 0, None
    # Take the contiguous run containing the artefact, so an unrelated spike
    # elsewhere in the window is left alone to be seen.
    left = right = k
    while left - 1 >= 0 and bad[left - 1]:
        left -= 1
    while right + 1 < len(mean) and bad[right + 1]:
        right += 1
    if not bad[k]:
        return spectra, 0, None

    patched = spectra.copy()
    good = np.array([left - 1, right + 1])
    if good[0] < 0 or good[1] >= len(mean):
        return spectra, 0, None
    for row in range(patched.shape[0]):
        patched[row, left:right + 1] = np.interp(
            freq_hz[left:right + 1], freq_hz[good], patched[row, good])
    return patched, right - left + 1, where


def plot_observation(path, output_path, name="", mode="spectrum",
                     transit_minutes=None, figsize=(16.0, 9.0), dpi=120):
    """Render a finished observation to a PNG. Returns the output path."""
    if not MATPLOTLIB_AVAILABLE:
        raise RuntimeError("matplotlib is not installed, so no plot can be drawn")
    freq_hz, spectra, stamps, taus, header = read_observation(path)

    band = requested_band(header)
    if band is not None:
        keep = (freq_hz >= band[0]) & (freq_hz <= band[1])
        # Only trim if the request actually lies inside what was recorded; a
        # mismatch means the header is not describing this file and the honest
        # thing is to draw all of it.
        if keep.sum() >= 16:
            freq_hz = freq_hz[keep]
            spectra = spectra[:, keep]
    # Divide out the measured instrument response before anything else looks at
    # the shape. Done here rather than in the receiver so the recorded files stay
    # raw and can be re-reduced against a better template later; done after the
    # trim so the polynomial is never asked to extrapolate past the band it was
    # fitted over. Refuses itself if the tuning does not match - see bandpass.py.
    spectra, bandpass_note = bandpass.apply_bandpass(freq_hz, spectra, header)
    spectra, n_patched, patched_at = patch_dc_artefact(freq_hz, spectra, header)

    # If a gain calibration applies to this tuning, put the spectrum in kelvin.
    # Same rule as the bandpass: it belongs to a tuning and a receiver gain, and
    # using it on another would be worse than leaving counts alone, because
    # counts are honestly unlabelled and wrong kelvin are not.
    import rf_calibration
    cal, cal_why, cal_source = rf_calibration.calibration_for(header)
    cal_ok = cal is not None
    if cal_ok:
        spectra = spectra / cal["gain_counts_per_k"] - cal["t_sys_k"]
    n = spectra.shape[0]
    started = (datetime.fromtimestamp(stamps[0], tz=timezone.utc)
               if stamps.size else None)
    title = name or os.path.basename(path)
    # The plotted spectrum is the average of every record, so the integration
    # its noise corresponds to is the total, not the per-record granularity.
    # Quote the total and let the record count explain where it came from.
    total_s = float(np.nansum(taus)) if taus is not None and taus.size else None
    if total_s:
        if total_s >= 60:
            total_text = "%.1f min total integration" % (total_s / 60.0)
        else:
            total_text = "%.0f s total integration" % total_s
        subtitle = "%s (%d record%s)" % (total_text, n, 's' if n != 1 else '')
    else:
        subtitle = f"{n} record{'s' if n != 1 else ''}"
    if started is not None:
        subtitle += f", from {started:%Y-%m-%d %H:%M:%S} UTC"
    if n_patched:
        subtitle += ("\n%d channel%s interpolated across the LO artefact at %.4f MHz"
                     % (n_patched, "s" if n_patched != 1 else "", patched_at / 1e6))
    # Always say whether the response was divided out, including when it was
    # not: a flat-looking spectrum that has silently been through a template is
    # indistinguishable from one that has not, and the difference matters.
    subtitle += "\n" + bandpass_note
    if cal_ok:
        # Say which gain was used. Normally the current one; for an archived
        # observation whose tuning nobody uses any more it is the one carried
        # in the file, and a reader comparing two plots needs to know which.
        subtitle += ("\ncalibrated to kelvin: T_sys %.0f K, gain measured %s UTC%s"
                     % (cal["t_sys_k"],
                        (cal.get("observed_utc") or "")[:16].replace("T", " "),
                        "" if cal_source == "current" else " (%s)" % cal_source))
    elif "no gain calibration" not in cal_why:
        subtitle += "\nin counts, not kelvin - " + cal_why

    # 1920x1080, matching the calibration plots. Read on the observatory
    # console and never on a phone, and the whole point of a spectrum is fine
    # structure across the band - at 900 px a 0.49 kHz channel is a fifth of a
    # pixel and the line profile is whatever the resampling decided.
    # The velocity frame is evaluated at the middle of the observation, and at
    # the observation's own epoch rather than now: the barycentric term moves
    # 1.95 km/s in a week. For a long run the frame drifts a little across it -
    # a couple of hundred m/s in an hour - and the mid-point is the honest
    # single value for an averaged spectrum.
    mid = None
    if stamps.size:
        mid = datetime.fromtimestamp(float(np.mean([stamps[0], stamps[-1]])),
                                     tz=timezone.utc)
    lsr = lsr_offset_km_s(header, mid)

    # The receiver's own clock offset, on top of the frame. An error in the
    # B210's TCXO scales the whole frequency axis, which across 2 MHz is a pure
    # velocity shift - measured at -2.36 +- 0.27 ppm, or -0.71 +- 0.08 km/s,
    # which is 7 channels at 0.49 kHz.
    #
    # Carried from the stored calibration rather than refitted, because the
    # clock is stable: the eight archived fits scatter by 4 ppm, but that
    # scatter tracks line strength and not time, and the constrained ones agree
    # to 0.27 ppm over 18 hours. Only a fit that had a strong enough line to
    # hold the shift is used - see trustworthy_velocity_shift - since a shift
    # fitted against a weak line is confidently wrong by several km/s, which is
    # worse than leaving it out.
    clock_shift = rf_calibration.trustworthy_velocity_shift(cal) if cal_ok else None

    fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
    secax = None
    if mode == "drift":
        _plot_drift(ax, spectra, stamps, transit_minutes)
    else:
        secax = _plot_spectrum(ax, freq_hz, spectra, lsr=lsr,
                               clock_shift=clock_shift)
        if cal_ok:
            ax.set_ylabel("Antenna temperature (K)")
    # The spectrum's velocity axis lives along the top of the frame, and
    # tight_layout does not count it when placing the title, so the two land on
    # top of each other. Reserve the room explicitly.
    ax.set_title(f"{title}\n{subtitle}", fontsize=10,
                 pad=38 if mode != "drift" else 10)
    _style_dark(fig)
    if secax is not None:
        # A secondary axis is a child axes, and the blanket styling above gives
        # it a background and a grid of its own - an opaque panel and a second
        # set of vertical lines at the velocity ticks, over the ones already
        # drawn at the frequency ticks. It needs the colours and nothing else.
        secax.set_facecolor("none")
        secax.grid(False)
        secax.xaxis.label.set_color(_PLOT_FG)
        secax.tick_params(colors=_PLOT_FG, which="both")
        for spine in secax.spines.values():
            spine.set_color(_PLOT_GRID)
    fig.tight_layout()
    fig.savefig(output_path, facecolor=fig.get_facecolor())
    plt.close(fig)
    log.info("Observation plot written to %s (%s, %d spectra)",
             output_path, mode, n)
    return output_path


def _robust_ylim(ax, y):
    """Scale to the body of the data, and say so when something is off scale.

    The first integration after the flowgraph starts is often a settling
    transient orders of magnitude above everything else, and a single one of
    those flattens the whole curve into the axis. Narrowband RFI does the same
    to a spectrum.

    Nothing is dropped: every point is still drawn, the limits are chosen from
    the 0.5-99.5 percentile range, and the count of points outside the frame is
    written on the plot. A scale that quietly excluded them would be worse than
    the squashed plot it fixes - the reader could not tell there was anything
    to look at.
    """
    y = np.asarray(y, dtype=float)
    finite = y[np.isfinite(y)]
    if finite.size < 8:
        return
    lo, hi = np.percentile(finite, [0.5, 99.5])
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        return
    pad = 0.08 * (hi - lo)
    lo, hi = lo - pad, hi + pad
    n_out = int(np.count_nonzero((finite < lo) | (finite > hi)))
    if n_out == 0:
        return
    ax.set_ylim(lo, hi)
    ax.text(0.995, 0.02,
            f"{n_out} point{'s' if n_out != 1 else ''} off scale",
            transform=ax.transAxes, ha="right", va="bottom",
            fontsize=8, color=_MARK)


def observation_direction(header, when=None):
    """(galactic l, b) the observation was pointed at, or None.

    Every coordinate system ends up here, because the velocity frame depends on
    the direction on the sky and nothing else. Alt/az and drift pointings have
    to be converted at a particular moment - the dish is fixed and the sky is
    not - so those are resolved at `when`, which callers pass as the mid-point
    of the observation.
    """
    system = str(header.get("coord_system", "") or "").lower()
    c1 = _dms(header, "coord1")
    c2 = _dms(header, "coord2")
    if c1 is None or c2 is None:
        return None
    try:
        import astropy.units as u
        from astropy.coordinates import AltAz, EarthLocation, SkyCoord
        from astropy.time import Time
    except ImportError:
        return None

    if system == "galactic":
        return float(c1), float(c2)
    if system == "drift" and str(header.get("drift_frame", "")).lower() == "galactic":
        # A drift entry's coordinates are in whichever frame it was typed in.
        # Read as RA/Dec, last night's Cas A scan (l=111.735, b=-2.130,
        # galactic frame) would have become RA 111.7 hours.
        return float(c1), float(c2)
    if system in ("radec", "drift"):
        # coord1 is in hours for an RA, per the schedule form.
        sky = SkyCoord(ra=float(c1) * 15.0 * u.deg, dec=float(c2) * u.deg,
                       frame="icrs")
    elif system == "altaz":
        if when is None:
            return None
        site = EarthLocation(lat=SITE_LAT_DEG * u.deg, lon=SITE_LON_DEG * u.deg,
                             height=SITE_HEIGHT_M * u.m)
        sky = SkyCoord(alt=float(c1) * u.deg, az=float(c2) * u.deg,
                       frame=AltAz(obstime=Time(when), location=site)).icrs
    else:
        return None
    gal = sky.galactic
    return float(gal.l.deg), float(gal.b.deg)


def _dms(header, prefix):
    """Decimal degrees (or hours) from the deg/min/sec triple in a header."""
    try:
        deg = float(header[prefix + "_deg"])
    except (KeyError, TypeError, ValueError):
        return None
    minutes = float(header.get(prefix + "_min", 0) or 0)
    seconds = float(header.get(prefix + "_sec", 0) or 0)
    sign = -1.0 if deg < 0 else 1.0
    return sign * (abs(deg) + minutes / 60.0 + seconds / 3600.0)


def lsr_offset_km_s(header, when):
    """Velocity to SUBTRACT from the topocentric axis to put it in LSR.

    The recorded axis is the raw Doppler shift of the observed frequency: no
    barycentric term, no solar motion. H I is quoted in LSR everywhere, so
    without this a spectrum cannot be compared with published data, with the
    simulator, or with the same source observed six months later - the offset
    reaches ~30 km/s and changes with the date and the direction.

    The calculation is the one already used in the other direction, and proven
    there: rf_calibration.simulated_spectrum shifts the simulator's LSR
    spectra to topocentric before fitting the gain, and those fits agree to
    1.4% across two days and 102 degrees of longitude. frame_offset returns
    what to *add* to an LSR axis to get the observed frame, so recovering LSR
    from an observation subtracts it.

    Evaluated at the observation's own epoch, never at "now": the barycentric
    term moves 1.95 km/s in a week and 8.06 km/s in a month, which at 0.49 kHz
    channels is 19 and 78 channels respectively.

    Returns (dv_km_s, glon, glat), or None when the direction cannot be worked
    out - in which case the caller must leave the axis topocentric and say so,
    because a velocity axis silently in the wrong frame is worse than one
    honestly labelled.
    """
    if when is None:
        return None
    direction = observation_direction(header, when)
    if direction is None:
        return None
    glon, glat = direction
    try:
        import sys
        if SIMULATOR_DIR not in sys.path:
            sys.path.insert(0, SIMULATOR_DIR)
        import astro_simulator as A
        dv_m_s = A.frame_offset(glon, glat, "topo", when)
    except Exception:                                     # noqa: BLE001
        return None
    return float(dv_m_s) / 1000.0, glon, glat


def _plot_spectrum(ax, freq_hz, spectra, lsr=None, clock_shift=None):
    mean = spectra.mean(axis=0)
    freq_mhz = freq_hz / 1e6
    # Staircase, as on the calibration plots. Each point is a channel with a
    # width, and a line between channel centres draws a slope across a channel
    # that was never measured at a slope. It matters most where it is easiest to
    # miss: a two-channel interference spike drawn as a line becomes a triangle
    # spanning four channels, which reads as something resolved.
    ax.plot(freq_mhz, mean, color=_ACCENT, lw=1.0, drawstyle="steps-mid")
    _robust_ylim(ax, mean)
    ax.set_xlabel("Frequency (MHz)")
    ax.set_ylabel("Mean power (linear, arb.)")
    rest_mhz = H1_REST_FREQ_HZ / 1e6
    if freq_mhz.min() <= rest_mhz <= freq_mhz.max():
        ax.axvline(rest_mhz, color=_MARK, lw=1.0, ls="--",
                   label="H I rest frequency")
        ax.legend(fontsize=8, loc="best")

    # Velocity on the top axis, in LSR when the direction and epoch are known.
    #
    # The recorded axis is the raw Doppler shift of the observed frequency, so
    # it carries the Earth's orbit and rotation and the Sun's motion through
    # the local standard of rest - up to ~30 km/s, varying with direction and
    # date. H I is quoted in LSR everywhere, so a topocentric axis cannot be
    # compared with published data, with the simulator, or with the same source
    # six months later. The correction is subtracted here rather than written
    # into the file: the recorded observation stays raw and can be re-reduced.
    #
    # Where the direction cannot be worked out the axis stays topocentric and
    # says so. A velocity axis silently in the wrong frame is worse than one
    # honestly labelled - which is what this was until 2026-08-25.
    # Two terms, both subtracted: the frame, and the receiver's clock. The
    # clock term is what the gain fit measured as velocity_shift_km_s - the
    # measured line sitting below the model - so removing it means subtracting
    # it. Checked on the 2026-08-25 anticentre observation by line centroid,
    # which is far more precise than a peak channel: measured minus HI4PI was
    # -0.758 km/s against a stored shift of -0.787, and applying it takes the
    # centroid agreement from 0.76 km/s to 0.03.
    dv = lsr[0] if lsr else 0.0
    clock = clock_shift or 0.0

    def to_vel(f_mhz):
        topo = C_KM_S * (1.0 - np.asarray(f_mhz) * 1e6 / H1_REST_FREQ_HZ)
        return topo - dv - clock

    def to_freq(v):
        topo = np.asarray(v) + dv + clock
        return H1_REST_FREQ_HZ * (1.0 - topo / C_KM_S) / 1e6

    secax = ax.secondary_xaxis("top", functions=(to_vel, to_freq))
    if lsr:
        note = "topocentric %+.2f km/s" % -dv
        if clock_shift is not None:
            note += ", clock %+.2f km/s" % -clock_shift
        secax.set_xlabel("LSR velocity (km/s)   [%s applied, l=%.1f b=%.1f]"
                         % (note, lsr[1], lsr[2]))
    else:
        secax.set_xlabel("Topocentric velocity (km/s) - direction unknown, "
                         "no LSR correction")
    return secax


def _plot_drift(ax, spectra, stamps, transit_minutes):
    power = spectra.mean(axis=1)
    if stamps.size == spectra.shape[0] and stamps.size > 1:
        minutes = (stamps - stamps[0]) / 60.0
    else:
        minutes = np.arange(spectra.shape[0], dtype=float)
    # Likewise: each point is a record, integrated over its own few seconds,
    # not a sample of a continuous curve.
    ax.plot(minutes, power, color=_ACCENT, lw=1.2, drawstyle="steps-mid")
    _robust_ylim(ax, power)
    ax.set_xlabel("Time since start (min)")
    ax.set_ylabel("Band power (linear, arb.)")
    # Where the source was meant to cross the beam centre. A peak away from
    # this line is the pointing or the clock, not the source.
    if transit_minutes is not None and math.isfinite(transit_minutes):
        if minutes.min() <= transit_minutes <= minutes.max():
            ax.axvline(transit_minutes, color=_MARK, lw=1.0, ls="--",
                       label="expected transit")
            ax.legend(fontsize=8, loc="best")


def plot_bandpass_check(observation_path, output_path, template=None,
                        figsize=(16.0, 9.0), dpi=120):
    """Draw the measured response and what dividing by it leaves.

    The point is to be checked by eye, so both panels share an axis and the
    lower one is the answer to the only question that matters: is it flat now.
    Drawn at 1920x1080 because it is read on the observatory console and never
    on a phone.

    The masked windows are shaded rather than hidden. The H I line was excluded
    from the fit deliberately - the Lockman Hole is about 1.3 K at the line, not
    zero - and a reader has to be able to see that the template was interpolated
    across it rather than fitted through it.
    """
    if not MATPLOTLIB_AVAILABLE:
        raise RuntimeError("matplotlib is not installed, so no plot can be drawn")
    if template is None:
        template = bandpass.load_bandpass()
    if not template:
        raise RuntimeError("no bandpass template has been measured yet")

    freq_hz, spectra, _stamps, _taus, header = read_observation(observation_path)
    ok, why = bandpass.applies_to(template, header)
    if not ok:
        raise RuntimeError("this template does not apply to that observation: %s" % why)

    raw = np.nanmean(spectra, axis=0)
    model = bandpass.evaluate(template, freq_hz)
    inside = np.isfinite(model) & (model > 0)
    corrected = np.full_like(raw, np.nan)
    corrected[inside] = raw[inside] / model[inside]
    level = np.nanmedian(corrected[inside])
    if level:
        corrected = corrected / level

    lo = template["config"]["lo_hz"]
    mhz = freq_hz / 1e6
    line_mhz = H1_REST_FREQ_HZ / 1e6
    mask_mhz = template.get("line_mask_hz", 250e3) / 1e6
    dc_mhz = template.get("dc_mask_hz", 30e3) / 1e6

    fig, (ax_raw, ax_flat) = plt.subplots(
        2, 1, figsize=figsize, dpi=dpi, sharex=True,
        gridspec_kw={"height_ratios": [1.15, 1.0], "hspace": 0.08})

    ax_raw.plot(mhz, raw, color=_ACCENT, lw=0.7, label="measured, uncorrected")
    ax_raw.plot(mhz[inside], model[inside] * (level or 1.0), color=_MARK, lw=2.0,
                label="fitted response (order %d)" % template["degree"])
    ax_raw.set_ylabel("Mean power (linear, arb.)")
    ax_raw.legend(loc="lower right", fontsize=9)

    ax_flat.plot(mhz, 100 * (corrected - 1.0), color=_ACCENT, lw=0.7)
    ax_flat.axhline(0.0, color=_MARK, lw=1.2)
    # Quote the flatness over the channels the template was actually fitted to.
    # Including the LO artefact makes it 1.13% against 0.43%, which says nothing
    # about the bandpass and everything about a spike that is known, marked, and
    # interpolated away downstream.
    scored = inside & (np.abs(freq_hz - H1_REST_FREQ_HZ) > template.get("line_mask_hz", 250e3)) \
        & (np.abs(freq_hz - lo) > template.get("dc_mask_hz", 30e3))
    resid = np.nanstd(corrected[scored] - 1.0)
    for sign in (1, -1):
        ax_flat.axhline(sign * 100 * resid, color=_PLOT_GRID, lw=1.0, ls="--")
    ax_flat.set_ylabel("Corrected, deviation from flat (%)")
    ax_flat.set_xlabel("Frequency (MHz)")
    span = max(4.0 * 100 * resid, 1.5)
    ax_flat.set_ylim(-span, span)

    for ax in (ax_raw, ax_flat):
        # What was excluded from the fit, and why it is not a hole in the data.
        ax.axvspan(line_mhz - mask_mhz, line_mhz + mask_mhz,
                   color="#ffa502", alpha=0.10, lw=0)
        ax.axvspan(lo / 1e6 - dc_mhz, lo / 1e6 + dc_mhz,
                   color="#ff4757", alpha=0.14, lw=0)
        for edge in (lo - template["u_scale_hz"], lo + template["u_scale_hz"]):
            ax.axvline(edge / 1e6, color=_PLOT_GRID, lw=1.0, ls=":")
        ax.axvline(line_mhz, color=_MARK, lw=1.0, ls="--", alpha=0.7)

    ax_raw.text(0.005, 0.97,
                "shaded: H I window masked from the fit (amber) and the LO "
                "artefact (red); dotted: edges of the fitted band",
                transform=ax_raw.transAxes, fontsize=8.5, color=_PLOT_FG,
                alpha=0.75, va="top")
    ax_flat.text(0.005, 0.03,
                 "dashed: +-%.3f%% rms, over the channels the template was fitted "
                 "to \u2014 the LO artefact runs off scale and is excluded"
                 % (100 * resid),
                 transform=ax_flat.transAxes, fontsize=9, color=_PLOT_FG,
                 alpha=0.85)

    when = (template.get("created_utc") or "")[:19].replace("T", " ")
    fig.suptitle("Bandpass correction check \u2014 template of %s UTC%s, "
                 "order %d, fit residual %.3f%%\n%s at LO %.6f MHz, %.3f Msps"
                 % (when,
                    " on " + template["source_name"] if template.get("source_name") else "",
                    template["degree"], 100 * template["fit_residual_rms"],
                    os.path.basename(observation_path), lo / 1e6,
                    template["config"]["sample_rate_hz"] / 1e6),
                 color=_PLOT_FG, fontsize=12)
    _style_dark(fig)
    fig.subplots_adjust(top=0.90, left=0.06, right=0.985, bottom=0.075)
    fig.savefig(output_path, facecolor=fig.get_facecolor())
    plt.close(fig)
    return output_path


def plot_gain_check(calibration, output_path, figsize=(16.0, 9.0), dpi=120):
    """The measured sky in kelvin, over the model it was calibrated against.

    Drawn from the same reduction the fit used - rf_calibration.reduce_for_fit -
    rather than a second one built to look similar. A picture produced by a
    slightly different pipeline is worse than no picture, because disagreement
    then means nothing and agreement looks like corroboration.

    Three views, because they fail differently. The spectrum says whether the
    line profile is right; the scatter against the model says whether the
    relation is linear, which is the assumption the whole calibration rests on;
    and the residual says where it is wrong, which a correlation coefficient
    cannot.
    """
    if not MATPLOTLIB_AVAILABLE:
        raise RuntimeError("matplotlib is not installed, so no plot can be drawn")
    import rf_calibration

    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(here, "data", calibration.get("source_file", ""))
    if calibration.get("source_file") and not os.path.exists(src):
        # Recordings live under data/observations; calibration runs in data.
        src = os.path.join(here, "data", "observations", calibration["source_file"])
    if not calibration.get("source_file") or not os.path.exists(src):
        raise RuntimeError("the observation this calibration came from is no "
                           "longer in the data folder")

    stored = calibration.get("reduction_version")
    if stored != rf_calibration.REDUCTION_VERSION:
        raise RuntimeError(
            "this calibration was fitted by an older reduction (version %s, now "
            "%d), so drawing it against the current one would show a difference "
            "that is the code changing rather than the telescope. Run the gain "
            "calibration again."
            % (stored, rf_calibration.REDUCTION_VERSION))

    red = rf_calibration.reduce_for_fit(src, calibration["glon"],
                                        calibration["glat"])
    use = red["usable"]
    freq = red["sim_freq_hz"][use]
    counts = red["binned_counts"][use]
    # Draw the model the fit actually used, shifted by the frequency-scale error
    # it fitted. Showing the unshifted model beside a spectrum fitted to the
    # shifted one would put a visible offset on the plot that is not in the fit.
    shift = calibration.get("velocity_shift_km_s") or 0.0
    if shift:
        scaled = red["sim_freq_hz"] * (1.0 - shift / (C_KM_S))
        model_k = np.interp(freq, scaled, red["sim_ta_k"])
    else:
        model_k = red["sim_ta_k"][use]

    gain = calibration["gain_counts_per_k"]
    t_sys = calibration["t_sys_k"]
    measured_k = counts / gain - t_sys
    resid_k = measured_k - model_k

    fig = plt.figure(figsize=figsize, dpi=dpi)
    gs = fig.add_gridspec(2, 2, height_ratios=[1.5, 1.0],
                          hspace=0.26, wspace=0.16)
    ax_spec = fig.add_subplot(gs[0, :])
    ax_scat = fig.add_subplot(gs[1, 0])
    ax_res = fig.add_subplot(gs[1, 1])

    # What the radiometer equation says this observation's noise should be.
    # sigma = (T_sys + T_A) / sqrt(n_pol * dnu * tau), one polarisation, on the
    # binned channels the fit actually used - binning down from 0.49 kHz to the
    # model's 6.1 kHz averages the noise down with it, so the fine-channel figure
    # would be four times too big. The signal term matters: a channel on the line
    # is hotter and therefore noisier, which is why this is a curve and not a
    # number.
    dnu = float(np.abs(np.median(np.diff(freq))))
    tau = float(red.get("tau_total_s") or 0.0)
    sigma_k = ((t_sys + np.maximum(model_k, 0.0)) / np.sqrt(dnu * tau)
               if dnu > 0 and tau > 0 else None)

    mhz = freq / 1e6
    # Staircase, not point-to-point: these are binned channels, each with a
    # width, and a line joining bin centres draws a slope across a channel that
    # was never measured at a slope.
    step = dict(drawstyle="steps-mid")
    ax_spec.plot(mhz, measured_k, color=_ACCENT, lw=1.1,
                 label="measured, calibrated to kelvin", **step)
    ax_spec.plot(mhz, model_k, color=_MARK, lw=1.8, alpha=0.9,
                 label="HI4PI through this beam (simulated)", **step)
    if sigma_k is not None:
        ax_spec.fill_between(mhz, model_k - sigma_k, model_k + sigma_k,
                             color=_MARK, alpha=0.20, lw=0, step="mid",
                             label="expected noise, $\\pm1\\sigma$")
    ax_spec.axvline(H1_REST_FREQ_HZ / 1e6, color=_PLOT_GRID, lw=1.0, ls="--")
    ax_spec.set_ylabel("Antenna temperature (K)")
    ax_spec.set_xlabel("Frequency (MHz)")
    ax_spec.legend(loc="upper right", fontsize=10)

    lim = max(np.nanmax(model_k), np.nanmax(measured_k))
    lo = min(np.nanmin(model_k), np.nanmin(measured_k), 0.0)
    ax_scat.plot([lo, lim], [lo, lim], color=_MARK, lw=1.5,
                 label="1:1 (perfect calibration)")
    ax_scat.scatter(model_k, measured_k, s=6, color=_ACCENT, alpha=0.55, lw=0)
    if sigma_k is not None:
        # A band of the width the thermal noise alone would give, so points
        # falling outside it are visibly not just noise.
        sig = float(np.nanmedian(sigma_k))
        ax_scat.fill_between([lo, lim], [lo - sig, lim - sig],
                             [lo + sig, lim + sig], color=_MARK, alpha=0.18,
                             lw=0, label="expected noise, $\\pm$%.2f K" % sig)
    ax_scat.set_xlabel("Model antenna temperature (K)")
    ax_scat.set_ylabel("Measured (K)")
    ax_scat.legend(loc="upper left", fontsize=9)

    # The residual against a drawn realisation of the noise it should have. If
    # the two look alike, the fit is noise-limited and there is nothing further
    # to find; if the real one is visibly rougher or more structured, the excess
    # is systematic and worth chasing. That comparison is far easier to make by
    # eye than from two numbers, which is the point of drawing it.
    line_free = np.abs(freq - H1_REST_FREQ_HZ) > 400e3
    rms = float(np.nanstd(resid_k[line_free])) if line_free.any() \
        else float(np.nanstd(resid_k))
    if sigma_k is not None:
        drawn = np.random.default_rng(20260824).standard_normal(sigma_k.size) * sigma_k
        ax_res.plot(mhz, drawn, color=_PLOT_FG, lw=0.8, alpha=0.55,
                    label="simulated noise at the expected level", **step)
    ax_res.plot(mhz, resid_k, color=_ACCENT, lw=0.9, label="measured - model",
                **step)
    ax_res.axhline(0.0, color=_MARK, lw=1.2)
    for sign in (1, -1):
        ax_res.axhline(sign * rms, color=_PLOT_GRID, lw=1.0, ls="--")
    ax_res.set_xlabel("Frequency (MHz)")
    ax_res.set_ylabel("Measured - model (K)")
    ax_res.legend(loc="upper right", fontsize=8)
    if sigma_k is not None:
        expected = float(np.nanmedian(sigma_k))
        ax_res.text(0.02, 0.05,
                    "measured %.2f K rms off the line, expected %.2f K "
                    "(%.1f\u00d7)" % (rms, expected,
                                      rms / expected if expected else float("nan")),
                    transform=ax_res.transAxes, fontsize=9, color=_PLOT_FG,
                    alpha=0.9)
    else:
        ax_res.text(0.02, 0.05, "rms %.2f K" % rms, transform=ax_res.transAxes,
                    fontsize=10, color=_PLOT_FG, alpha=0.85)

    # Say plainly when the fit is not to be trusted. A calibration that sat on
    # its floor, or one with no lever arm, must not look like a measurement
    # merely because a plot was drawn of it.
    flags = []
    if calibration.get("t_sys_bound_active"):
        flags.append("T_sys pinned at its %.0f K floor - fitted against the "
                     "bound, not measured" % calibration.get("min_t_sys_k", 50))
    if calibration.get("t_sys_level"):
        flags.append("T_sys is %s (%.0f K)" % (calibration["t_sys_level"],
                                               calibration.get("t_sys_k", 0)))
    if (calibration.get("correlation") or 0) < 0.8:
        flags.append("weak correlation (r=%.2f): little lever arm, so T_sys is "
                     "poorly determined" % (calibration.get("correlation") or 0))
    if flags:
        ax_spec.text(0.01, 0.97, "\n".join("! " + f for f in flags),
                     transform=ax_spec.transAxes, fontsize=10, color="#ff6b6b",
                     va="top")

    when = (calibration.get("observed_utc") or "")[:19].replace("T", " ")
    fig.suptitle("Gain calibration \u2014 l=%.0f b=%+.0f, %s UTC\n"
                 "T_sys %.1f K, gain %.4g counts/K, r=%.3f, residual %.2f K%s"
                 % (calibration["glon"], calibration["glat"], when,
                    t_sys, gain, calibration.get("correlation") or float("nan"),
                    calibration.get("residual_rms_k") or rms,
                    ("  |  clock %+.2f ppm (%+.2f km/s)"
                     % (calibration["implied_ppm"], shift))
                    if calibration.get("implied_ppm") else ""),
                 color=_PLOT_FG, fontsize=12)
    _style_dark(fig)
    fig.subplots_adjust(top=0.89, left=0.055, right=0.985, bottom=0.075)
    fig.savefig(output_path, facecolor=fig.get_facecolor())
    plt.close(fig)
    return output_path


def plot_drift_fit(fit, output_path, figsize=(16.0, 9.0), dpi=120):
    """The recorded drift scan over the simulator's prediction, after the fit.

    Two panels: the measured band power in kelvin (counts through the fitted
    gain, T_sys subtracted) as a staircase - each record a level held over its
    integration, as every plot here draws a measurement - with the predicted
    curve through it; and the residual beneath, which is where the fit is
    wrong in a way a correlation coefficient cannot show. Both against UTC.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from datetime import datetime, timezone

    t = [datetime.fromtimestamp(s, tz=timezone.utc) for s in fit["stamps"]]
    meas = np.asarray(fit["measured_k"], float)
    model = np.asarray(fit["model_k"], float)
    fig, (ax, axr) = plt.subplots(2, 1, figsize=figsize, dpi=dpi, sharex=True,
                                  gridspec_kw={"height_ratios": [3, 1]})
    ax.step(t, meas, where="mid", color="#ffa502", lw=1.6, label="recorded (fitted gain, T_sys subtracted)")
    ax.plot(t, model, color="#00d4ff", lw=2.0, label="simulator: predicted drift curve")
    ax.set_ylabel("band-mean antenna temperature (K)")
    tr = fit.get("track", {})
    ax.set_title(
        "%s  -  total-power fit: gain %.3g counts/K, T_sys %.0f K, correlation %.3f, "
        "residual %.2f K (%d records)\nparked at alt %.1f az %.1f;  track l %.1f -> %.1f, b %.1f -> %.1f"
        % (fit.get("source_file", ""), fit["gain_counts_per_k"], fit["t_sys_k"],
           fit["correlation"], fit["residual_rms_k"], fit["records_used"],
           fit["pointing"]["alt_deg"], fit["pointing"]["az_deg"],
           tr.get("glon", [0])[0], tr.get("glon", [0])[-1],
           tr.get("glat", [0])[0], tr.get("glat", [0])[-1]))
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
    axr.step(t, meas - model, where="mid", color="#c8c8d8", lw=1.2)
    axr.axhline(0, color="#555", lw=0.8)
    axr.set_ylabel("residual (K)")
    axr.set_xlabel("UTC")
    axr.grid(alpha=0.3)
    axr.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    fig.tight_layout()
    _style_dark(fig)
    fig.savefig(output_path, facecolor=fig.get_facecolor())
    plt.close(fig)
    return output_path
