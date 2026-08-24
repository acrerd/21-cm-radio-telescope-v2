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
            in Doppler velocity. That axis is *topocentric* - no LSR or
            barycentric correction is applied here - and is labelled as such,
            because a velocity axis silently in the wrong frame is worse than
            no velocity axis at all.

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
                     transit_minutes=None):
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

    fig, ax = plt.subplots(figsize=(9, 4.5))
    secax = None
    if mode == "drift":
        _plot_drift(ax, spectra, stamps, transit_minutes)
    else:
        secax = _plot_spectrum(ax, freq_hz, spectra)
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
    fig.savefig(output_path, dpi=110, facecolor=fig.get_facecolor())
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


def _plot_spectrum(ax, freq_hz, spectra):
    mean = spectra.mean(axis=0)
    freq_mhz = freq_hz / 1e6
    ax.plot(freq_mhz, mean, color=_ACCENT, lw=1.0)
    _robust_ylim(ax, mean)
    ax.set_xlabel("Frequency (MHz)")
    ax.set_ylabel("Mean power (linear, arb.)")
    rest_mhz = H1_REST_FREQ_HZ / 1e6
    if freq_mhz.min() <= rest_mhz <= freq_mhz.max():
        ax.axvline(rest_mhz, color=_MARK, lw=1.0, ls="--",
                   label="H I rest frequency")
        ax.legend(fontsize=8, loc="best")

    # Velocity on the top axis. Topocentric: this is the raw Doppler shift of
    # the observed frequency, with no LSR or barycentric term, so it is offset
    # from the LSR velocity an H I spectrum is normally quoted in by up to
    # ~30 km/s. Labelled, not silently corrected.
    def to_vel(f_mhz):
        return C_KM_S * (1.0 - np.asarray(f_mhz) * 1e6 / H1_REST_FREQ_HZ)

    def to_freq(v):
        return H1_REST_FREQ_HZ * (1.0 - np.asarray(v) / C_KM_S) / 1e6

    secax = ax.secondary_xaxis("top", functions=(to_vel, to_freq))
    secax.set_xlabel("Topocentric velocity (km/s) - no LSR correction")
    return secax


def _plot_drift(ax, spectra, stamps, transit_minutes):
    power = spectra.mean(axis=1)
    if stamps.size == spectra.shape[0] and stamps.size > 1:
        minutes = (stamps - stamps[0]) / 60.0
    else:
        minutes = np.arange(spectra.shape[0], dtype=float)
    ax.plot(minutes, power, color=_ACCENT, lw=1.2)
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
