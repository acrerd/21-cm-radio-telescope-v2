"""The velocity axis, and getting it into the frame H I is quoted in.

Until 2026-08-25 a recorded spectrum was plotted on a topocentric axis - the
raw Doppler shift of the observed frequency, carrying the Earth's orbit and
rotation and the Sun's motion through the local standard of rest. It was
honestly labelled as such, but it meant nothing recorded here could be compared
with published data, with the simulator, or with the same source six months
later: the offset reaches ~30 km/s and changes with direction and date.

The conversion is the one rf_calibration already ran in the other direction to
fit the gain, so it arrives with evidence rather than on trust. What these tests
add is the sign, the direction handling for each coordinate system, and the
refusal to guess when the direction is unknown.
"""

from datetime import datetime, timezone

import numpy as np
import pytest

import observation_plot as op

C_KM_S = 299792.458
H1 = 1420.405752e6

# 2026-08-25 11:26 UTC, the epoch of the anticentre calibration this was
# checked against. Fixed, because the barycentric term moves 1.95 km/s in a
# week and a test evaluated at "now" would drift with the calendar.
WHEN = datetime(2026, 8, 25, 11, 26, 3, tzinfo=timezone.utc)


def header(system="galactic", c1=184.0, c2=0.0):
    return {"coord_system": system,
            "coord1_deg": c1, "coord1_min": 0, "coord1_sec": 0.0,
            "coord2_deg": c2, "coord2_min": 0, "coord2_sec": 0.0}


def test_the_direction_is_recovered_from_each_coordinate_system():
    """Galactic passes straight through; the others are converted."""
    assert op.observation_direction(header(), WHEN) == pytest.approx((184.0, 0.0))

    # Cas A, RA 23h23m Dec +58.8, is l=111.7 b=-2.1.
    l, b = op.observation_direction(header("radec", 23.39, 58.8), WHEN)
    assert l == pytest.approx(111.7, abs=0.5)
    assert b == pytest.approx(-2.1, abs=0.5)

    # An alt/az pointing is a direction on the sky only at a given moment.
    altaz = op.observation_direction(header("altaz", 60.0, 180.0), WHEN)
    assert altaz is not None
    later = op.observation_direction(
        header("altaz", 60.0, 180.0),
        datetime(2026, 8, 25, 17, 26, 3, tzinfo=timezone.utc))
    assert abs(altaz[0] - later[0]) > 10.0, \
        "a fixed alt/az points somewhere else six hours later"


def test_an_unknown_direction_is_admitted_rather_than_guessed():
    """A velocity axis silently in the wrong frame is worse than a labelled one."""
    assert op.lsr_offset_km_s(header("satellite"), WHEN) is None
    assert op.lsr_offset_km_s({}, WHEN) is None
    assert op.lsr_offset_km_s(header(), None) is None, \
        "without an epoch the barycentric term cannot be evaluated"


def test_the_correction_is_subtracted_not_added():
    """The sign, which is the whole thing and is easy to get backwards.

    frame_offset returns what to ADD to an LSR axis to express it in the
    observed frame, so recovering LSR from an observation subtracts it. Getting
    this the wrong way round doubles the error instead of removing it, and the
    result still looks like a plausible spectrum.
    """
    import sys
    if op.SIMULATOR_DIR not in sys.path:
        sys.path.insert(0, op.SIMULATOR_DIR)
    import astro_simulator as A

    dv, glon, glat = op.lsr_offset_km_s(header(), WHEN)
    direct = A.frame_offset(184.0, 0.0, "topo", WHEN) / 1000.0
    assert dv == pytest.approx(direct, abs=1e-9)

    # And the application: v_lsr = v_topo - dv.
    v_topo = -11.17
    assert (v_topo - dv) == pytest.approx(v_topo - direct)


def test_a_known_lsr_velocity_survives_the_round_trip():
    """Put a line at a known v_LSR, observe it, and get the velocity back.

    Built the way rf_calibration builds its model - LSR velocity shifted into
    the topocentric frame and turned into a sky frequency - so this closes the
    loop against the path that was already trusted for the gain fit.
    """
    import sys
    if op.SIMULATOR_DIR not in sys.path:
        sys.path.insert(0, op.SIMULATOR_DIR)
    import astro_simulator as A

    for glon, glat, v_lsr_true in ((184.0, 0.0, 0.0), (80.0, 0.0, -15.0),
                                   (120.0, 30.0, 7.5)):
        head = header("galactic", glon, glat)
        dv_m_s = A.frame_offset(glon, glat, "topo", WHEN)
        v_topo = v_lsr_true + dv_m_s / 1000.0
        freq_hz = H1 * (1.0 - v_topo / C_KM_S)

        dv, _, _ = op.lsr_offset_km_s(head, WHEN)
        recovered = C_KM_S * (1.0 - freq_hz / H1) - dv
        assert recovered == pytest.approx(v_lsr_true, abs=1e-6), \
            "l=%.0f b=%.0f did not round-trip" % (glon, glat)


