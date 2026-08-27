"""Where to park a drift scan so the source crosses beam centre exactly.

The mount parks on a grid: the Due rounds every target to the nearest encoder
pulse, 0.5 degrees in each drive axis (`executeDrive` in src/main.cpp), and the
controller converts the true alt/az it is sent into drive coordinates first
(`trueToDrive` in pointing.cpp: refraction, then the model terms). So "park
where the source will be at T" really parks up to a quarter of a degree from
it in each axis - which on the sky is a shift of the crossing time by up to a
couple of minutes, plus a cross-drift miss of up to about 0.35 degrees. Both
were invisible until the 2026-08-26 drift scans were compared with the planned
T, and both are avoidable.

A source's track through the sky is a curve; it never passes exactly through a
grid point, but among the grid points near the track one is closest to it. Park
there: the perpendicular distance from the track to that point is all that is
left of the pointing error, and the moment the track passes it is the crossing
time - known in advance, to the second, and recorded in the file, rather than
assumed to be the mid-point of the slot.

Doing that needs the controller's transform reproduced here, terms fetched from
its `/pointing` endpoint, since the grid lives in the drive frame and the
scheduler works in the sky frame. `true_to_drive` below is `trueToDrive` line
for line, and `refraction_deg` is shared with sun_scan, which already had to
match the controller's for the fit to be right. The parked position is then
sent to `/direct` as the *true* position of the chosen grid point, so the
controller's own transform lands on the point exactly and its rounding has
nothing to do. A mismatch between the copy here and the controller would show
up as the drive position reported after the slew differing from the one
chosen, which the scheduler checks and logs.
"""

import math
from datetime import timedelta

from sun_scan import refraction_deg

# Encoder quantum, degrees, both axes (PULSES_PER_DEGREE = 2 on the Due).
DRIVE_STEP_DEG = 0.5
# Mirrors of the controller's guards in pointing.cpp, so the copy here clamps
# where it clamps.
MAX_CORRECTION_DEG = 10.0
MAX_TAN_ALT_DEG = 89.0
TERM_NAMES = ('IE', 'IA', 'AN', 'AE', 'CA', 'NPAE', 'TF', 'AZSCALE')
# How far either side of the requested crossing time the track is searched,
# and how finely. A source at the equator moves 15 deg/h, so a quarter-degree
# grid miss is at most a minute; four minutes covers any declination the
# beam can usefully drift across, and 5 s steps put ~0.02 deg between
# samples, well inside the grid.
SEARCH_HALF_SPAN_S = 240.0
SEARCH_STEP_S = 5.0
REFINE_STEP_S = 0.25
# Misses this close count as equal when choosing between grid points: the
# refinement step above moves a fast source ~0.001 deg, so anything finer is
# search noise, not a better point.
TIE_DEG = 0.002
# Inverse transform: iterate until the forward of the answer is this close to
# the grid point, or give up after this many passes (the clamp on the model
# correction can make the fixed point cycle right at the zenith).
INVERSE_TOL_DEG = 1e-6
INVERSE_MAX_PASSES = 50


def _normalise_az(az):
    while az < 0.0:
        az += 360.0
    while az >= 360.0:
        az -= 360.0
    return az


def _clamp(d):
    if not math.isfinite(d):
        return 0.0
    return max(-MAX_CORRECTION_DEG, min(MAX_CORRECTION_DEG, d))


def model_correction(alt_deg, az_deg, terms):
    """(dAlt, dAz) to ADD to a true position to reach the drive frame.

    `modelCorrection` in pointing.cpp, evaluated at the position given. An
    empty or None `terms` is a controller with no model loaded: zero.
    """
    if not terms:
        return 0.0, 0.0
    t = {k: float(terms.get(k, 0.0) or 0.0) for k in TERM_NAMES}
    az = math.radians(az_deg)
    alt_for_tan = max(-MAX_TAN_ALT_DEG, min(MAX_TAN_ALT_DEG, alt_deg))
    alt = math.radians(alt_for_tan)
    sin_az, cos_az = math.sin(az), math.cos(az)
    tan_alt, cos_alt = math.tan(alt), math.cos(alt)
    sec_alt = (1.0 / cos_alt) if abs(cos_alt) > 1e-6 else 0.0
    d_alt = (t['IE'] + t['AN'] * cos_az + t['AE'] * sin_az
             - t['TF'] * math.cos(math.radians(alt_deg)))
    d_az = (t['IA'] + (t['AN'] * sin_az - t['AE'] * cos_az) * tan_alt
            + t['CA'] * sec_alt + t['NPAE'] * tan_alt + t['AZSCALE'] * az_deg)
    return _clamp(d_alt), _clamp(d_az)


def true_to_drive(true_alt, true_az, terms):
    """`trueToDrive`: refraction, then the model, both added."""
    apparent = true_alt + refraction_deg(true_alt)
    d_alt, d_az = model_correction(apparent, true_az, terms)
    return apparent + d_alt, _normalise_az(true_az + d_az)


