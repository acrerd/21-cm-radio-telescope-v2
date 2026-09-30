"""Modified Allan deviation of the Thunderbolt's PPS record, and the fit that
separates GPS measurement noise from the output's noise by slope."""
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


def test_the_fit_puts_each_noise_where_it_belongs():
    """GPS-like white phase noise (2 ns) plus oscillator-like white frequency
    noise (3e-12) over 5.5 h: the GPS component dominates at 1 s and the output
    at the longest tau, and the crossover lands near where the two curves
    cross. Analytically that is 1620 s (white PM measured at 3.44e-9 tau^-3/2
    for 2 ns; white FM at 3e-12 / sqrt(2) tau^-1/2 at long tau). Six seeds gave
    930-3300 s: with this much record the crossover is good to a factor of
    about two, and the tolerance says so."""
    for seed in (3, 5, 8):
        rng = np.random.default_rng(seed)
        n = 20_000
        x = rng.normal(0, 2e-9, n) + np.concatenate([[0.0], np.cumsum(rng.normal(0, 3e-12, n - 1))])
        s = C.stability(_rows(x))
        assert s["ok"]
        gps, out = np.array(s["fit_gps"]), np.array(s["fit_output"])
        assert gps[0] > 10 * out[0] and out[-1] > gps[-1]
        assert np.log(s["crossover_s"]) == pytest.approx(np.log(1620.0), abs=np.log(2.5))


def test_only_the_last_unbroken_run_is_used():
    rng = np.random.default_rng(4)
    s = C.stability(_rows(rng.normal(0, 1e-9, 3000), gap_at=1000))
    assert s["ok"] and s["n_s"] == 2000 and s["gap_trimmed"] == 1000


def test_too_short_a_record_is_refused():
    assert not C.stability(_rows(np.zeros(10)))["ok"]
