#!/usr/bin/env python3
"""Where the telescope is, for everything on the scheduler side.

The values themselves live in ``astro_simulator/instrument.py``, alongside the
measured beam, because both are surveyed constants of this observatory and that
file is deliberately free of astropy and matplotlib so anything can read it.
This module exists only so the path handling is written once rather than in
every consumer - it holds no numbers of its own, and must not acquire any.

The position had reached nine literals across six files before this, one of
them 3.6 km wrong. A constant repeated is a constant that will diverge.

Note what this is *not*: the runtime observer location the scheduler keeps in
its config is synced from the controller and is what the mount is actually
working to. These are the defaults it falls back on and the truth it should
agree with - if the two ever disagree, that is worth knowing rather than
papering over, so nothing here silently overwrites the configured value.
"""

import os
import sys

SIMULATOR_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "astro_simulator")

if SIMULATOR_DIR not in sys.path:
    sys.path.insert(0, SIMULATOR_DIR)

from instrument import (  # noqa: E402  - the path has to be set up first
    BEAM_FWHM_REF_DEG,
    DISH_M,
    MAIN_BEAM_EFFICIENCY,
    SITE_HEIGHT_M,
    SITE_LAT_DEG,
    SITE_LON_DEG,
    SITE_NAME,
    beam_fwhm_deg,
)

__all__ = ["MAIN_BEAM_EFFICIENCY", "SITE_NAME", "SITE_LAT_DEG", "SITE_LON_DEG", "SITE_HEIGHT_M",
           "DISH_M", "BEAM_FWHM_REF_DEG", "beam_fwhm_deg"]
