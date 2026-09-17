"""The tracking scallop (`scallop.py`), and taking it out of a tracked compact source.

What these guard: it is applied only where it is real (a tracked compact
source, never a parked mount and never a field that may be diffuse); an
injected scallop of known amplitude and phase is recovered and removed; a
series with no scallop is left alone; an implausible fit is refused rather
than applied; and the correction acts on the source term, which is what makes
it belong after T_sys rather than on the counts.
"""
import math

import numpy as np
import pytest

import scallop

ephem = pytest.importorskip("ephem")

TERMS = {"IE": -1.456, "IA": 0.599, "AN": -0.458, "AE": -0.883,
         "CA": -1.356, "NPAE": 0.0, "TF": 0.0, "AZSCALE": 0.0004}

SITE = {"site_lat_deg": 55.902426, "site_lon_deg": -4.307865, "site_height_m": 50.0}


def _header(**over):
    h = dict(SITE, observation_mode="track", coord_system="object",
             object_name="sun", beam_fwhm_deg=4.57)
    h.update(over)
    return h


def _run(hours=2.0, tau=3.0, start=1789650000.0):
    """Record times over a solar track long enough to fit."""
    n = int(hours * 3600 / tau)
    return start + tau * np.arange(n)


class TestWhereItApplies:
    def test_a_tracked_named_object_yes(self):
        ok, why = scallop.applies_to(_header())
        assert ok and why == ""
        assert scallop.applies_to(_header(object_name="moon"))[0]

    def test_a_parked_mount_has_no_scallop(self):
        """A drift scan does not move, so the quantisation never walks."""
        ok, why = scallop.applies_to(_header(observation_mode="drift"))
        assert not ok and "parked" in why

    def test_a_field_that_may_be_diffuse_is_refused(self):
        """The loss is the beam moving off a source smaller than itself.
        Diffuse emission fills the beam however far it is offset, so
        correcting an H I field would inject a modulation that was not there."""
        ok, why = scallop.applies_to(_header(coord_system="galactic", object_name=""))
        assert not ok and "diffuse" in why
        ok, why = scallop.applies_to(_header(coord_system="radec", object_name="cas a"))
        assert not ok and "compact" in why


def _inject(header, stamps, terms, k_alt, k_az, phase_alt=0.0, phase_az=0.0,
            trend=None, noise=0.0, seed=1):
    """A per-record antenna temperature with a known scallop in it."""
    d_alt, d_az, t_alt = scallop.drive_demand(header, stamps, terms)
    e_alt, e_az = scallop.sky_offsets(d_alt, d_az, t_alt, phase_alt, phase_az)
    g = 1.0 - k_alt * e_alt ** 2 - k_az * e_az ** 2
    x = (stamps - stamps[0]) / (stamps[-1] - stamps[0])
    base = 1600.0 * (1.0 + 0.15 * x - 0.05 * x ** 2) if trend is None else trend
    rng = np.random.default_rng(seed)
    return base * g * (1.0 + noise * rng.standard_normal(len(stamps))), g


