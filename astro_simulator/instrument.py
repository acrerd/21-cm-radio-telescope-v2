#!/usr/bin/env python3
"""Instrument constants for the Acre Road 3 m dish.

Kept out of astro_simulator.py so that web/make_web_data.py can read them
without importing matplotlib and astropy, and so the measured beam is written
down in exactly one place rather than once per consumer.
"""

# Beam FWHM, measured rather than derived: 18 solar scans on 2026-08-22 fitted
# 5.173 +/- 0.020 deg on the 3.0 m dish (receiver_scheduler/sun_scan.py, which
# fits the width as a free parameter of each raster).
#
# That raw figure is the beam *convolved with the Sun*, which is not a point
# source, so the disk is deconvolved out of it here. The Sun was 0.5271 deg
# across on the day (pyephem). A uniform disk of diameter d has a per-axis
# variance of (d/4)^2, so it acts like a Gaussian of FWHM 0.310 deg, and
# widths add in quadrature: sqrt(5.173^2 - 0.310^2) = 5.164 deg. Confirmed
# numerically by convolving a uniform disk with a trial beam and refitting
# with the scans' own estimator, both densely sampled and on the real 9x9
# raster - all three agree to four decimals.
#
# The correction is only -0.009 deg, below the 0.020 deg scatter of the
# measurement itself, and it is insensitive to how much larger the radio Sun
# is than the optical one: at twice the optical diameter it is still only
# -0.038 deg. It is applied because it is a known systematic that costs
# nothing to remove, not because it matters at this precision.
#
# Deliberately not 1.22 lambda/D, which this was until 2026-08-22. That is the
# first-null radius of the Airy pattern of a *uniformly illuminated* circular
# aperture - the wrong quantity and the wrong aperture at once. The FWHM of
# that uniform aperture is 1.03 lambda/D, not 1.22; and a real dish is tapered
# by the feed's illumination pattern, blocked by the feed and its supports, and
# degraded by surface error, every one of which broadens the main lobe. Taking
# a null radius for a FWHM overstates the uniform case by 19%, which landed
# within 5% of this dish by luck rather than by physics, so the error was
# invisible until the beam was measured.
#
# The deconvolved beam corresponds to 1.281 lambda/D. Beam width scales as
# lambda/D and this simulator is effectively monochromatic at the HI line - a
# 2 MHz band is 0.14% in wavelength - so the reference is scaled by diameter
# alone.
#
# This is the *default*, not a fixed property: both front ends keep an
# editable beam box, so a user can ask for any width the loaded dataset can
# support (see DishSimulator.set_beam, which floors it at min_fwhm).
BEAM_FWHM_REF_DEG = 5.164
BEAM_FWHM_REF_DISH_M = 3.0

DISH_M = 3.0


def beam_fwhm_deg(dish_m=DISH_M):
    """Beam FWHM in degrees at the HI line for a dish of ``dish_m`` diameter."""
    if dish_m <= 0:
        raise ValueError("Dish diameter must be positive")
    return BEAM_FWHM_REF_DEG * BEAM_FWHM_REF_DISH_M / dish_m
