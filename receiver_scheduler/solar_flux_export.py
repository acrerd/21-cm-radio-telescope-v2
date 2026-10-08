#!/usr/bin/env python3
"""A day's solar flux at 1417 MHz, calibrated and with the tracking scallop
taken out, written as FITS and as RSTN3 ASCII.

    python solar_flux_export.py 2026-10-07
    python solar_flux_export.py --recording data/observations/20261007_111534_track.h5
    python solar_flux_export.py 2026-10-07 --out /some/dir

The scheduler runs the second form whenever a Sun track ends
(`update_solar_flux_export`), so the folder is always up to date. Everything
goes under `data/solar_flux/`:

    README.md                          what the products are, copied from solar_flux_examples/
    fits/acre_srt_1417MHz_YYYYMMDD.fits
    rstn/DDMONYY.ACR                   RSTN3 archive format
    examples/read_solar_flux.py        readers, copied from solar_flux_examples/
    examples/read_solar_flux.pro
    examples/read_solar_flux_example.ipynb   a worked example using the Python reader

**FITS** is the full record: the FITS 4.0 time conventions (Rots et al. 2015:
TIMESYS, MJDREF, TIMEUNIT, TREFPOS, DATE-BEG/DATE-END, a TIME column) and the
SolarSoft observation keywords (DATE-OBS, TELESCOP, INSTRUME, OBSRVTRY,
OBSGEO-X/Y/Z). Its FLUX extension has one row per record: TIME (s from 00:00
UTC of the day, the record's mid-point), TIMEDEL, FLUX (SFU above the
atmosphere), ERROR (the run's measured record-to-record scatter), TANT
(antenna temperature as measured), SCALLOP (the gain divided out of the
source term), TRANSMIS (atmospheric transmission divided out), SUN_ALT,
SUN_AZ, QUALITY and RUN (row of the RUNS extension, which holds each
recording's calibration and scallop solution).

**RSTN3** is the format the USAF/NOAA network archives its 1-second data in,
for putting this beside Learmonth or San Vito with tools written for theirs:
one 66-character record plus CRLF per line, `OBSCYYYYMMDDHHmmSS` and eight
6-character fields at 245, 410, 610, 1415, 2695, 4995, 8800 and 15400 MHz,
integer SFU, blank where there is no useful data. Only the 1415 field is
filled. It costs a lot: whole SFU against a 0.12 SFU scatter, no errors or
flags, and the column says 1415 MHz where the band is centred at 1417.26.
One line per record at the UT second of its mid-point, which is one line every
3 s rather than every second; a flagged record's line is written blank. The
observatory code `ACRE` is ours, not one RSTN assigned.

QUALITY bits: 1 = within `SETTLE_S` of the receiver starting (every fresh
start reads 0.3-1.7% high and settles over 2-5 min, occasionally 20); 2 = the
beam off the Sun (below half the run's median - a slew, or the stow at the
end of a run); 4 = samples dropped by the radio in that record. Flagged rows
are kept in the FITS: a reader decides. Bursts are never flagged.

**The reduction** is the one the Observe tab's solar plot draws
(`observation_plot.plot_observation`, mode "solar"), record by record rather
than binned: the continuum product over the continuum band (no H I, no LO
spur), the bandpass template and gain in force, T_sys subtracted, the scallop
removed from the source term (`scallop.correct`: fitted to the run or carried
from the last quiet one, whichever leaves less modulation), the antenna
theorem on the measured beam, and the zenith opacity for the Sun's elevation.
The gain and the beam are each used only on recordings made with the same
feed (`INSTRUMENT_CHANGES_UTC`). A run the bandpass or a gain does not apply
to is left out and said so, not exported in counts. The export never replaces the carried scallop parameters.

ERROR is statistical only: it is measured from second differences of the
run's own corrected series, so it holds the radiometer noise and the
receiver's common-mode gain wobble but no systematic. The flux scale carries
those - T_sys and gain drift ~0.3%/h through an evening and are not tracked
within a run, the effective area is the main lobe's (an upper bound on the
true aperture), and the RSTN stations themselves disagree by 10-20 SFU a day.
"""
import argparse
import glob
import math
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import bandpass                     # noqa: E402
import drift_fit                    # noqa: E402
import observation_plot             # noqa: E402
import rf_calibration               # noqa: E402
import scallop                      # noqa: E402
import observatory                  # noqa: E402,F401  (puts astro_simulator/ on the path)
import instrument                   # noqa: E402  (on the path once observatory is imported)