class TestFitAndCorrect:
    def test_an_injected_scallop_is_recovered_and_removed(self):
        header, stamps = _header(), _run()
        k = 4 * math.log(2) / 4.57 ** 2
        values, g = _inject(header, stamps, TERMS, k, k, 0.0, 0.0, noise=0.002)
        out, rep = scallop.correct(values, stamps, header, terms=TERMS)
        assert rep["ok"], rep["why"]
        assert rep["k_alt"] == pytest.approx(k, rel=0.2)
        assert rep["k_az"] == pytest.approx(k, rel=0.25)
        assert abs(rep["phase_alt_deg"]) < 0.05 and abs(rep["phase_az_deg"]) < 0.05
        # the modulation is gone: fold on the drive phase and compare
        before = _fold(values, stamps, header, TERMS, "alt")
        after = _fold(out, stamps, header, TERMS, "alt")
        assert after < 0.3 * before

    def test_a_phase_offset_is_found_rather_than_assumed(self):
        """The pointing model's own residual shifts the scallop's phase - 0.067
        deg of it on 2026-09-17 - and a correction applied at the wrong phase
        adds modulation instead of removing it."""
        header, stamps = _header(), _run()
        k = 4 * math.log(2) / 4.57 ** 2
        values, _ = _inject(header, stamps, TERMS, k, k, phase_alt=0.12, noise=0.002)
        out, rep = scallop.correct(values, stamps, header, terms=TERMS)
        assert rep["ok"]
        assert rep["phase_alt_deg"] == pytest.approx(0.12, abs=0.04)
        before = _fold(values, stamps, header, TERMS, "alt", phase=0.12)
        after = _fold(out, stamps, header, TERMS, "alt", phase=0.12)
        assert after < 0.4 * before
        # and applying it at the phase the model alone would have given makes
        # things worse, which is why the fit carries the offset
        naive = dict(rep, phase_alt_deg=0.0, phase_az_deg=0.0)
        worse = values / scallop.gain(naive, stamps, header, TERMS)
        assert _fold(worse, stamps, header, TERMS, "alt", phase=0.12) > 0.8 * before

    def test_nothing_is_applied_to_a_series_with_no_scallop(self):
        header, stamps = _header(), _run()
        rng = np.random.default_rng(4)
        flat = 1600.0 * (1.0 + 0.003 * rng.standard_normal(len(stamps)))
        out, rep = scallop.correct(flat, stamps, header, terms=TERMS)
        assert not rep["ok"] and "not detected" in rep["why"]
        assert out is not flat or np.allclose(out, flat)
        assert np.allclose(out, flat)

    def test_an_implausible_amplitude_is_refused(self):
        """Something other than the scallop has been fitted - the guard is
        against a runaway, not against a beam 20% off the Gaussian."""
        header, stamps = _header(), _run()
        k = 4 * math.log(2) / 4.57 ** 2
        values, _ = _inject(header, stamps, TERMS, 20 * k, 20 * k, noise=0.001)
        out, rep = scallop.correct(values, stamps, header, terms=TERMS)
        assert not rep["ok"] and "outside" in rep["why"]
        assert np.allclose(out, values)

    def test_a_short_run_is_refused_rather_than_extrapolated(self):
        header = _header()
        stamps = _run(hours=0.02)
        out, rep = scallop.correct(np.full(len(stamps), 1600.0), stamps, header, terms=TERMS)
        assert not rep["ok"] and "too few records" in rep["why"]

    def test_the_gain_is_bounded_and_unity_without_a_fit(self):
        header, stamps = _header(), _run(hours=0.2)
        g = scallop.gain({"ok": False}, stamps, header, TERMS)
        assert np.all(g == 1.0)
        k = 4 * math.log(2) / 4.57 ** 2
        g = scallop.gain({"ok": True, "k_alt": k, "k_az": k,
                          "phase_alt_deg": 0.0, "phase_az_deg": 0.0},
                         stamps, header, TERMS)
        assert g.max() <= 1.0 and g.min() >= 0.9
        # A quarter pulse off costs what the beam says it should. The two axes
        # step at their own rates and both can be at their worst at once, so
        # the deepest loss lies between one axis alone and both together -
        # azimuth's half-pulse is smaller on the sky by cos(alt).
        d_alt, d_az, t_alt = scallop.drive_demand(header, stamps, TERMS)
        c2 = float(np.cos(np.radians(t_alt.mean())) ** 2)
        assert k * 0.25 ** 2 * 0.9 <= (1 - g.min()) <= k * 0.25 ** 2 * (1 + c2) * 1.05


def _fold(values, stamps, header, terms, axis, phase=0.0, nb=12):
    """Peak-to-peak of the series folded on the drive's quantisation phase."""
    d_alt, d_az, _ = scallop.drive_demand(header, stamps, terms)
    d = d_alt if axis == "alt" else d_az
    t = stamps - stamps[0]
    r = values / np.polyval(np.polyfit(t, values, 6), t) - 1.0
    ph = np.mod((d + phase) / scallop.PULSE_DEG, 1.0)
    idx = np.clip((ph * nb).astype(int), 0, nb - 1)
    prof = np.array([r[idx == j].mean() for j in range(nb)])
    return float(prof.max() - prof.min())


