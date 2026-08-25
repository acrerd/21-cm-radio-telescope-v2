#!/usr/bin/env python3
"""Every horizon we have measured, kept by date, with one of them chosen.

The horizon is not a constant of the observatory. Trees grow through a season
and are cut back between them, so a profile measured in August is optimistic in
the following June and pessimistic in the June after a pruning. A single file
overwritten by each scan cannot express that: it silently replaces a horizon
that may still be the honest one, and once overwritten the old measurement is
gone even though nothing about it was wrong.

So scans accumulate here, named by when they were taken, and one of them is
*chosen* to be the one the rest of the system believes. The choice is a
separate act from the measurement, and deliberately so - a fresh scan is
archived but does not become active on its own. Trimming the trees makes the
horizon genuinely more open, and a scan that walks in and lowers the horizon
without anyone agreeing to it is exactly the failure this is built to avoid.
Opening the horizon should be a decision; keeping it closed is the safe default.

This module is deliberately stdlib-only - no numpy, no astropy, no requests -
because both the scheduler and the simulator read it, and it lives on the
simulator side for the same reason ``instrument.py`` does: that is the layer
everything else is allowed to depend on.

The archive itself sits under ``receiver_scheduler/horizon_profiles/``, next to
the scans it holds, with ``active.json`` naming the chosen one.
"""

import json
import math
import os
import shutil

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)

SCHEDULER_DIR = os.path.join(_REPO, "receiver_scheduler")
ARCHIVE_DIR = os.path.join(SCHEDULER_DIR, "horizon_profiles")
ACTIVE_FILE = os.path.join(ARCHIVE_DIR, "active.json")

# The single-file path the scan used to write, and which older readers still
# open. It is now kept as a mirror of whichever profile is active, so nothing
# that reads it has to know the archive exists.
LEGACY_FILE = os.path.join(SCHEDULER_DIR, "horizon_profile.json")


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

def profile_stamp(profile: dict) -> str:
    """The compact UTC stamp a profile is filed under, from when it started.

    Keyed on the *start* of the scan rather than the end, because that is what
    identifies the observing session, and a scan that was interrupted still has
    one. Two scans in a night are distinct; two saves of the same scan - the
    partials, the final write - are the same file, which is what we want.
    """
    started = str(profile.get("started_utc") or "")
    keep = [c for c in started if c.isdigit()]
    if len(keep) < 14:
        return "unknown"
    return "%sT%sZ" % ("".join(keep[:8]), "".join(keep[8:14]))


def profile_name(profile: dict) -> str:
    return "horizon_%s" % profile_stamp(profile)


def profile_date(profile: dict) -> str:
    """The scan's date as YYYY-MM-DD, for showing a human a list."""
    stamp = profile_stamp(profile)
    if stamp == "unknown":
        return "unknown"
    return "%s-%s-%s" % (stamp[0:4], stamp[4:6], stamp[6:8])


def _path_for(name: str) -> str:
    """Resolve a profile name to a path, refusing anything with a separator.

    The name arrives from an HTTP request, so it is not allowed to address
    anything outside the archive.
    """
    base = os.path.basename(str(name))
    if not base.endswith(".json"):
        base += ".json"
    if base in (".json", os.path.basename(ACTIVE_FILE)):
        raise ValueError("not a profile name: %r" % (name,))
    return os.path.join(ARCHIVE_DIR, base)


# ---------------------------------------------------------------------------
# Reading and writing
# ---------------------------------------------------------------------------

