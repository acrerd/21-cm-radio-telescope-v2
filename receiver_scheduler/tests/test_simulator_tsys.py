"""The simulator starts at the T_sys the telescope actually has.

Both simulators defaulted to 200 K, a round number from before anything was
measured. This receiver has never fitted below ~340 K, so a simulated spectrum
was drawn quieter than any real one and the two could not be compared without
somebody remembering to set a box - which is the sort of thing nobody
remembers, and the discrepancy then reads as a fault in the telescope.

The value is read from the gain calibration in force rather than pinned in
code, so it follows a re-fit. It is a *starting* value: every simulator still
lets the operator type a different one, which is the point of simulating.
"""
import json
import os
from unittest.mock import patch

import pytest

import observatory  # puts astro_simulator/ on the path

import instrument

META = os.path.join(observatory.SIMULATOR_DIR, "web", "data", "meta.json")


class TestTheAccessor:
    def test_it_reads_the_calibration_in_force(self):
        """Not a constant: the gain is refitted every few weeks, and a pinned
        number would be wrong by the time anyone noticed."""
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "gain_calibration.json")) as fh:
            stored = float(json.load(fh)["t_sys_k"])
        assert instrument.measured_t_sys_k() == pytest.approx(stored)

    def test_a_missing_file_falls_back(self, tmp_path):
        """A checkout with no observatory behind it still has to simulate."""
        with patch.object(instrument, "_GAIN_CALIBRATION",
                          str(tmp_path / "nope.json")):
            assert instrument.measured_t_sys_k() == instrument.T_SYS_FALLBACK_K

    def test_rubbish_in_the_file_falls_back(self, tmp_path):
        for content in ('{}', 'not json', '{"t_sys_k": "warm"}',
                        '{"t_sys_k": null}'):
            p = tmp_path / "gain.json"
            p.write_text(content)
            with patch.object(instrument, "_GAIN_CALIBRATION", str(p)):
                assert instrument.measured_t_sys_k() == instrument.T_SYS_FALLBACK_K, content

    def test_an_implausible_fit_is_refused(self, tmp_path):
        """rf_calibration clamps T_sys at a floor, and a fit sitting on its
        own bound is not a measurement. A runaway is worse than the default."""
        for bad in (0.0, 10.0, 49.0, 5001.0, 1e9, -300.0):
            p = tmp_path / "gain.json"
            p.write_text(json.dumps({"t_sys_k": bad}))
            with patch.object(instrument, "_GAIN_CALIBRATION", str(p)):
                assert instrument.measured_t_sys_k() == instrument.T_SYS_FALLBACK_K, bad

    def test_a_plausible_fit_is_used(self, tmp_path):
        p = tmp_path / "gain.json"
        p.write_text(json.dumps({"t_sys_k": 372.5}))
        with patch.object(instrument, "_GAIN_CALIBRATION", str(p)):
            assert instrument.measured_t_sys_k() == pytest.approx(372.5)

    def test_the_fallback_is_a_temperature_this_receiver_could_have(self):
        """340-372 K is the measured range; a fallback outside it would be a
        worse guess than the thing it replaces."""
        assert 300.0 <= instrument.T_SYS_FALLBACK_K <= 400.0


class TestTheWebBuild:
    def test_meta_ships_a_measured_tsys_not_a_round_number(self):
        """Regenerate with: python make_web_data.py --meta

        Deliberately a range and not equality with measured_t_sys_k(): the
        bundle is a snapshot, the gain is refitted every few weeks, and a test
        demanding they match exactly would fail on the observatory's ordinary
        workflow rather than on a mistake. The page asks the scheduler for the
        live value anyway (main.js); this is the standalone fallback.
        """
        with open(META) as fh:
            meta = json.load(fh)
        tsys = meta["defaults"]["tsys"]
        # 100-400 K: the old feed fitted 340-360, the new one (2026-09-22)
        # 185-210. The lower bound guards against a fit sitting on
        # rf_calibration's own floor; the upper against the 200 K default
        # ever having been "measured".
        assert 100.0 <= tsys <= 400.0, (
            "meta.json still ships %r - regenerate it" % tsys)
        assert tsys != 200.0


class TestTheLivePageAsksForIt:
    """No JavaScript engine here, so read the source. What these catch is the
    wiring being half-done, which is how it would actually break."""

    @pytest.fixture
    def js(self):
        base = os.path.join(observatory.SIMULATOR_DIR, "web", "js")
        with open(os.path.join(base, "main.js")) as fh:
            main = fh.read()
        with open(os.path.join(base, "ui.js")) as fh:
            ui = fh.read()
        return main, ui

    def test_the_page_asks_the_scheduler(self, js):
        main, _ = js
        assert '"/api/rf/status"' in main
        assert "setMeasuredTsys" in main
        assert "t_sys_k" in main

    def test_the_hook_exists_and_is_exported(self, js):
        _, ui = js
        assert "function setMeasuredTsys(" in ui
        ret = ui[ui.rindex("return {"):]
        assert "setMeasuredTsys" in ret, "setupUI must expose it"

    def test_it_never_overwrites_a_typed_value(self, js):
        """The operator's number wins. Simulating a different receiver is a
        thing people do, and having the box silently snap back would be worse
        than the stale default this replaces."""
        _, ui = js
        fn = ui[ui.index("function setMeasuredTsys("):]
        fn = fn[:fn.index("\n  }")]
        assert "sky.tsys" in fn and "return false" in fn