class TestItCorrectsTheSourceNotTheSystem:
    def test_the_correction_is_applied_to_antenna_temperature(self):
        """counts = G B (T_sys + g T_A): the scallop multiplies the source and
        not the system temperature, so a correction applied to the total power
        would be wrong by the ratio of the two - 22% on a 1636 K Sun over a
        357 K system."""
        header, stamps = _header(), _run()
        k = 4 * math.log(2) / 4.57 ** 2
        t_a, g = _inject(header, stamps, TERMS, k, k, noise=0.0005)
        out, rep = scallop.correct(t_a, stamps, header, terms=TERMS)
        assert rep["ok"]
        # recovered source, against the same series with the total power
        # "corrected" instead - the mistake this guards
        total = t_a + 357.0
        wrong, _ = scallop.correct(total, stamps, header, terms=TERMS)
        assert _fold(out, stamps, header, TERMS, "alt") < _fold(t_a, stamps, header, TERMS, "alt")
        assert rep["k_alt"] == pytest.approx(k, rel=0.2)


class TestPointingTerms:
    def test_embedded_terms_are_preferred_and_the_source_is_named(self):
        import json
        h = _header(pointing_terms=json.dumps(TERMS))
        terms, source = scallop.pointing_terms(h, fallback={"IE": 0.0})
        assert "recording" in source and terms["IE"] == pytest.approx(TERMS["IE"])
        terms, source = scallop.pointing_terms(_header(), fallback={"IE": 0.5})
        assert "controller" in source and terms["IE"] == 0.5
        terms, source = scallop.pointing_terms(_header(pointing_terms="not json"),
                                               fallback={"IE": 0.5})
        assert "controller" in source and terms["IE"] == 0.5

    def test_a_recording_with_no_terms_falls_back_to_the_fitted_model(self):
        """The reduction must never run with *no* pointing model. Without it
        the reconstructed demand is over a degree out and varies across the
        sky, so the quantisation phase is wrong and the fit returns a negative
        amplitude - which is what the plot path did on 2026-09-17, reporting
        'not detected' on a track the same code fitted at 37 sigma when it was
        handed the terms."""
        terms, source = scallop.pointing_terms(_header(), fallback=None)
        assert terms, "no model at all is never the right answer"
        assert "fitted" in source
        assert set(terms) >= {"IE", "IA", "AN", "AE"}

    def test_the_fit_collapses_without_the_model_which_is_why_there_is_a_fallback(self):
        header, stamps = _header(), _run()
        k = 4 * math.log(2) / 4.57 ** 2
        values, _ = _inject(header, stamps, TERMS, k, k, noise=0.002)
        assert scallop.correct(values, stamps, header, terms=TERMS)[1]["ok"]
        # the same data reduced with no model at all
        _, rep = scallop.correct(values, stamps, header, terms={})
        assert not rep["ok"] or rep["k_alt"] < 0.5 * k


class TestThePlotCaption:
    def test_it_fits_a_figure_and_names_the_pointing_model(self):
        """The full `why` is written for the log; on the plot it ran off the
        right edge of the figure, which is how the terms line came to be
        checked at all."""
        header, stamps = _header(), _run()
        k = 4 * math.log(2) / 4.57 ** 2
        values, _ = _inject(header, stamps, TERMS, k, k, noise=0.002)
        _, rep = scallop.correct(values, stamps, header)
        line = scallop.plot_caption(rep)
        assert len(line) < 130, line
        assert "p-p" in line and "terms" in line

    def test_it_is_empty_where_the_scallop_does_not_arise(self):
        assert scallop.plot_caption({}) == ""
        assert scallop.plot_caption({"ok": False, "applies": False, "why": "drift"}) == ""

    def test_a_refusal_still_says_which_model_was_used(self):
        line = scallop.plot_caption({"applies": True, "ok": False, "why": "alt not detected",
                                     "terms_source": "none - refraction only"})
        assert "left in" in line and "NONE" in line


class TestEachAxisIsJudgedOnItsOwn:
    def test_an_undetected_axis_is_zeroed_not_carried(self):
        """A negative fitted amplitude applied would *amplify* that axis's
        modulation. One axis being well measured must not drag the other in."""
        header, stamps = _header(), _run()
        k = 4 * math.log(2) / 4.57 ** 2
        values, _ = _inject(header, stamps, TERMS, k, 0.0, noise=0.002)
        out, rep = scallop.correct(values, stamps, header, terms=TERMS)
        assert rep["ok"] and rep["axes_used"] == 1
        assert rep["k_alt"] == pytest.approx(k, rel=0.2)
        assert rep["k_az"] == 0.0
        assert "az not detected" in rep["why"]
        # and the altitude scallop really did come out
        assert (_fold(out, stamps, header, TERMS, "alt")
                < 0.3 * _fold(values, stamps, header, TERMS, "alt"))
