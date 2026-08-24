#!/usr/bin/env python3
"""The instrument's frequency response, measured on the sky and stored for reuse.

Everything between the feed and the recorded spectrum shapes the band: the
SAWbird's SAW filter and the amplifier and filter after it, the AD9361's
decimation chain, and the FPGA's own down-converter. Only the first of those is
described by a datasheet and only the second can be queried from the hardware
(see ad9361_filters.py, which computes it and finds it accounts for about 4% of
a roll-off that reaches 40%). The rest is neither documented nor derivable, so
the whole product is measured at once instead - one observation of a patch of
sky with no hydrogen in it is a measurement of the instrument and nothing else.

The Lockman Hole is that patch, but it is not empty. It runs about 1.5% of total
power at the line, a couple of kelvin, so fitting straight through it would bake
a negative line into the template and quietly subtract it from every future
observation. The line is therefore masked and the polynomial interpolates
across: "assume zero" is asserted only where it is actually true. Measured on
2026-08-24, bridging a masked window that wide biases the fit by at most 0.18%,
comfortably inside the noise.

Why a polynomial: fitted on one run and applied to a different run at the same
tuning, the residual falls 4.05% (linear), 1.45% (quadratic), 0.86% (cubic),
0.53% (quartic) and then stops at the radiometric noise floor. This is a
cross-run figure deliberately - fitting and scoring on the same data measures
nothing, because a high enough order always fits itself. The order actually used
is set by how wide a band the tuning demands; see DEFAULT_DEGREE below.

Why the stored file pins its configuration: the same template applied at other
local-oscillator settings degrades at once - 0.51% at the tuning it was measured
at, 1.85% at 0.2 MHz away, 3.63% at 0.6 MHz away. Part of the response belongs
to the baseband filters and moves with the LO while the front end stays with the
sky, so a template is only valid for the tuning that produced it. Applying one
to a different setup would be worse than applying none, hence the refusal rather
than a warning.

The correction is applied when a spectrum is reduced or plotted, never in the
receiver: the HDF5 files stay raw so a better template can be applied later.
"""

import json
import os
from datetime import datetime, timezone

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
BANDPASS_FILE = os.path.join(HERE, "bandpass_template.json")

H1_REST_FREQ_HZ = 1420.405752e6

# Order nine, and the band no wider than the tuning actually needs. Measured by
# cross-run prediction on 2026-08-24 (fit one run, apply to another at the same
# LO; noise floor 0.61%):
#
#   band       order 5   order 7   order 9  order 13
#   +-0.27 Fs   0.493%    0.488%    0.488%    0.488%
#   +-0.40 Fs   0.703%    0.497%    0.491%    0.490%
#   +-0.45 Fs   1.805%    0.870%    0.696%    0.500%
#
# Order nine reaches the noise floor everywhere out to 0.40 and costs nothing on
# a narrower band. Past ~0.42 the FPGA decimator's corner turns over too sharply
# for any sane polynomial - order 13 barely manages - so the band is capped
# rather than the order raised, and the plot is trimmed to the requested band
# well inside it anyway.
DEFAULT_DEGREE = 9
DEFAULT_BAND_FRACTION = 0.41
MAX_BAND_FRACTION = 0.42
DEFAULT_LINE_MASK_HZ = 250e3      # +-53 km/s, covers Lockman Hole emission
DEFAULT_DC_MASK_HZ = 30e3         # the LO artefact

# How closely a template's configuration must match the data it is applied to.
LO_TOLERANCE_HZ = 1e3
RATE_TOLERANCE_HZ = 1.0

TEMPLATE_VERSION = 1


def _config_of(header):
    """The tuning that a template is tied to."""
    lo = header.get("center_freq_hz")
    return {
        "lo_hz": None if lo is None else float(lo),
        "sample_rate_hz": float(header.get("sample_rate_hz", 0.0)),
        "sky_center_freq_hz": (float(header["sky_center_freq_hz"])
                               if header.get("sky_center_freq_hz") is not None
                               else None),
        "lo_offset_hz": (float(header["lo_offset_hz"])
                         if header.get("lo_offset_hz") is not None else None),
        "gain_db": (float(header["gain_db"])
                    if header.get("gain_db") is not None else None),
    }