OBS_DIR = os.path.join(_SCRIPT_DIR, "data", "observations")
OUT_DIR = os.path.join(_SCRIPT_DIR, "data", "solar_flux")
# The README and the readers live in git here and are copied into OUT_DIR.
EXAMPLES_DIR = os.path.join(_SCRIPT_DIR, "solar_flux_examples")
EXAMPLE_FILES = {"README.md": "README.md",
                 "read_solar_flux.py": os.path.join("examples", "read_solar_flux.py"),
                 "read_solar_flux.pro": os.path.join("examples", "read_solar_flux.pro"),
                 "read_solar_flux_example.ipynb": os.path.join("examples", "read_solar_flux_example.ipynb")}

SETTLE_S = 300.0
OFF_SOURCE_FRACTION = 0.5

Q_SETTLE = 1
Q_OFF_SOURCE = 2
Q_OVERFLOW = 4

# FITS has no solar flux unit; this is its spelling of one.
SFU = "10**-22 W m-2 Hz-1"

# The instrument changes a calibration cannot cross. A gain or a beam measured
# on one side of one belongs to a different telescope from a recording on the
# other, and rf_calibration.calibration_for matches only the tuning: on its own
# it applied the new feed's gain (T_sys 191 K) to old-feed Sun tracks taken at
# the same 20 dB, and 2026-09-17..21 read 43 SFU against RSTN's 63-75.
# The feed was swapped between the Sun track that stowed at 09:32 UTC on
# 2026-09-22 and the homing at 11:43.
INSTRUMENT_CHANGES_UTC = (("2026-09-22T10:00:00+00:00", "feed replaced"),)
# Each epoch's main-lobe solid angle, by beam_scan's method (sq deg), where the
# beam in force (instrument.measured_beam) is not that epoch's. The old feed's
# is its 2026-09-15 20 dB Sun drift re-reduced that way (docs/CALIBRATION.md).
EPOCH_BEAM_SQ_DEG = {0: 21.6}