def drive_to_true(drive_alt, drive_az, terms):
    """`driveToTrue`: the inverse, by fixed point on the forward transform.

    The controller stops after three passes, which is under a thousandth of
    a degree except close to the zenith, where the sec(alt) azimuth term is
    large (and clamped) and three passes can leave a tenth of a degree at
    alt 85. That is what its /status reports, and it is immaterial to it -
    but the true position returned here is what gets *sent*, and the
    controller's forward transform of it must land back on the grid point,
    so this one iterates until it does.
    """
    t_alt, t_az = drive_alt, drive_az
    for _ in range(INVERSE_MAX_PASSES):
        f_alt, f_az = true_to_drive(t_alt, t_az, terms)
        residual_az = drive_az - f_az
        while residual_az > 180.0:
            residual_az -= 360.0
        while residual_az < -180.0:
            residual_az += 360.0
        residual_alt = drive_alt - f_alt
        t_alt += residual_alt
        t_az += residual_az
        if abs(residual_alt) < INVERSE_TOL_DEG and abs(residual_az) < INVERSE_TOL_DEG:
            break
    return t_alt, _normalise_az(t_az)


def quantise(value):
    """Nearest grid point, rounding halves away from zero as C's round() does."""
    steps = math.floor(abs(value) / DRIVE_STEP_DEG + 0.5)
    return math.copysign(steps * DRIVE_STEP_DEG, value) if value else 0.0


def separation_deg(alt1, az1, alt2, az2):
    """Angular separation on the sky, degrees."""
    a1, a2 = math.radians(alt1), math.radians(alt2)
    daz = math.radians(az1 - az2)
    c = math.sin(a1) * math.sin(a2) + math.cos(a1) * math.cos(a2) * math.cos(daz)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def closest_approach(distance, t_ref, half_span_s=SEARCH_HALF_SPAN_S,
                     step_s=SEARCH_STEP_S, refine_s=REFINE_STEP_S):
    """The time nearest t_ref at which distance(t) is smallest, and that distance.

    A coarse pass over the whole span, then a fine pass either side of the
    coarse minimum. `distance` takes a datetime and returns degrees.
    """
    n = int(round(half_span_s / step_s))
    best_t, best_d = t_ref, distance(t_ref)
    for k in range(-n, n + 1):
        t = t_ref + timedelta(seconds=k * step_s)
        d = distance(t)
        # Strictly better, or as good and nearer the requested time.
        if d < best_d - 1e-9 or (abs(d - best_d) <= 1e-9
                                 and abs((t - t_ref).total_seconds())
                                 < abs((best_t - t_ref).total_seconds())):
            best_t, best_d = t, d
    coarse_t = best_t
    m = int(round(step_s / refine_s))
    for k in range(-m, m + 1):
        t = coarse_t + timedelta(seconds=k * refine_s)
        d = distance(t)
        if d < best_d - 1e-9:
            best_t, best_d = t, d
    return best_t, best_d


def choose_parking(track, t_ref, terms, half_span_s=SEARCH_HALF_SPAN_S,
                   step_s=SEARCH_STEP_S, reachable=None):
    """The drive grid point the source's track passes closest to near t_ref.

    `track(t)` gives the source's true (alt, az) at datetime t. `reachable`, if
    given, is a predicate `(drive_alt, drive_az) -> bool` that a grid point
    must satisfy - the mount limits and the azimuth dead zone - so a source
    that transits through the dead zone (Cas A culminates due north, in the
    355-360 deg gap) is parked at the closest point it can actually reach,
    slightly off exact transit, rather than at a point it cannot.

    Returns a dict (`drive_alt`/`drive_az`, `true_alt`/`true_az`, `crossing`,
    `offset_deg`), or None if no grid point near the track is reachable.
    """
    n = int(round(half_span_s / step_s))
    candidates = set()
    for k in range(-n, n + 1):
        alt, az = track(t_ref + timedelta(seconds=k * step_s))
        d_alt, d_az = true_to_drive(alt, az, terms)
        candidates.add((quantise(d_alt), quantise(d_az)))
    if reachable is not None:
        candidates = {c for c in candidates if reachable(*c)}
    if not candidates:
        return None

    found = []
    for g_alt, g_az in sorted(candidates):
        true_alt, true_az = drive_to_true(g_alt, g_az, terms)

        def distance(t, ta=true_alt, tz=true_az):
            alt, az = track(t)
            return separation_deg(alt, az, ta, tz)

        when, sep = closest_approach(distance, t_ref, half_span_s, step_s)
        found.append({'drive_alt': g_alt, 'drive_az': g_az,
                      'true_alt': true_alt, 'true_az': true_az,
                      'crossing': when, 'offset_deg': sep})
    # The smallest miss wins; a track running along a grid line passes several
    # points equally well (to the search resolution), and of those the one
    # nearest the requested time is wanted.
    least = min(f['offset_deg'] for f in found)
    ties = [f for f in found if f['offset_deg'] <= least + TIE_DEG]
    return min(ties, key=lambda f: abs((f['crossing'] - t_ref).total_seconds()))


def crossing_at(track, true_alt, true_az, t_ref, half_span_s=SEARCH_HALF_SPAN_S,
                step_s=SEARCH_STEP_S):
    """When the track passes nearest a given true direction, and how near.

    Model-free: used after the slew with the true position the controller
    reports for where it actually parked, so the recorded crossing does not
    depend on the copy of the model in this file agreeing with the controller.
    """
    def distance(t):
        alt, az = track(t)
        return separation_deg(alt, az, true_alt, true_az)
    return closest_approach(distance, t_ref, half_span_s, step_s)