def _needed_band_fraction(header, margin=1.03):
    """How far from the LO the reduced spectrum will actually reach."""
    rate = float(header.get("sample_rate_hz") or 0.0)
    offset = header.get("lo_offset_hz")
    wanted = header.get("sample_rate_requested_hz")
    if not rate or offset is None or wanted is None:
        return 0.0
    reach = abs(float(offset)) + float(wanted) / 2.0
    return margin * reach / rate


def fit_bandpass(freq_hz, spectra, header, degree=DEFAULT_DEGREE,
                 band_fraction=DEFAULT_BAND_FRACTION,
                 line_mask_hz=DEFAULT_LINE_MASK_HZ,
                 dc_mask_hz=DEFAULT_DC_MASK_HZ,
                 source_name="", source_file=""):
    """Fit the instrument response from an observation of empty sky.

    Returns a template dict ready for save_bandpass(). The polynomial is
    normalised to unit median across the fitted band, so dividing by it flattens
    the spectrum without moving its overall level - the counts-to-kelvin scale is
    a separate matter, deliberately not folded in here.
    """
    freq_hz = np.asarray(freq_hz, float)
    spectra = np.asarray(spectra, float)
    cfg = _config_of(header)
    if not cfg["lo_hz"] or not cfg["sample_rate_hz"]:
        raise ValueError("the observation header does not record its tuning, "
                         "so a template fitted from it could never be matched "
                         "to anything")

    # Cover what this tuning will actually ask for. The plot is trimmed to the
    # requested bandwidth about the *sky* centre, and the LO sits an offset away
    # from that, so the far edge is (offset + bandwidth/2) from the LO - further
    # on one side than the other. Fitting a band symmetric about the LO and
    # narrower than that silently drops the low end of every plot, which is
    # exactly what the first version of this did.
    band_fraction = max(band_fraction, _needed_band_fraction(header))
    band_fraction = min(band_fraction, MAX_BAND_FRACTION)

    nu = freq_hz - cfg["lo_hz"]
    scale = band_fraction * cfg["sample_rate_hz"]
    inside = np.abs(nu) <= scale
    keep = (inside
            & (np.abs(freq_hz - H1_REST_FREQ_HZ) > line_mask_hz)
            & (np.abs(nu) > dc_mask_hz))
    if keep.sum() < 10 * (degree + 1):
        raise ValueError("too few unmasked channels (%d) to fit order %d"
                         % (keep.sum(), degree))

    mean = spectra.mean(axis=0) if spectra.ndim == 2 else spectra
    u = nu / scale
    coef = np.polyfit(u[keep], mean[keep], degree)
    model = np.polyval(coef, u)
    # Unit median over the whole fitted band, not over the unmasked channels.
    # The masked line sits off-centre in a tilted band, so normalising on what
    # survived the mask makes the overall scale depend on how much was masked -
    # measured at 4% here, which would then land in the counts-to-kelvin factor.
    level = float(np.median(model[inside]))
    coef = np.asarray(coef, float) / level

    residual = mean[keep] / (model[keep]) - 1.0
    return {
        "version": TEMPLATE_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_name": source_name,
        "source_file": os.path.basename(source_file) if source_file else "",
        "degree": int(degree),
        "coefficients": [float(c) for c in coef],
        "u_scale_hz": float(scale),
        "band_fraction": float(band_fraction),
        "config": cfg,
        "n_records": int(spectra.shape[0]) if spectra.ndim == 2 else 1,
        "n_channels_fitted": int(keep.sum()),
        "line_mask_hz": float(line_mask_hz),
        "dc_mask_hz": float(dc_mask_hz),
        "fit_residual_rms": float(np.std(residual)),
    }


def save_bandpass(template, path=BANDPASS_FILE):
    with open(path, "w") as fh:
        json.dump(template, fh, indent=2)
    return path


def load_bandpass(path=BANDPASS_FILE):
    """The stored template, or None if there isn't one."""
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            t = json.load(fh)
    except (OSError, ValueError):
        return None
    if t.get("version") != TEMPLATE_VERSION or "coefficients" not in t:
        return None
    return t


