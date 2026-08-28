#!/usr/bin/env python3
"""Tests for tuning.py — where the LO goes and how wide we sample."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tuning


def test_the_line_never_lands_on_dc():
    """The whole point: DC is where the correction eats the signal.

    Tuned to the line, UHD's automatic DC-offset correction subtracts it - a
    three-channel notch 7.6% deep, measured toward the Lockman Hole on
    2026-08-24 before this existed.
    """
    plan = tuning.plan_tuning(tuning.H1_REST_FREQ_HZ, 2.0e6, 327)
    assert plan["tuned_center_freq_hz"] != plan["sky_center_freq_hz"]
    assert plan["lo_offset_hz"] == tuning.DEFAULT_LO_OFFSET_HZ


def test_the_line_stays_inside_the_recorded_band():
    """An offset larger than Nyquist would put the line off the edge entirely."""
    for requested in (1.0e6, 2.0e6, 2.4e6, 4.0e6, 10.0e6):
        plan = tuning.plan_tuning(tuning.H1_REST_FREQ_HZ, requested, 4096)
        half_span = plan["sample_rate_hz"] / 2
        assert abs(plan["lo_offset_hz"]) < half_span, requested


def test_the_line_is_placed_with_margin_not_on_the_shoulder():
    """Inside the flat region, not on its edge.

    The first version of this rule aimed the line at USABLE_HALF_WIDTH itself,
    which put it exactly on the 90% contour - the shoulder - and measured 89%
    on sky. The target is now LINE_PLACEMENT, comfortably inside it.
    """
    for requested in (0.5e6, 2.0e6, 2.4e6, 3.5e6):
        plan = tuning.plan_tuning(tuning.H1_REST_FREQ_HZ, requested, 4096)
        assert plan["line_offset_fraction"] <= tuning.LINE_PLACEMENT + 1e-9
        assert plan["line_offset_fraction"] < tuning.USABLE_HALF_WIDTH, (
            "the line must sit inside the flat region, not on its edge")


def test_a_wide_enough_request_is_left_alone():
    wide = tuning.minimum_sample_rate() + 1.0e6
    plan = tuning.plan_tuning(tuning.H1_REST_FREQ_HZ, wide, 4096)
    assert plan["sample_rate_raised"] is False
    assert plan["sample_rate_hz"] == wide
    assert plan["channels"] == 4096


def test_raising_the_rate_holds_the_resolution():
    """Widening the band silently coarsens every channel unless channels follow.

    The observer asked for a velocity resolution, not for a channel count.
    """
    requested_rate, requested_channels = 2.0e6, 327
    plan = tuning.plan_tuning(tuning.H1_REST_FREQ_HZ, requested_rate,
                              requested_channels)
    assert plan["sample_rate_raised"] is True
    before = requested_rate / requested_channels
    assert plan["channel_width_hz"] == pytest.approx(before, rel=0.02)


def test_channel_counts_are_fast_transform_sizes():
    """5973 is 3 x 11 x 181 and would take the slow FFT path."""
    for n in (327, 572, 1000, 5973, 4097):
        fast = tuning.next_fast_size(n)
        assert fast >= n
        remaining = fast
        for factor in (2, 3, 5):
            while remaining % factor == 0:
                remaining //= factor
        assert remaining == 1, (n, fast)


def test_a_zero_offset_reproduces_the_old_behaviour():
    """Kept so the pre-2026-08-24 observations can be reproduced deliberately."""
    plan = tuning.plan_tuning(tuning.H1_REST_FREQ_HZ, 2.0e6, 327, lo_offset_hz=0)
    assert plan["tuned_center_freq_hz"] == tuning.H1_REST_FREQ_HZ
    assert plan["sample_rate_raised"] is False


def test_the_description_says_what_was_substituted():
    """The page and the log must show the numbers actually used."""
    plan = tuning.plan_tuning(tuning.H1_REST_FREQ_HZ, 2.0e6, 327)
    text = tuning.describe_tuning(plan)
    assert "1421.205752" in text
    assert "raised" in text
    assert "resolution" in text