def test_the_correction_is_large_enough_to_matter():
    """If it were negligible there would be no reason for any of this.

    Toward the anticentre on this date it is ~15 km/s, which at 0.49 kHz
    channels is some 145 channels - and it is a fifth of the width of the line
    it sits under.
    """
    dv, _, _ = op.lsr_offset_km_s(header(), WHEN)
    assert abs(dv) > 5.0
    assert abs(dv) < 40.0, "no line-of-sight correction should exceed ~30 km/s"


def test_it_moves_the_anticentre_line_to_where_the_survey_puts_it():
    """The check that made this believable, kept as a test.

    l=184 b=0 is the galactic anticentre, where differential rotation
    contributes nothing along the line of sight, so the H I sits near v_LSR 0.
    HI4PI puts the peak at +3.97 km/s. The 2026-08-25 calibration observation
    of that direction peaked at -11.17 km/s topocentric; corrected it lands at
    +3.75, which is 0.22 km/s from the survey - against 15 km/s before.

    Uses the simulator for the survey value rather than a stored number, so it
    keeps testing the agreement rather than a memory of it.
    """
    import rf_calibration

    sim = rf_calibration.load_simulator()
    v, ta = sim.spectrum(184.0, 0.0)[:2]
    survey_peak_km_s = float(v[int(np.nanargmax(ta))]) / 1000.0
    assert abs(survey_peak_km_s) < 10.0, "the anticentre should sit near v_LSR 0"

    dv, _, _ = op.lsr_offset_km_s(header(), WHEN)
    measured_topocentric = -11.17
    corrected = measured_topocentric - dv
    assert abs(corrected - survey_peak_km_s) < 1.0, (
        "corrected peak %+.2f against survey %+.2f" % (corrected, survey_peak_km_s))
    assert abs(measured_topocentric - survey_peak_km_s) > 10.0, (
        "the uncorrected axis should be far from the survey, or this test is "
        "not demonstrating anything")


def test_the_clock_term_is_only_carried_from_a_constrained_fit():
    """The correction that inverted a documented conclusion.

    The B210's TCXO scales the frequency axis, which over 2 MHz is a pure
    velocity shift. It was recorded as unusable - "the clock really moved by
    3.7 ppm in an hour and a half" - and that reading was wrong. Re-fitting the
    eight archived calibrations against a settled bandpass showed the scatter
    tracks *line strength*, not time: 0.999 correlations on the plane give
    -2.63 and -2.09 ppm, while correlations of 0.41 to 0.93 on weak
    high-latitude fields give -6.6 to +18.4. A shift with no line to hold it
    slides onto whatever is nearby and reports a confident number.

    Constrained fits only: -2.36 +- 0.27 ppm across 18 hours, or +-0.08 km/s.
    An ordinary TCXO behaving like one - which is what made carrying the value
    between observations defensible.
    """
    import rf_calibration

    good = {"velocity_shift_km_s": -0.7875, "correlation": 0.99918,
            "shift_at_search_limit": False}
    assert rf_calibration.trustworthy_velocity_shift(good) == pytest.approx(-0.7875)

    for why, cal in (
            ("a weak line lets the shift slide", dict(good, correlation=0.93)),
            ("nothing to fit at all", dict(good, correlation=0.41)),
            ("a limit is not a measurement", dict(good, shift_at_search_limit=True)),
            ("no fit recorded", {}),
    ):
        assert rf_calibration.trustworthy_velocity_shift(cal) is None, why


def test_the_clock_term_is_subtracted_like_the_frame_term():
    """Sign again, checked by line centroid rather than peak channel.

    velocity_shift_km_s is the measured line sitting below the model, so
    removing it subtracts. On the 2026-08-25 anticentre observation the
    measured centroid was 0.758 km/s below HI4PI against a stored shift of
    -0.787; subtracting takes the disagreement to 0.030 km/s. Adding it would
    have doubled the error to 1.5 km/s while still looking like a spectrum.
    """
    shift = -0.7875
    measured_centroid, survey_centroid = 1.785, 2.543
    assert abs((measured_centroid - shift) - survey_centroid) < 0.05
    assert abs((measured_centroid + shift) - survey_centroid) > 1.0


def test_a_galactic_drift_entry_is_read_in_its_own_frame():
    """A drift entry carries drift_frame; reading every drift as RA/Dec turned
    last night's Cas A scan (l=111.735, galactic) into RA 111.7 hours."""
    head = header("drift", 111.735, -2.130)
    head["drift_frame"] = "galactic"
    assert op.observation_direction(head, WHEN) == pytest.approx((111.735, -2.130))
    head["drift_frame"] = "radec"
    l, b = op.observation_direction(dict(head, coord1_deg=23.39, coord2_deg=58.8), WHEN)
    assert l == pytest.approx(111.7, abs=0.5), "an RA/Dec drift entry still converts"
