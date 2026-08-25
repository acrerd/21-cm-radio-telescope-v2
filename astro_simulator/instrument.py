#!/usr/bin/env python3
"""Instrument constants for the Acre Road 3 m dish.

Kept out of astro_simulator.py so that web/make_web_data.py can read them
without importing matplotlib and astropy, and so the measured beam is written
down in exactly one place rather than once per consumer.
"""

import math

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

# Where the telescope is. This is the one place it is written down; everything
# else imports it, because a position appearing in several files is a position
# that will eventually differ between them. It had reached nine literals across
# six files, one of which - the simulator's own default of 55.87, -4.29 - was
# 3.6 km out and quietly wrong.
#
# This is the *true* surveyed site. The pointing model's tilt terms belong in
# the pointing model: the observer position must never be nudged to make the
# pointing fit, which turns a surveyed constant into a free parameter of a fit
# that has enough of them already.
# Main-beam efficiency, and the reason it is one.
#
# The beam is measured, and a measured beam already determines everything this
# would otherwise be asked to supply. For extended emission the antenna
# temperature is the beam-weighted brightness temperature; for a point source the
# effective area follows from the antenna theorem, A_e * Omega_A = lambda^2, with
# Omega_A the solid angle of that same beam. One measurement, both answers, no
# free parameter - so putting an efficiency in front of either is adding back a
# quantity the beam has already fixed, and doing it to one and not the other is
# how the calibration tab and the simulator came to disagree by 1/0.7 about the
# same patch of sky on 2026-08-24.
#
# What a value below one really encodes is power in sidelobes that have not been
# measured. The solar scans constrain the main lobe and say nothing about the
# rest, so the pattern is taken to be the measured beam and nothing else. The
# honest consequence is not that the extended sky needs scaling down - sidelobes
# see sky of much the same brightness, so extended predictions are barely
# affected either way - but that *point source* predictions become upper limits,
# since real sidelobes would reduce the effective area. Say so when it matters
# rather than discounting the whole sky to hide it.
MAIN_BEAM_EFFICIENCY = 1.0

SITE_NAME = "Acre Road"
SITE_LAT_DEG = 55.902426
SITE_LON_DEG = -4.307865
SITE_HEIGHT_M = 50.0


def beam_fwhm_deg(dish_m=DISH_M):
    """Beam FWHM in degrees at the HI line for a dish of ``dish_m`` diameter."""
    if dish_m <= 0:
        raise ValueError("Dish diameter must be positive")
    return BEAM_FWHM_REF_DEG * BEAM_FWHM_REF_DISH_M / dish_m


# H I rest frequency and c, for turning the beam into a collecting area. Kept
# here rather than imported so this file stays free of anything but arithmetic -
# it is read by the scheduler, the simulator and the browser bundle alike.
H1_REST_FREQ_HZ = 1420.405752e6
C_M_S = 299792458.0
BOLTZMANN = 1.380649e-23


def beam_solid_angle_sr(dish_m=DISH_M):
    """Main-beam solid angle for a Gaussian beam of the measured width.

    1.133 theta^2 is the exact integral of a 2-D Gaussian expressed through its
    FWHM (pi/(4 ln 2)), so this follows from the beam having been *measured*
    rather than assumed - 5.164 deg off eighteen solar scans, deconvolved.
    """
    fwhm_rad = math.radians(beam_fwhm_deg(dish_m))
    return 1.133 * fwhm_rad ** 2


def effective_area_m2(dish_m=DISH_M):
    """Collecting area from the antenna theorem, A_e * Omega_A = lambda^2.

    Derived from the measured beam, never from an assumed aperture efficiency:
    that is the same argument that put MAIN_BEAM_EFFICIENCY at 1.0 rather than
    at a number chosen to make an answer come out. For the 3 m dish this gives
    4.84 m^2 against a physical 7.07, an aperture efficiency of 0.68 - which is
    an output of the measurement, not an input to it.
    """
    lam = C_M_S / H1_REST_FREQ_HZ
    return lam ** 2 / beam_solid_angle_sr(dish_m)


def flux_to_antenna_temperature(sfu, dish_m=DISH_M):
    """Antenna temperature (K) a point source of this flux density produces.

    T_A = S * A_e / 2k, in solar flux units of 1e-22 W/m^2/Hz. The factor of
    two is the single polarisation: an unpolarised source divides its power
    equally between the two, and this receiver keeps one. It is the same factor
    that is already inside the simulator's antenna temperatures, and adding it
    twice would halve every calibrated number.
    """
    return float(sfu) * 1e-22 * effective_area_m2(dish_m) / (2.0 * BOLTZMANN)


def antenna_temperature_to_flux(t_a_k, dish_m=DISH_M):
    """Solar flux units from an antenna temperature. The inverse of the above."""
    return float(t_a_k) * 2.0 * BOLTZMANN / (effective_area_m2(dish_m) * 1e-22)
