"""Modified Allan deviation of the Thunderbolt's PPS record, and the
transition read off it: where the curve stops falling and turns up."""
import time

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


def _brute_mdev(x, m):
    """MDEV at factor m straight from the definition, skipping every term
    whose 3m samples include a NaN."""
    terms = []
    for j in range(len(x) - 3 * m + 1):
        w = x[j:j + 3 * m]
        if np.isnan(w).any():
            continue
        a = [np.mean(w[i * m:(i + 1) * m]) for i in range(3)]
        terms.append((a[2] - 2 * a[1] + a[0]) ** 2)
    return np.sqrt(np.mean(terms) / (2.0 * m * m))


def test_a_gap_costs_only_the_terms_that_touch_it():
    rng = np.random.default_rng(5)
    x = rng.normal(0, 1e-9, 400)
    x[150:153] = np.nan
    t, d, k = C.mdev(x, 1.0, [1, 4, 20])
    for m, v in zip(t, d):
        assert v == pytest.approx(_brute_mdev(x, int(m)), rel=1e-9)
    assert k[0] == 400 - 2 - 5          # 398 terms, five of which touch the three missing samples


def _blocks_from_seconds(x_s, t0=1_790_000_400.0, block=600, **settings):
    """The long-term log's rows for a one-second phase record."""
    out = []
    for i in range(len(x_s) // block):
        out.append({"start": t0 + i * block, "n_s": block, "normal_s": block, "minor_bits": 0,
                    "pps_ns_mean": float(np.mean(x_s[i * block:(i + 1) * block]) * 1e9),
                    "time_constant_s": settings.get("tc", "100.0"), "damping": "1.000",
                    "cable_delay_ns": settings.get("cable", "")})
    return out


def test_the_log_means_give_the_one_second_mdev():
    """A block's mean is the phase averaged over 600 s, so the log's MDEV at
    tau = M x 600 s is the one-second estimator there, sampled at fewer
    starts: the two agree within their errors."""
    rng = np.random.default_rng(6)
    x = _white_pm_and_random_walk_fm(rng, 2 * 86400, 1e-14)
    lg = C.log_stability(_blocks_from_seconds(x))
    t1, d1, k1 = C.mdev(x, 1.0, [int(t) for t in lg["tau"]])
    e1 = C.mdev_errors(t1, d1, k1, 1.0)
    for t, d, e, ds, es in zip(lg["tau"], lg["mdev"], lg["err"], d1, e1):
        assert abs(d - ds) < 3 * np.hypot(e, es), t
    assert lg["n_gaps"] == 0 and lg["span_s"] == 2 * 86400


def test_the_log_skips_partial_and_missing_blocks():
    rng = np.random.default_rng(7)
    b = _blocks_from_seconds(rng.normal(0, 2e-9, 86400))
    b[40]["n_s"] = b[40]["normal_s"] = 462           # a scheduler restart cut it
    b[60]["normal_s"] = 500                          # not all in normal disciplining
    del b[80]                                        # not written at all
    lg = C.log_stability(b)
    assert lg["n_gaps"] == 3 and lg["n_blocks"] == 144 - 3


def test_the_log_run_starts_an_hour_after_a_survey_or_a_change():
    rng = np.random.default_rng(8)
    b = _blocks_from_seconds(rng.normal(0, 2e-9, 86400))
    t0 = b[0]["start"]
    b[30]["minor_bits"] = 0x20                       # survey in progress
    assert C.log_run_start(b)[0] == t0 + 31 * 600 + C.LOG_SETTLE_S
    for r in b[100:]:
        r["cable_delay_ns"] = "-50.5"                # set at block 100; earlier rows lack the column
    assert C.log_run_start(b)[0] == t0 + 31 * 600 + C.LOG_SETTLE_S    # '' is unknown, not a change
    for r in b[:100]:
        r["cable_delay_ns"] = "0.0"
    start, settings = C.log_run_start(b)
    assert start == t0 + 101 * 600 + C.LOG_SETTLE_S
    assert settings["cable_delay_ns"] == "-50.5"
    lg = C.log_stability(b)
    assert lg["since_utc"] == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start))


def test_a_log_whose_columns_grew_mid_month_reads_whole(tmp_path):
    """append_log_row starts a fresh header line when the columns change,
    and read_log rebinds at it."""
    import thunderbolt
    old = [c for c in thunderbolt.LOG_COLUMNS if c != "cable_delay_ns"]
    path = tmp_path / "thunderbolt_2026-10.csv"
    row = {c: "" for c in thunderbolt.LOG_COLUMNS}
    row.update(utc_start="2026-10-01T00:00:00Z", utc_end="2026-10-01T00:10:00Z", n_s="600",
               normal_s="600", minor_bits="0x0000", pps_ns_mean="0.25", time_constant_s="100.0", damping="1.000")
    path.write_text(",".join(old) + "\n" + ",".join(row[c] for c in old) + "\n")
    row.update(utc_start="2026-10-01T00:10:00Z", utc_end="2026-10-01T00:20:00Z", pps_ns_mean="-0.5",
               cable_delay_ns="-50.5")
    thunderbolt.append_log_row(str(tmp_path), [row[c] for c in thunderbolt.LOG_COLUMNS])
    row["utc_start"] = "2026-10-01T00:20:00Z"
    thunderbolt.append_log_row(str(tmp_path), [row[c] for c in thunderbolt.LOG_COLUMNS])
    assert path.read_text().count("utc_start") == 2  # one new header, not one per row
    b = C.read_log(str(tmp_path))
    assert [r["pps_ns_mean"] for r in b] == [0.25, -0.5, -0.5]
    assert [r["cable_delay_ns"] for r in b] == ["", "-50.5", "-50.5"]


def test_the_transition_uses_the_log_beyond_ten_minutes():
    rng = np.random.default_rng(9)
    x = _white_pm_and_random_walk_fm(rng, 86400, 3e-13)
    s = C.stability(_rows(x[-20000:]), log_blocks=_blocks_from_seconds(x))
    assert s["transition_from"] == "one-second + log" and s["log"]["tau"][0] == 600
    assert C.stability(_rows(x[-20000:]))["transition_from"] == "one-second"
