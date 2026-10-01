"""Modified Allan deviation of the Thunderbolt's PPS record, and the
transition read off it: where the curve stops falling and turns up."""
import numpy as np
import pytest

import clocks as C


def test_white_frequency_noise_matches_the_textbook():
    """MDEV equals ADEV at tau0 (sigma_y exactly), falls as tau^-1/2, and sits
    at 1/sqrt(2) of ADEV at long tau (NIST SP 1065 table 8)."""
    rng = np.random.default_rng(1)
    y = rng.normal(0, 1e-11, 200_000)
    x = np.concatenate([[0.0], np.cumsum(y)])
    t, d, _ = C.mdev(x, 1.0, [1, 10, 100, 1000])
    assert d[0] == pytest.approx(1e-11, rel=0.01)
    assert np.polyfit(np.log10(t[1:]), np.log10(d[1:]), 1)[0] == pytest.approx(-0.5, abs=0.05)
    assert d[2] / (1e-11 * 100 ** -0.5) == pytest.approx(0.707, abs=0.04)


def test_white_phase_noise_falls_as_tau_to_the_minus_three_halves():
    rng = np.random.default_rng(2)
    x = rng.normal(0, 1e-9, 100_000)
    t, d, _ = C.mdev(x, 1.0, [4, 16, 64, 256])
    assert np.polyfit(np.log10(t), np.log10(d), 1)[0] == pytest.approx(-1.5, abs=0.05)


def _rows(x_s, start=1_790_000_000.0, gap_at=None):
    t = start + np.arange(len(x_s), dtype=float)
    if gap_at is not None:
        t[gap_at:] += 120.0
    return [(float(ti), 0.0, float(xi * 1e9), 2.11, 43.5, 0) for ti, xi in zip(t, x_s)]


def _white_pm_and_random_walk_fm(rng, n, rw):
    """2 ns of white phase noise (GPS-like) plus random-walk frequency noise:
    MDEV falls, bottoms out and rises, so the curve has a transition."""
    y = np.cumsum(rng.normal(0, rw, n))
    return rng.normal(0, 2e-9, n) + np.concatenate([[0.0], np.cumsum(y[:-1])])


def test_the_transition_is_where_the_measured_curve_turns_up():
    """Averaged over 20 runs the curve bottoms out at 205 s with rw = 3e-13
    over 20 000 s; each single run finds it there or one point along, and
    says the rise beyond it is real."""
    for seed in (1, 2, 5, 8):
        x = _white_pm_and_random_walk_fm(np.random.default_rng(seed), 20_000, 3e-13)
        tr = C.stability(_rows(x))["transition"]
        assert tr["found"]
        assert 140.0 <= tr["tau_s"] <= 300.0
        assert tr["span_s"][0] <= tr["tau_s"] <= tr["span_s"][1]


def test_a_curve_still_falling_has_no_transition():
    """White phase noise alone falls at every tau: no turn is claimed, and
    the lowest point is the last, where the record ends."""
    for seed in (1, 2, 3):
        s = C.stability(_rows(np.random.default_rng(seed).normal(0, 2e-9, 20_000)))
        tr = s["transition"]
        assert not tr["found"]
        assert tr["tau_s"] == tr["tau_max_s"] == s["tau"][-1]


def test_no_noise_model_is_fitted_or_reported():
    """The GPS/output split cannot be made from a record the unit has
    smoothed (clocks.py, 2026-09-30); nothing may claim it."""
    s = C.stability(_rows(np.random.default_rng(1).normal(0, 2e-9, 3000)))
    assert not any(k.startswith("fit_") or k in ("coefficients", "crossover_s") for k in s)


def test_only_the_last_unbroken_run_is_used():
    rng = np.random.default_rng(4)
    s = C.stability(_rows(rng.normal(0, 1e-9, 3000), gap_at=1000))
    assert s["ok"] and s["n_s"] == 2000 and s["gap_trimmed"] == 1000


def test_the_white_noise_expectation_matches_white_noise():
    """The plot's white-phase-noise line, sqrt(3) sigma_x tau^-3/2 at the
    reported sigma_x, lies on the measured MDEV of pure white phase noise."""
    s = C.stability(_rows(np.random.default_rng(3).normal(0, 2e-9, 20_000)))
    assert s["sigma_x_s"] == pytest.approx(2e-9, rel=0.02)
    for t, d in zip(s["tau"], s["mdev"]):
        if t >= 4:
            assert d == pytest.approx(np.sqrt(3) * s["sigma_x_s"] * t ** -1.5, rel=0.25)
    ref = s["reference"]
    assert len(ref["tau_s"]) == len(ref["mdev"]) and ref["source"].startswith("http")


def test_too_short_a_record_is_refused():
    assert not C.stability(_rows(np.zeros(10)))["ok"]