def applies_to(template, header):
    """(ok, reason). Refuses rather than warns - see the module docstring."""
    if not template:
        return False, "no bandpass template stored"
    cfg, want = template.get("config", {}), _config_of(header)
    if want["lo_hz"] is None:
        return False, "observation does not record its tuned frequency"
    if cfg.get("lo_hz") is None:
        return False, "template does not record its tuned frequency"
    if abs(cfg["lo_hz"] - want["lo_hz"]) > LO_TOLERANCE_HZ:
        return False, ("template is for LO %.6f MHz, this is %.6f MHz"
                       % (cfg["lo_hz"] / 1e6, want["lo_hz"] / 1e6))
    if abs(cfg.get("sample_rate_hz", 0) - want["sample_rate_hz"]) > RATE_TOLERANCE_HZ:
        return False, ("template is for %.3f Msps, this is %.3f Msps"
                       % (cfg.get("sample_rate_hz", 0) / 1e6,
                          want["sample_rate_hz"] / 1e6))
    return True, ""


def evaluate(template, freq_hz):
    """The template's response on an arbitrary frequency grid.

    NaN outside the band it was fitted over: a polynomial extrapolates without
    any hint that it has left the data behind, and dividing by an extrapolated
    tail would invent structure at exactly the band edges where the roll-off is
    steepest.
    """
    nu = np.asarray(freq_hz, float) - template["config"]["lo_hz"]
    u = nu / template["u_scale_hz"]
    model = np.polyval(np.asarray(template["coefficients"], float), u)
    return np.where(np.abs(u) <= 1.0, model, np.nan)


def apply_bandpass(freq_hz, spectra, header, template=None, path=BANDPASS_FILE):
    """Divide out the instrument response.

    Returns (corrected, note). `corrected` is a new array - the caller's data is
    never modified - and `note` says what happened, for the plot to display: a
    correction the reader cannot see is one they cannot check.
    """
    spectra = np.asarray(spectra, float)
    if template is None:
        template = load_bandpass(path)
    ok, why = applies_to(template, header)
    if not ok:
        return spectra, ("not bandpass corrected - %s" % why)

    model = evaluate(template, freq_hz)
    good = np.isfinite(model) & (model > 0)
    corrected = spectra.copy()
    if spectra.ndim == 2:
        corrected[:, good] = spectra[:, good] / model[good]
        corrected[:, ~good] = np.nan
    else:
        corrected[good] = spectra[good] / model[good]
        corrected[~good] = np.nan

    when = (template.get("created_utc") or "")[:10]
    note = ("bandpass corrected (order %d, measured %s%s)"
            % (template["degree"], when,
               " on " + template["source_name"] if template.get("source_name") else ""))
    if (~good).any():
        note += ", %d channels outside the measured band dropped" % int((~good).sum())
    return corrected, note


def fit_from_observation(path, name="", degree=DEFAULT_DEGREE, out=BANDPASS_FILE):
    """Fit and store a template from a recorded observation of empty sky."""
    from observation_plot import read_observation

    freq_hz, spectra, _stamps, _taus, header = read_observation(path)
    template = fit_bandpass(freq_hz, spectra, header, degree=degree,
                            source_name=name or header.get("obs_name", ""),
                            source_file=path)
    save_bandpass(template, out)
    return template, out


def main():
    import argparse

    p = argparse.ArgumentParser(
        description="Measure the instrument's frequency response from an "
                    "observation of sky with no hydrogen in it.")
    p.add_argument("observation", help="HDF5 file, e.g. a Lockman Hole run")
    p.add_argument("--name", default="", help="what was observed")
    p.add_argument("--degree", type=int, default=DEFAULT_DEGREE)
    p.add_argument("--out", default=BANDPASS_FILE)
    args = p.parse_args()

    t, out = fit_from_observation(args.observation, args.name, args.degree,
                                  args.out)
    c = t["config"]
    print("Fitted order %d over %d channels of %s"
          % (t["degree"], t["n_channels_fitted"], os.path.basename(args.observation)))
    print("   tuning        LO %.6f MHz, %.3f Msps"
          % (c["lo_hz"] / 1e6, c["sample_rate_hz"] / 1e6))
    print("   band fitted   +-%.3f MHz about the LO" % (t["u_scale_hz"] / 1e6))
    print("   masked        H I +-%.0f kHz, DC +-%.0f kHz"
          % (t["line_mask_hz"] / 1e3, t["dc_mask_hz"] / 1e3))
    print("   fit residual  %.4f%%" % (100 * t["fit_residual_rms"]))
    print("   written to    %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