def _read(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write(path: str, payload: dict) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return path


def archive_profile(profile: dict, note: str = "") -> str:
    """File a profile under its date. Does not make it active."""
    path = _path_for(profile_name(profile))
    if note:
        profile = dict(profile, note=note)
    return _write(path, profile)


def load_profile(name: str):
    return _read(_path_for(name))


HEMISPHERE_SQ_DEG = 2.0 * math.pi * (180.0 / math.pi) ** 2   # 20626.5


def visible_sky_sq_deg(profile: dict, step_deg: float = 0.25) -> float:
    """How much sky this horizon leaves, in square degrees.

    The honest answer to "how much does this horizon cost me", and not the same
    question as the median floor. Solid angle goes as cos(alt), so azimuth
    cells are widest at the horizon and a tall obstruction costs far more sky
    than its share of the azimuth count suggests: on the 2026-08-24 profile
    three azimuths blocked to 45 deg cost more than ten azimuths open to 5 deg
    return.

    Integrates sin(floor) over azimuth through `horizon_floor`, so the number
    describes the same piecewise rule that decides whether a target is blocked,
    rather than a smoothed version of it.

    Note what the scan floor does to this: altitudes below the lowest strip
    were never measured, so an azimuth recorded as clear at 5 deg is counted as
    blocked below 5 deg. The figure is therefore a lower bound on visible sky,
    which is the safe direction.
    """
    floors = profile_floors(profile)
    if not floors:
        return HEMISPHERE_SQ_DEG
    n = max(1, int(round(360.0 / float(step_deg))))
    total = 0.0
    for i in range(n):
        alt = horizon_floor(profile, (i + 0.5) * 360.0 / n)
        total += 1.0 - math.sin(math.radians(alt))
    return (total / n) * HEMISPHERE_SQ_DEG


def floors_summary(profile: dict) -> dict:
    """The one-line character of a scan: how open is this horizon?

    Two scans of the same sky are compared by these numbers, and that is the
    whole point of keeping both - a pruning should show up as visible sky
    gained, and a scan that disagrees with its neighbours for any other reason
    is worth looking at before it is trusted.
    """
    alts = sorted(alt for _, alt in profile_floors(profile))
    if not alts:
        return {"n": 0, "median_deg": None, "min_deg": None, "max_deg": None,
                "visible_sq_deg": None, "visible_fraction": None}
    mid = len(alts) // 2
    median = alts[mid] if len(alts) % 2 else 0.5 * (alts[mid - 1] + alts[mid])
    visible = visible_sky_sq_deg(profile)
    return {"n": len(alts), "median_deg": round(median, 2),
            "min_deg": round(alts[0], 2), "max_deg": round(alts[-1], 2),
            "visible_sq_deg": round(visible),
            "visible_fraction": round(visible / HEMISPHERE_SQ_DEG, 4)}


def summarise(profile: dict, name: str = "") -> dict:
    """What a chooser needs to tell one scan from another."""
    summary = {
        "name": name or profile_name(profile),
        "date": profile_date(profile),
        "started_utc": profile.get("started_utc"),
        "duration_s": profile.get("duration_s"),
        "n_azimuths": profile.get("n_azimuths"),
        "az_step_deg": profile.get("az_step_deg"),
        "alt_step_deg": profile.get("alt_step_deg"),
        "alt_min_deg": profile.get("alt_min_deg"),
        "alt_max_deg": profile.get("alt_max_deg"),
        "complete": profile.get("complete"),
        "sdr_type": profile.get("sdr_type"),
        "record_version": profile.get("record_version"),
        "note": profile.get("note") or "",
    }
    summary["floors"] = floors_summary(profile)
    # A demo scan is synthetic. It must never be mistaken for a measurement of
    # the actual sky, so it is labelled everywhere it can be chosen.
    summary["is_demo"] = (profile.get("sdr_type") == "demo")
    return summary


def _migrate_legacy() -> None:
    """Bring the pre-archive single file in, once, so nothing is stranded."""
    profile = _read(LEGACY_FILE)
    if not profile:
        return
    name = profile_name(profile)
    if name == "horizon_unknown":
        return
    path = _path_for(name)
    if not os.path.exists(path):
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        shutil.copy2(LEGACY_FILE, path)


def list_profiles() -> list:
    """Every archived scan, newest first, with the active one flagged."""
    _migrate_legacy()
    active = active_name()
    out = []
    try:
        names = sorted(os.listdir(ARCHIVE_DIR))
    except OSError:
        return out
    for filename in names:
        if not filename.endswith(".json") or filename == os.path.basename(ACTIVE_FILE):
            continue
        profile = _read(os.path.join(ARCHIVE_DIR, filename))
        if not profile:
            continue
        summary = summarise(profile, name=filename[:-len(".json")])
        summary["active"] = (summary["name"] == active)
        out.append(summary)
    out.sort(key=lambda s: str(s.get("started_utc") or ""), reverse=True)
    return out


# ---------------------------------------------------------------------------
# The chosen one
# ---------------------------------------------------------------------------

def active_name():
    """The chosen profile's name, or None if nobody has chosen."""
    record = _read(ACTIVE_FILE) or {}
    name = record.get("active")
    if name and os.path.exists(_path_for(name)):
        return name
    return None


def active_record() -> dict:
    return _read(ACTIVE_FILE) or {}


def set_active(name: str, note: str = "") -> dict:
    """Choose the profile the rest of the system believes.

    Also refreshes the legacy single-file path, so every existing reader of
    ``horizon_profile.json`` follows the choice without knowing about it.
    """
    profile = load_profile(name)
    if profile is None:
        raise FileNotFoundError("no archived horizon profile named %r" % (name,))
    record = {"active": os.path.basename(str(name)).replace(".json", ""),
              "note": note,
              "profile_started_utc": profile.get("started_utc")}
    _write(ACTIVE_FILE, record)
    _write(LEGACY_FILE, profile)
    return record


def clear_active() -> None:
    try:
        os.remove(ACTIVE_FILE)
    except OSError:
        pass


def load_active():
    """The profile in force, or None.

    With nothing chosen this falls back to the most recent *complete* scan, so
    a fresh installation behaves sensibly - but the fallback is only ever a
    starting point. Recency is not the same as correctness, which is why the
    choice exists at all.
    """
    name = active_name()
    if name:
        return load_profile(name)
    candidates = [s for s in list_profiles()
                  if s.get("complete") and not s.get("is_demo")]
    if not candidates:
        return _read(LEGACY_FILE)
    return load_profile(candidates[0]["name"])


# ---------------------------------------------------------------------------
# Using a profile
# ---------------------------------------------------------------------------

def profile_floors(profile: dict) -> list:
    """(azimuth, clearance altitude) pairs, sorted, for the usable entries."""
    floors = []
    for entry in (profile or {}).get("entries", []):
        fit = entry.get("fit") or {}
        if fit.get("success") and fit.get("alt_clear") is not None:
            floors.append((float(entry["az_deg"]) % 360.0,
                           float(fit["alt_clear"])))
    floors.sort()
    return floors


def horizon_floor(profile: dict, az_deg: float, margin_deg: float = 0.0) -> float:
    """Lowest clean altitude at this azimuth.

    Takes the higher of the two bracketing samples rather than interpolating.
    An obstruction narrower than the sampling is more likely to be missed than
    double-counted, so between two measured azimuths the safe assumption is the
    worse of them. This is also what makes the drawn horizon a castellation
    rather than a smooth curve: the rule really is piecewise, and drawing it as
    a smooth line would claim a precision between samples that we do not have.
    """
    floors = profile_floors(profile)
    if not floors:
        return 0.0
    az = float(az_deg) % 360.0
    azs = [a for a, _ in floors]
    before = max((i for i, a in enumerate(azs) if a <= az), default=len(azs) - 1)
    after = min((i for i, a in enumerate(azs) if a >= az), default=0)
    return max(floors[before][1], floors[after][1]) + margin_deg


def is_obstructed(profile: dict, alt_deg: float, az_deg: float,
                  margin_deg: float = 0.0) -> bool:
    """Is this true-frame sky position inside the obstructed horizon?"""
    return float(alt_deg) < horizon_floor(profile, az_deg, margin_deg)


def horizon_castellation(profile: dict, step_deg: float = 1.0,
                         margin_deg: float = 0.0):
    """The measured horizon as a dense (az, alt) outline, ready to draw.

    Sampled through ``horizon_floor`` rather than by joining the measured
    points, so the drawn line is exactly the rule the rest of the system
    applies - flat across each azimuth's cell and stepping at the boundary. A
    reader looking at the plot is then looking at the thing that will actually
    decide whether a target is blocked.
    """
    floors = profile_floors(profile)
    if not floors:
        return [], []
    n = max(2, int(round(360.0 / float(step_deg))))
    az = [i * 360.0 / n for i in range(n + 1)]
    return az, [horizon_floor(profile, a, margin_deg) for a in az]
