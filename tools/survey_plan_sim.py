#!/usr/bin/env python3
"""Plan an all-visible-sky survey: greedy simulation against the measured horizon and the mount's rates.

    python tools/survey_plan_sim.py BEAM_DEG GRID_DEG T_INT_S [SLEW_WEIGHT] [DAYS]

e.g. `5.16 2.58 60 1 4` is the Nyquist-sampled four-day programme of issue #37. Reads the active
horizon profile through horizon_store; prints coverage, integration and idle hours, homings and
mount travel. The chooser is the one the survey observation type should use: the patch that sets
soonest if any sets within the hour, otherwise the nearest in mount travel (weighted by SLEW_WEIGHT).
"""
import sys, math, numpy as np
sys.path.insert(0, '/home/astro/21-cm-radio-telescope-v2/receiver_scheduler'); sys.path.insert(0, '/home/astro/21-cm-radio-telescope-v2/astro_simulator')
import observatory, horizon_store as hs
LAT = math.radians(55.902426)
prof = hs.load_active()
floors = np.array([hs.horizon_floor(prof, az) for az in range(0, 360)])   # deg, per integer azimuth
BEAM = float(sys.argv[1]) if len(sys.argv) > 1 else 5.16
SPACING = float(sys.argv[2]) if len(sys.argv) > 2 else BEAM              # grid spacing (deg); BEAM = one beam per patch
T_INT = float(sys.argv[3]) if len(sys.argv) > 3 else 60.0                # s per patch
AZ_RATE, ALT_RATE, MOVE_OVERHEAD = 2.9, 3.9, 1.2                         # deg/s, deg/s, s (settle + polling)
HOME_EVERY_S, HOME_COST = 2 * 3600, 55.0
ALT_MAX, AZ_MIN, AZ_MAX = 88.0, 2.0, 353.0
SLEW_W = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
CLEAR = 2.6                                                              # half a beam above the measured floor
# equal-area-ish grid: dec bands of SPACING, RA spacing SPACING/cos(dec)
pix = []
for dec in np.arange(-34 + SPACING / 2, 90, SPACING):
    n = max(1, int(round(360 * math.cos(math.radians(dec)) / SPACING)))
    for i in range(n): pix.append((math.radians(360.0 * i / n), math.radians(dec)))
ra = np.array([p[0] for p in pix]); dec = np.array([p[1] for p in pix]); N = len(pix)
def altaz(lst):
    ha = lst - ra
    sa = np.sin(LAT) * np.sin(dec) + np.cos(LAT) * np.cos(dec) * np.cos(ha)
    alt = np.arcsin(np.clip(sa, -1, 1))
    az = np.arctan2(-np.sin(ha) * np.cos(dec), np.sin(dec) * np.cos(LAT) - np.cos(dec) * np.sin(LAT) * np.cos(ha))
    return np.degrees(alt), np.degrees(az) % 360
def visible(alt, az):
    return (alt > floors[np.clip(az.astype(int), 0, 359)] + CLEAR) & (alt <= ALT_MAX) & (az >= AZ_MIN) & (az <= AZ_MAX)
# how long each pixel stays visible from now: sample the next 12 h
def time_to_set(lst, idx):
    out = np.full(len(idx), 12 * 3600.0)
    for k, step in enumerate(np.arange(300, 12 * 3600 + 1, 300)):
        a, z = altaz(lst + step * 2 * math.pi / 86164.1)
        v = visible(a[idx], z[idx]); newly = (~v) & (out == 12 * 3600.0); out[newly] = step
    return out
ever = np.zeros(N, bool)
for s in np.arange(0, 86164.1, 600):
    a, z = altaz(s * 2 * math.pi / 86164.1); ever |= visible(a, z)
done = np.zeros(N, bool); t = 0.0; lst0 = 0.0
cur_alt, cur_az = 40.0, 180.0; travel_alt = travel_az = 0.0; n_home = 0; next_home = HOME_EVERY_S; n_obs = 0; idle = 0.0
DAYS = float(sys.argv[5]) if len(sys.argv) > 5 else 1.0
while t < 86400 * DAYS:
    if t >= next_home:
        t += HOME_COST; next_home += HOME_EVERY_S; n_home += 1; cur_alt, cur_az = 0.0, 0.0
    lst = lst0 + t * 2 * math.pi / 86164.1
    alt, az = altaz(lst); cand = np.where(visible(alt, az) & ~done)[0]
    if cand.size == 0:
        t += 60; idle += 60; continue
    tts = time_to_set(lst, cand)
    slew = np.maximum(np.abs(alt[cand] - cur_alt) / ALT_RATE, np.abs(az[cand] - cur_az) / AZ_RATE)
    # cost: urgency first (setting within the next hour dominates), then slew time
    cost = np.where(tts < 3600, tts / 3600.0, 1.0) * 100.0 + SLEW_W * slew
    j = cand[np.argmin(cost)]
    move = max(abs(alt[j] - cur_alt) / ALT_RATE, abs(az[j] - cur_az) / AZ_RATE) + MOVE_OVERHEAD
    travel_alt += abs(alt[j] - cur_alt); travel_az += abs(az[j] - cur_az)
    t += move + T_INT; cur_alt, cur_az = alt[j], az[j]; done[j] = True; n_obs += 1
print(f"beam {BEAM} deg, grid {SPACING} deg, {T_INT:.0f} s per patch: {N} patches on the grid, {ever.sum()} ever observable from here ({ever.sum()*SPACING*SPACING:.0f} sq deg)")
print(f"  observed in {DAYS:g} day(s): {done.sum()} of {ever.sum()} observable ({100*done.sum()/ever.sum():.1f}%); integrating {n_obs*T_INT/3600:.1f} h, idle {idle/3600:.1f} h, {n_home} homings")
print(f"  mount travel: alt {travel_alt:.0f} deg, az {travel_az:.0f} deg; mean move {(86400*DAYS-idle-n_obs*T_INT-n_home*HOME_COST)/max(n_obs,1):.1f} s per patch")
missed = ever & ~done
if missed.any(): print(f"  missed: dec range {np.degrees(dec[missed]).min():.0f}..{np.degrees(dec[missed]).max():.0f}, {missed.sum()} patches")