def _epoch_seconds(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def instrument_epoch(epoch_s):
    """0 before the first change in INSTRUMENT_CHANGES_UTC, 1 after it, ..."""
    return sum(1 for when, _ in INSTRUMENT_CHANGES_UTC if epoch_s >= _epoch_seconds(when))


def calibration_for(header, start_s):
    """(calibration, source) - the gain for a recording starting at `start_s`:
    the one in force if it fits the tuning and was measured on the same
    instrument, else the one the file carries on the same terms, else
    (None, reason)."""
    epoch = instrument_epoch(start_s)
    why = []
    for cal, source in ((rf_calibration.load_calibration(), "current"),
                        (rf_calibration.embedded_calibration(header), "recorded with the observation")):
        if not cal:
            continue
        ok, reason = rf_calibration.calibration_applies_to(cal, header)
        measured = _epoch_seconds(cal.get("observed_utc") or "")
        if ok and measured is not None and instrument_epoch(measured) == epoch:
            return cal, source
        why.append("%s: %s" % (source, reason or "measured on a different instrument (%s)"
                               % ", ".join(n for _, n in INSTRUMENT_CHANGES_UTC)))
    return None, "; ".join(why) or "no gain calibration has been made"


def beam_for(start_s):
    """(main-lobe solid angle in sq deg, effective area m2, where from) for a
    recording starting at `start_s`."""
    epoch = instrument_epoch(start_s)
    beam = instrument.measured_beam() or {}
    measured = _epoch_seconds(beam.get("measured_utc") or "")
    if beam.get("solid_angle_sq_deg") and measured is not None and instrument_epoch(measured) == epoch:
        sq_deg, source = float(beam["solid_angle_sq_deg"]), "measured %s" % beam["measured_utc"][:10]
    elif epoch in EPOCH_BEAM_SQ_DEG:
        sq_deg, source = EPOCH_BEAM_SQ_DEG[epoch], "this instrument's, from the archive"
    elif beam.get("solid_angle_sq_deg"):
        sq_deg, source = float(beam["solid_angle_sq_deg"]), "the beam in force (another instrument's)"
    else:
        return None, instrument.effective_area_m2(), "no beam measured: the reference value"
    lam = instrument.C_M_S / instrument.H1_REST_FREQ_HZ
    return sq_deg, lam ** 2 / (sq_deg * (math.pi / 180.0) ** 2), source


def to_sfu(t_a_k, a_eff_m2):
    """Solar flux units from antenna temperature: S = 2kT/A_e, one polarisation."""
    return np.asarray(t_a_k, float) * 2.0 * instrument.BOLTZMANN / (a_eff_m2 * 1e-22)


# RSTN3: observatory code, then the eight frequencies in field order.
RSTN_CODE = "ACRE"
RSTN_EXT = "ACR"
RSTN_FREQS_MHZ = (245, 410, 610, 1415, 2695, 4995, 8800, 15400)
RSTN_FIELD = 6
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def is_solar_track(header):
    """The rule the scheduler draws a solar plot by (`live_plot_kind`)."""
    return (str(header.get("coord_system", "")) == "object"
            and str(header.get("object_name", "")).strip().lower() == "sun"
            and str(header.get("observation_mode", "")) == "track")


def candidate_files(day, obs_dir=OBS_DIR):
    """Tracked recordings that may hold records on `day` (a date, UTC).

    Filenames carry local time, so the day either side is looked at too and
    the records themselves decide.
    """
    names = set()
    for d in (day - timedelta(days=1), day, day + timedelta(days=1)):
        names.update(glob.glob(os.path.join(obs_dir, d.strftime("%Y%m%d") + "_*_track.h5")))
    return sorted(names)


def in_observations(path, obs_dir=None):
    """Only the observatory's own recordings are exported - never a test's
    temporary file."""
    return os.path.dirname(os.path.realpath(path)) == os.path.realpath(obs_dir or OBS_DIR)


def days_of_recording(path):
    """The UTC days a recording's records fall on."""
    with observation_plot.open_readonly(path) as hf:
        stamps = np.asarray(hf["timestamps"][:], float)
    stamps = stamps[np.isfinite(stamps)]
    if not stamps.size:
        return []
    first = datetime.fromtimestamp(float(stamps.min()), tz=timezone.utc).date()
    last = datetime.fromtimestamp(float(stamps.max()), tz=timezone.utc).date()
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _per_record(path, name, n_total, keep):
    """A per-record dataset aligned with what read_observation returned."""
    with observation_plot.open_readonly(path) as hf:
        if name not in hf:
            return None
        v = np.asarray(hf[name][:n_total])
    return v[keep] if keep is not None else v


def _robust_sigma(x):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if x.size < 5:
        return float("nan")
    return float(1.4826 * np.median(np.abs(x - np.median(x))))


def reduce_run(path):
    """((run, rows), "") for one recording, or (None, reason)."""
    # The header alone says whether it is a solar track; a tracked H I field
    # is not worth reading the spectra of to find out.
    with observation_plot.open_readonly(path) as hf:
        if not is_solar_track(dict(hf.attrs)):
            return None, "not a solar track"
    freq, spectra, stamps, taus, header = observation_plot.read_observation(
        path, product="wide", keep_pilot=True)
    if not is_solar_track(header):
        return None, "not a solar track"
    n_rec = spectra.shape[0]

    # Records read_observation dropped (pilot bursts) are dropped here too, so
    # the per-record datasets stay aligned.
    with observation_plot.open_readonly(path) as hf:
        n_total = int(hf["timestamps"].shape[0])
        burst = np.asarray(hf["pilot_burst"][:]) if "pilot_burst" in hf else None
    keep = None
    if burst is not None and (burst != 0).any():
        n = min(len(burst), n_total)
        keep = np.asarray(burst[:n]) == 0
        n_total = n
    overflows = _per_record(path, "overflows", n_total, keep)

    # The solar plot's chain, in the same order.
    _, _, cont = drift_fit._band_window(header, freq)
    spectra, bp_note = bandpass.apply_bandpass(
        freq, spectra, header,
        product=("wide" if header.get("product_used") == "wide" else "h1"))
    if bp_note.startswith("not bandpass corrected"):
        return None, bp_note
    spectra = np.where(cont[None, :], spectra, np.nan)
    spectra, _, _ = observation_plot.patch_dc_artefact(freq, spectra, header)
    cal, cal_source = calibration_for(header, float(stamps[0]))
    if cal is None:
        return None, "no gain calibration applies: %s" % cal_source
    spectra = spectra / cal["gain_counts_per_k"] - cal["t_sys_k"]
    t_a = np.nanmean(spectra, axis=1)

    # Per record, on the source term alone, before anything is binned.
    corrected, report = scallop.correct(t_a, stamps, header, source_file=os.path.basename(path),
                                        update_reference=False)
    with np.errstate(divide="ignore", invalid="ignore"):
        scal = np.where(corrected != 0, t_a / corrected, 1.0)
    if not report.get("ok"):
        scal = np.ones(n_rec)

    alt, az = scallop.target_altaz(header, stamps, scallop.observer(header))
    trans = np.array([rf_calibration.atmospheric_transmission(a) for a in alt], float)
    trans = np.where(np.isfinite(trans) & (trans > 0.5), trans, np.nan)
    beam_sq_deg, a_eff, beam_source = beam_for(float(stamps[0]))
    flux = to_sfu(corrected, a_eff) / trans

    quality = np.zeros(n_rec, dtype=np.int16)
    quality[stamps - stamps[0] < SETTLE_S] |= Q_SETTLE
    # Against the run's median, not a running one: at the end of a short run
    # the stow fills most of a running window and drags its median down with it.
    quality[~(corrected > OFF_SOURCE_FRACTION * np.nanmedian(corrected))] |= Q_OFF_SOURCE
    if overflows is not None and overflows.size == n_rec:
        quality[overflows > 0] |= Q_OVERFLOW

    # Second differences of the clean records: white noise of sigma s gives
    # second differences of sigma s*sqrt(6), and any smooth trend drops out.
    clean = quality == 0
    sigma = _robust_sigma(np.diff(flux[clean], n=2)) / math.sqrt(6.0)

    ok = bool(report.get("ok"))
    k_beam = report.get("k_beam") or 1.0
    mod = report.get("modulation_pct") or {}
    run = {
        "file": os.path.basename(path),
        "obs_name": str(header.get("obs_name", "")),
        "sun_monitor": int(header.get("sun_monitor", 0) or 0),
        "gain": float(cal["gain_counts_per_k"]),
        "t_sys": float(cal["t_sys_k"]),
        "cal_utc": str(cal.get("observed_utc") or ""),
        "cal_source": cal_source,
        "bandpass": bp_note,
        "scallop": report.get("chosen", "none") if report.get("applies") else "n/a",
        "scallop_why": str(report.get("why", "")),
        "scallop_caption": scallop.plot_caption(report),
        "k_alt": float((report.get("k_alt") or 0.0) / k_beam) if ok else 0.0,
        "k_az": float((report.get("k_az") or 0.0) / k_beam) if ok else 0.0,
        "phase_alt": float(report.get("phase_alt_deg", 0.0)) if ok else 0.0,
        "phase_az": float(report.get("phase_az_deg", 0.0)) if ok else 0.0,
        "mod_before": float(mod.get("none", float("nan"))),
        "mod_after": float(mod.get(report.get("chosen"), float("nan"))),
        "sigma": sigma,
        "beam_sq_deg": float(beam_sq_deg or float("nan")),
        "a_eff": float(a_eff),
        "beam_source": beam_source,
        "terms_source": str(report.get("terms_source", "")),
        "header": header,
    }
    rows = {
        "stamps": stamps, "taus": taus if taus.size == n_rec else np.full(n_rec, np.nan),
        "flux": flux, "t_a": corrected, "scallop": scal, "trans": trans,
        "alt": alt, "az": az, "quality": quality,
    }
    return (run, rows), ""


def collect_day(day, runs):
    """(columns, runs on the day, epoch of 00:00 UTC), or None if nothing falls on it.

    The columns are every reduced record whose mid-point is on `day`, in time
    order; each run is clipped to the day and counted from 0 in RUN.
    """
    t0 = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    names = ("time", "timedel", "flux", "error", "t_a", "scallop",
             "trans", "alt", "az", "quality", "run")
    cols = {k: [] for k in names}
    kept = []
    for run, rows in runs:
        inside = (rows["stamps"] >= t0) & (rows["stamps"] < t0 + 86400.0)
        if not inside.any():
            continue
        run = dict(run)
        clean = inside & (rows["quality"] == 0)
        run["n_rows"] = int(inside.sum())
        run["first"] = float(rows["stamps"][inside][0])
        run["last"] = float(rows["stamps"][inside][-1])
        run["mean_flux"] = float(np.nanmean(rows["flux"][clean])) if clean.any() else float("nan")
        cols["time"].append(rows["stamps"][inside] - t0)
        cols["error"].append(np.full(inside.sum(), run["sigma"]))
        cols["run"].append(np.full(inside.sum(), len(kept), dtype=np.int16))
        for k, src in (("timedel", "taus"), ("flux", "flux"), ("t_a", "t_a"),
                       ("scallop", "scallop"), ("trans", "trans"), ("alt", "alt"),
                       ("az", "az"), ("quality", "quality")):
            cols[k].append(rows[src][inside])
        kept.append(run)
    if not kept:
        return None
    c = {k: np.concatenate(v) for k, v in cols.items()}
    order = np.argsort(c["time"], kind="stable")
    return {k: v[order] for k, v in c.items()}, kept, t0


def _obsgeo(header):
    from astropy.coordinates import EarthLocation
    import astropy.units as u
    loc = EarthLocation.from_geodetic(float(header["site_lon_deg"]) * u.deg,
                                      float(header["site_lat_deg"]) * u.deg,
                                      float(header.get("site_height_m", 0.0)) * u.m)
    return [float(v.to_value(u.m)) for v in (loc.x, loc.y, loc.z)]


def _reference(day):
    """RSTN 1415 MHz for the day from the scheduler's cached history, no network."""
    try:
        import solar_reference
        hist = solar_reference.history(max_age_s=1e12)
        return solar_reference.values_for(day.isoformat(), hist)
    except Exception:                                    # noqa: BLE001
        return {}


# The TTYPEn comments: what each column is, for a reader without this script.
DESCRIPTIONS = {
    "TIME": "record mid-point, s after MJDREF",
    "TIMEDEL": "record integration",
    "FLUX": "solar flux above the atmosphere, SFU",
    "ERROR": "run's record-to-record scatter, SFU",
    "TANT": "antenna temperature as measured",
    "SCALLOP": "tracking-scallop gain divided out",
    "TRANSMIS": "atmospheric transmission divided out",
    "SUN_ALT": "true Sun elevation, no refraction",
    "SUN_AZ": "true Sun azimuth, N through E",
    "QUALITY": "1 settling, 2 off the Sun, 4 dropped samples",
    "RUN": "row of the RUNS table, from 0",
    "FILE": "recording",
    "SUN_MON": "1 = Sun monitor run, 0 = booked",
    "FIRST": "first row's TIME",
    "LAST": "last row's TIME",
    "MEANFLUX": "mean FLUX of unflagged rows, SFU",
    "SIGMA": "record-to-record scatter, SFU",
    "GAIN": "gain in force",
    "TSYS": "system temperature in force",
    "CAL_UTC": "when the gain was measured",
    "CAL_SRC": "where the gain came from",
    "BEAM_SA": "main-lobe solid angle used",
    "AEFF": "effective area used, lambda^2/Omega",
    "BEAM_SRC": "where the beam came from",
    "SCALSOL": "scallop: fitted, carried or none",
    "SCAL_KAL": "alt amplitude / beam curvature",
    "SCAL_KAZ": "az amplitude / beam curvature",
    "SCAL_PAL": "alt phase offset",
    "SCAL_PAZ": "az phase offset",
    "MOD_PRE": "pulse-phase modulation before, %",
    "MOD_POST": "pulse-phase modulation after, %",
    "TERMS": "pointing terms the scallop used",
    "SCALNOTE": "scallop summary",
}


def _describe(hdu):
    for i in range(1, hdu.header["TFIELDS"] + 1):
        key = "TTYPE%d" % i
        text = DESCRIPTIONS.get(hdu.header[key])
        if text:
            hdu.header.comments[key] = text


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def write_fits(day, c, kept_runs, t0, out_path):
    from astropy.io import fits
    from astropy.time import Time

    h = kept_runs[0]["header"]
    first, last = c["time"][0] + t0, c["time"][-1] + t0
    band = [float(x) for x in h["continuum_band_hz"]]
    obsgeo = _obsgeo(h)

    prim = fits.PrimaryHDU()
    ph = prim.header
    ph["DATE"] = (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "file written (UTC)")
    ph["ORIGIN"] = "Acre Road Observatory, University of Glasgow"
    ph["OBSRVTRY"] = "Acre Road Observatory"
    ph["TELESCOP"] = "Acre Road SRT"
    ph["INSTRUME"] = "SAWbird+ H1 / Ettus B200"
    ph["OBJECT"] = "Sun"
    ph["OBS_TYPE"] = ("solar flux", "tracked total power, continuum band")
    ph["DATE-OBS"] = (_iso(first), "first record mid-point (UTC)")
    ph["DATE-BEG"] = (_iso(first - c["timedel"][0] / 2.0), "start of first record (UTC)")
    ph["DATE-END"] = (_iso(last + c["timedel"][-1] / 2.0), "end of last record (UTC)")
    ph["OBSGEO-X"] = (obsgeo[0], "[m] ITRS geocentric")
    ph["OBSGEO-Y"] = (obsgeo[1], "[m] ITRS geocentric")
    ph["OBSGEO-Z"] = (obsgeo[2], "[m] ITRS geocentric")
    ph["OBS_LAT"] = (float(h["site_lat_deg"]), "[deg] geodetic latitude")
    ph["OBS_LON"] = (float(h["site_lon_deg"]), "[deg] east longitude")
    ph["OBS_ALT"] = (float(h.get("site_height_m", 0.0)), "[m] height")
    ph["FREQ"] = (0.5 * (band[0] + band[1]), "[Hz] centre of the continuum band")
    ph["BANDWID"] = (band[1] - band[0], "[Hz] continuum band, H I and LO spur excluded")
    ph["POLARIZ"] = ("single linear", "flux = 2x one polarisation (unpolarised Sun)")
    ph["DISHDIAM"] = (instrument.DISH_M, "[m]")
    ph["TAU_ZEN"] = (rf_calibration.ZENITH_OPTICAL_DEPTH, "zenith optical depth used")
    ph["NRUNS"] = len(kept_runs)
    for station, value in sorted(_reference(day).items()):
        key = "R_" + "".join(ch for ch in station.upper() if ch.isalnum())[:6]
        ph[key] = (int(value), "[SFU] RSTN 1415 MHz noon flux, %s" % station)
    ph["COMMENT"] = "FLUX is above the atmosphere, tracking scallop removed per record."
    ph["COMMENT"] = "ERROR is statistical only (run's own scatter); see the RUNS table."
    ph["COMMENT"] = "QUALITY bits: 1 receiver settling, 2 beam off the Sun, 4 samples dropped."

    flux_hdu = fits.BinTableHDU.from_columns([
        fits.Column("TIME", "D", unit="s", array=c["time"]),
        fits.Column("TIMEDEL", "E", unit="s", array=c["timedel"]),
        fits.Column("FLUX", "E", unit=SFU, array=c["flux"]),
        fits.Column("ERROR", "E", unit=SFU, array=c["error"]),
        fits.Column("TANT", "E", unit="K", array=c["t_a"]),
        fits.Column("SCALLOP", "E", array=c["scallop"]),
        fits.Column("TRANSMIS", "E", array=c["trans"]),
        fits.Column("SUN_ALT", "E", unit="deg", array=c["alt"]),
        fits.Column("SUN_AZ", "E", unit="deg", array=c["az"]),
        fits.Column("QUALITY", "I", array=c["quality"]),
        fits.Column("RUN", "I", array=c["run"]),
    ], name="FLUX")
    fh = flux_hdu.header
    fh["TIMESYS"] = "UTC"
    fh["MJDREF"] = (Time(day.isoformat(), scale="utc").mjd, "00:00 UTC of the day")
    fh["TIMEUNIT"] = "s"
    fh["TIMEREF"] = ("LOCAL", "at the observatory, no light-time correction")
    fh["TREFPOS"] = "TOPOCENTER"
    fh["TSTART"] = (float(c["time"][0]), "[s] first record mid-point")
    fh["TSTOP"] = (float(c["time"][-1]), "[s] last record mid-point")
    fh["TIMEPIXR"] = (0.5, "TIME is the middle of each record")
    fh["DATE-OBS"] = ph["DATE-OBS"]
    fh["DATE-END"] = ph["DATE-END"]

    r = kept_runs
    runs_hdu = fits.BinTableHDU.from_columns([
        fits.Column("FILE", "40A", array=[x["file"] for x in r]),
        fits.Column("OBS_NAME", "24A", array=[x["obs_name"] for x in r]),
        fits.Column("SUN_MON", "I", array=[x["sun_monitor"] for x in r]),
        fits.Column("FIRST", "D", unit="s", array=[x["first"] - t0 for x in r]),
        fits.Column("LAST", "D", unit="s", array=[x["last"] - t0 for x in r]),
        fits.Column("NROWS", "J", array=[x["n_rows"] for x in r]),
        fits.Column("MEANFLUX", "E", unit=SFU, array=[x["mean_flux"] for x in r]),
        fits.Column("SIGMA", "E", unit=SFU, array=[x["sigma"] for x in r]),
        fits.Column("GAIN", "D", unit="count/K", array=[x["gain"] for x in r]),
        fits.Column("TSYS", "E", unit="K", array=[x["t_sys"] for x in r]),
        fits.Column("CAL_UTC", "25A", array=[x["cal_utc"][:25] for x in r]),
        fits.Column("CAL_SRC", "32A", array=[x["cal_source"][:32] for x in r]),
        fits.Column("BEAM_SA", "E", unit="deg2", array=[x["beam_sq_deg"] for x in r]),
        fits.Column("AEFF", "E", unit="m2", array=[x["a_eff"] for x in r]),
        fits.Column("BEAM_SRC", "40A", array=[x["beam_source"][:40] for x in r]),
        fits.Column("SCALSOL", "8A", array=[x["scallop"] for x in r]),
        fits.Column("SCAL_KAL", "E", array=[x["k_alt"] for x in r]),
        fits.Column("SCAL_KAZ", "E", array=[x["k_az"] for x in r]),
        fits.Column("SCAL_PAL", "E", unit="deg", array=[x["phase_alt"] for x in r]),
        fits.Column("SCAL_PAZ", "E", unit="deg", array=[x["phase_az"] for x in r]),
        fits.Column("MOD_PRE", "E", array=[x["mod_before"] for x in r]),
        fits.Column("MOD_POST", "E", array=[x["mod_after"] for x in r]),
        fits.Column("TERMS", "40A", array=[x["terms_source"][:40] for x in r]),
        fits.Column("SCALNOTE", "160A", array=[x["scallop_caption"][:160] for x in r]),
    ], name="RUNS")
    runs_hdu.header["COMMENT"] = "SCAL_KAL/KAZ: scallop amplitude per axis over the beam's curvature."
    runs_hdu.header["COMMENT"] = "MOD_PRE/POST: modulation folded at the pulse phase, quiet records."
    _describe(flux_hdu)
    _describe(runs_hdu)
    tmp = out_path + ".tmp"
    fits.HDUList([prim, flux_hdu, runs_hdu]).writeto(tmp, overwrite=True, checksum=True)
    os.replace(tmp, out_path)
    return out_path


def rstn_filename(day):
    """RSTN's `ddmonyy` name, with our observatory's extension."""
    return "%02d%s%02d.%s" % (day.day, _MONTHS[day.month - 1], day.year % 100, RSTN_EXT)


def rstn_lines(c, t0):
    """The RSTN3 records for a day's columns: one per record, at the UT second
    of its mid-point, the 1415 MHz field the flux in whole SFU or blank for a
    flagged or unusable record, every other field blank. A second already
    written (two records rounding onto one) keeps the first."""
    lines = []
    seen = set()
    i1415 = RSTN_FREQS_MHZ.index(1415)
    for t, flux, q in zip(c["time"], c["flux"], c["quality"]):
        sec = int(round(float(t)))
        if not 0 <= sec < 86400 or sec in seen:
            continue
        seen.add(sec)
        fields = [""] * len(RSTN_FREQS_MHZ)
        if q == 0 and np.isfinite(flux) and 0 <= round(float(flux)) < 10 ** RSTN_FIELD:
            fields[i1415] = "%d" % int(round(float(flux)))
        stamp = datetime.fromtimestamp(t0 + sec, tz=timezone.utc).strftime("%Y%m%d%H%M%S")
        lines.append(RSTN_CODE + stamp + "".join(f.rjust(RSTN_FIELD) for f in fields))
    return lines


def write_rstn(day, c, t0, out_path):
    tmp = out_path + ".tmp"
    with open(tmp, "w", newline="") as fh:
        for line in rstn_lines(c, t0):
            fh.write(line + "\r\n")
    os.replace(tmp, out_path)
    return out_path


def install_examples(out_dir, src_dir=EXAMPLES_DIR):
    """Copy the README and the readers into the products folder, where they
    are out of date or missing. The copies in git are the ones to edit."""
    for name, dest in EXAMPLE_FILES.items():
        src = os.path.join(src_dir, name)
        dst = os.path.join(out_dir, dest)
        if not os.path.exists(src):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            with open(src, "rb") as a, open(dst, "rb") as b:
                if a.read() == b.read():
                    continue
        shutil.copyfile(src, dst)


def export_day(day, obs_dir=OBS_DIR, out_dir=OUT_DIR, files=None, log=print):
    """Write the day's FITS and RSTN3 files. Returns {'fits', 'rstn'} or None."""
    paths = files if files else candidate_files(day, obs_dir)
    runs = []
    for path in paths:
        try:
            result, why = reduce_run(path)
        except (KeyError, ValueError, OSError) as exc:
            log("skipped %s: %s" % (os.path.basename(path), exc))
            continue
        if result is None:
            if why != "not a solar track":
                log("skipped %s: %s" % (os.path.basename(path), why))
            continue
        runs.append(result)
    collected = collect_day(day, runs)
    if collected is None:
        log("no solar-track records on %s" % day.isoformat())
        return None
    c, kept, t0 = collected
    fits_dir = os.path.join(out_dir, "fits")
    rstn_dir = os.path.join(out_dir, "rstn")
    os.makedirs(fits_dir, exist_ok=True)
    os.makedirs(rstn_dir, exist_ok=True)
    install_examples(out_dir)
    out = {
        "fits": write_fits(day, c, kept, t0, os.path.join(
            fits_dir, "acre_srt_1417MHz_%s.fits" % day.strftime("%Y%m%d"))),
        "rstn": write_rstn(day, c, t0, os.path.join(rstn_dir, rstn_filename(day))),
    }
    for r in kept:
        log("%s: %d rows, mean %.1f SFU, sigma %.2f SFU/record; %s"
            % (r["file"], r["n_rows"], r["mean_flux"], r["sigma"],
               r["scallop_caption"] or "scallop: " + r["scallop_why"]))
    log("%s: %d rows (%d flagged) -> %s, %s"
        % (day.isoformat(), c["time"].size, int((c["quality"] != 0).sum()),
           os.path.relpath(out["fits"], out_dir), os.path.relpath(out["rstn"], out_dir)))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("day", nargs="?", help="UTC date, YYYY-MM-DD")
    p.add_argument("--recording", help="export every UTC day this recording touches")
    p.add_argument("--out", default=OUT_DIR, help="products folder (default data/solar_flux)")
    p.add_argument("--obs-dir", default=OBS_DIR, help="where the recordings are")
    p.add_argument("--files", nargs="*", help="particular recordings instead of the day's")
    a = p.parse_args(argv)
    if a.recording:
        days = days_of_recording(a.recording)
    elif a.day:
        days = [datetime.strptime(a.day, "%Y-%m-%d").date()]
    else:
        p.error("give a day or --recording")
    # A day with nothing to write is said so on stdout, not an error: a
    # failed start leaves a recording with no usable records.
    for d in days:
        export_day(d, a.obs_dir, a.out, a.files)
    return 0


if __name__ == "__main__":
    sys.exit(main())
