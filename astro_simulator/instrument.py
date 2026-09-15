#!/usr/bin/env python3
"""Instrument constants for the Acre Road 3 m dish.

Kept out of astro_simulator.py so that web/make_web_data.py can read them
without importing matplotlib and astropy, and so the measured beam is written
down in exactly one place rather than once per consumer.
"""

import math

# Beam, measured rather than derived. The number that matters to every
# consumer here is the main-lobe SOLID ANGLE, because that is what the antenna
# theorem turns into a collecting area; the FWHM is its Gaussian equivalent,
# Omega = 1.133 FWHM^2, kept because the simulators convolve with a Gaussian.
#
# Measured 2026-09-15 (issue #35) from three hour-long drift scans of the Sun
# at B210 gains of 40, 30 and 20 dB, 352 x 10 s each, +-7.4 deg of drift so
# the baseline is seen on both sides. Integrating the crossing directly,
# 2 pi int P(theta) theta dtheta on each side, gives 23.7 sq deg at 30 dB and
# 23.8 at 20 dB - identical, so the receiver is linear there and that is the
# antenna: main lobe 23.7 sq deg, Gaussian-equivalent FWHM 4.57 deg (the
# whole-window Gaussian fits 4.56 +- 0.01). The crossing is not Gaussian: it
# has the flattened top of a focused aperture (residual against the Gaussian
# core -0.7%, 1-3 deg +0.7%, 3-5 deg -1.0% of peak), which is why a Gaussian
# fitted to it is window-dependent (5.13 deg over +-4.7 deg of the same data).
#
# This was 5.164 deg from 2026-08-22 to 2026-09-15: 18 solar rasters at 40 dB
# (5.173 deconvolved of the solar disc) and the 2026-08-27 Sun drift (5.16,
# but over a +-4.7 deg window whose fitted baseline sat below T_sys). Both
# were the Sun at 40 dB, where the receiver compresses its peak by ~5.7%
# (peak/baseline 5.12 at 40 dB against 5.41 at 30), and both were fitted over
# the core alone. The faint sources (Moon 4.0, Cas A 3.7, scallop 4.3 deg)
# were closer to the truth all along, and on the corrected solid angle the
# Sun reads 79 SFU against RSTN's 75 where the old one read 93.
#
# The Sun's own disc (0.53 deg) is not deconvolved: a uniform disc of that
# size widens a 4.56 deg Gaussian by 0.006 deg, below the fit's error.
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
# The measured beam corresponds to 1.134 lambda/D. Beam width scales as
# lambda/D and this simulator is effectively monochromatic at the HI line - a
# 2 MHz band is 0.14% in wavelength - so the reference is scaled by diameter
# alone.
#
# This is the *default*, not a fixed property: both front ends keep an
# editable beam box, so a user can ask for any width the loaded dataset can
# support (see DishSimulator.set_beam, which floors it at min_fwhm).
BEAM_FWHM_REF_DEG = 4.57
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

# White common-mode instability of band-integrated total power, as a fraction
# of system temperature per ~minute sample. Measured 2026-08-25 on a 99-record
# drift scan (60 s records, T_sys ~ 350 K): second differences of the
# band-mean - which cancel smooth sky drift - give 0.079 K per record,
# stationary across the run, against a thermal floor for the 618-channel mean
# of 0.023 K. So 3.4x thermal on the band integral, while the *per-channel*
# scatter is thermal to 6% (0.617 K measured, 0.581 K predicted): the wobble
# is a few parts in 1e4 moving every channel together, invisible per channel
# and dominant on the integral, where thermal divides by sqrt(N_chan) and
# common mode does not. Receiver gain and atmospheric emission fluctuation
# are indistinguishable here without a switched load; this constant is their
# sum, whatever the mixture.
#
# The simulator's drift-scan samples add it in quadrature:
#     sigma = sqrt( ((Tsys+T)/sqrt(npol B tau))^2  +  (GAIN_INSTABILITY (Tsys+T))^2 )
# independent of tau, since instability does not integrate down like the
# radiometer term; the fluctuation spectrum's slope is unmeasured, so at very
# different sample times this is an estimate. Band-integrated drift scans
# only - in a spectrum it is common mode and largely divides out with the
# bandpass.
#
# First recorded as 1.1e-3 the same day, from four first-differences on a
# freshly started receiver: that conflated the warm-up transient and real sky
# drifting through the beam with the instability, and overstated it 5x. Second
# differences on a settled run are the measurement; keep it honest the same
# way if remeasuring.
GAIN_INSTABILITY = 2.3e-4

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
    FWHM (pi/(4 ln 2)). The FWHM above is the Gaussian equivalent of the
    *directly integrated* main lobe (23.7 sq deg, three Sun drifts, 2026-09-15),
    so this returns the measured solid angle, not a model of one.
    """
    fwhm_rad = math.radians(beam_fwhm_deg(dish_m))
    return 1.133 * fwhm_rad ** 2


def effective_area_m2(dish_m=DISH_M):
    """Collecting area from the antenna theorem, A_e * Omega_A = lambda^2.

    Derived from the measured beam, never from an assumed aperture efficiency:
    that is the same argument that put MAIN_BEAM_EFFICIENCY at 1.0 rather than
    at a number chosen to make an answer come out. For the 3 m dish this gives
    6.18 m^2 against a physical 7.07, an aperture efficiency of 0.87 - which is
    an output of the measurement, not an input to it. It is an upper bound:
    Omega here is the main lobe out to ~7 deg, and the sidelobes and the ~10%
    of spillover measured by the horizon strip scan lie outside it, so the true
    A_e is smaller by the main-beam efficiency (of order 0.85-0.9). The 4.84 m^2
    this gave until 2026-09-15 came from a Gaussian fitted to the core of a
    compressed Sun (see BEAM_FWHM_REF_DEG).
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
