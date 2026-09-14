"""The professional solar radio flux, for quoting beside our own.

NOAA SWPC publishes the USAF Radio Solar Telescope Network's daily local-noon
fluxes at 245-15400 MHz (Learmonth, San Vito, Sagamore Hill, Palehua) and the
Penticton 2800 MHz values, as one plain-text file updated hourly:

    https://services.swpc.noaa.gov/text/solar_radio_flux.txt

The 1415 MHz row is the closest professional measurement to this dish's
1420 MHz, and the number the flux monitor should be read against: the
stations disagree by 10-20 SFU on any given day, so agreement to within that
is agreement.

Stdlib only. Never raises to a caller: a failed fetch keeps whatever was last
read, and a reader that finds nothing gets None. The file holds seven days,
so each successful fetch is merged into a history on disk - a recording from
last month can still be captioned with the value from its own day.
"""
import json
import logging
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("scheduler")

NOAA_URL = "https://services.swpc.noaa.gov/text/solar_radio_flux.txt"
FREQ_MHZ = 1415                      # the row quoted
F107_MHZ = 2800                      # Penticton's 10.7 cm index, quoted alongside
REFRESH_S = 3600                     # the file is updated once an hour
FETCH_TIMEOUT_S = 10

_HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(_HERE, "data", "solar_reference_history.json")

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def parse(text):
    """{'issued': str, 'days': {'YYYY-MM-DD': {freq_mhz: {station: value}}}}.

    Station names come from the two header lines: a name and the UTC of its
    local noon, joined ("Penticton 1700"), because Penticton appears three
    times. Missing values (-1) are left out rather than stored.
    """
    issued = None
    stations = []
    days = {}
    day = None
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith(":Issued:"):
            issued = line.split(":", 2)[2].strip()
            continue
        if line.startswith("Freq") and i + 1 < len(lines):
            names = re.split(r"\s{2,}", line)[1:]
            times = re.findall(r"\b(\d{4}) UTC\b", lines[i + 1])
            stations = [f"{n} {t}" if names.count(n) > 1 else n
                        for n, t in zip(names, times)]
            continue
        m = re.match(r"^(\d{4}) ([A-Z][a-z]{2}) (\d{1,2})$", line)
        if m:
            day = "%s-%02d-%02d" % (m.group(1), _MONTHS.get(m.group(2), 0), int(m.group(3)))
            days.setdefault(day, {})
            continue
        if day and stations and re.match(r"^\d+(\s+-?\d+)+$", line):
            parts = line.split()
            freq = int(parts[0])
            row = {}
            for station, value in zip(stations, parts[1:]):
                v = int(value)
                if v >= 0:
                    row[station] = v
            if row:
                days[day][freq] = row
    return {"issued": issued, "days": days}


def fetch(url=NOAA_URL, timeout=FETCH_TIMEOUT_S):
    """The raw file, or None. Network errors are logged at debug level: an
    observatory without a route out is a normal condition, not a fault."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "acreroad-srt/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("ascii", "replace")
    except Exception as exc:                          # noqa: BLE001
        log.debug("solar reference: fetch of %s failed: %s", url, exc)
        return None


def _load_history(path=None):
    path = HISTORY_PATH if path is None else path        # resolved at call time, so tests can move it
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_history(history, path=None):
    path = HISTORY_PATH if path is None else path
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(history, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("solar reference: could not save %s: %s", path, exc)


def merge_into_history(parsed, path=None):
    """Fold a parsed file into the on-disk history and return the history.
    Keys are dates; each holds {freq: {station: value}} and the issue stamp.
    A later fetch of the same day replaces the day - the file carries the
    latest readings, and a station's noon value can arrive hours late."""
    history = _load_history(path)
    for day, rows in (parsed.get("days") or {}).items():
        if not rows:
            continue
        history[day] = {"issued": parsed.get("issued"),
                        "flux": {str(f): v for f, v in rows.items()}}
    _save_history(history, path)
    return history


# ---------------------------------------------------------------------------
# The cache the scheduler reads
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_state = {"fetched_at": 0.0, "refreshing": False, "history": None}


def _refresh(url):
    text = fetch(url)
    with _lock:
        _state["refreshing"] = False
        if text is None:
            return
        try:
            history = merge_into_history(parse(text))
        except Exception as exc:                      # noqa: BLE001
            log.warning("solar reference: could not parse the NOAA file: %s", exc)
            return
        _state["history"] = history
        _state["fetched_at"] = time.time()


def history(max_age_s=REFRESH_S, url=NOAA_URL, blocking=False):
    """The history, refreshing it in the background when stale.

    Called from request handlers, so it never waits on the network: a stale
    cache starts one fetch thread and returns what is on disk meanwhile.
    `blocking=True` is for tests and one-off tools.
    """
    with _lock:
        if _state["history"] is None:
            _state["history"] = _load_history()
        stale = time.time() - _state["fetched_at"] > max_age_s
        start = stale and not _state["refreshing"]
        if start:
            _state["refreshing"] = True
    if start:
        if blocking:
            _refresh(url)
        else:
            threading.Thread(target=_refresh, args=(url,), daemon=True,
                             name="solar-reference").start()
    with _lock:
        return dict(_state["history"] or {})


def reset_cache():
    with _lock:
        _state.update(fetched_at=0.0, refreshing=False, history=None)


# ---------------------------------------------------------------------------
# What gets quoted
# ---------------------------------------------------------------------------

def values_for(day, hist=None, freq=FREQ_MHZ):
    """{station: value} at `freq` on `day` (YYYY-MM-DD), or {}."""
    hist = history() if hist is None else hist
    entry = hist.get(day) or {}
    return dict((entry.get("flux") or {}).get(str(freq)) or {})


def nearest_day(day, hist):
    """The latest day in the history at or before `day`, or None."""
    candidates = [d for d in hist if d <= day and (hist[d].get("flux") or {}).get(str(FREQ_MHZ))]
    return max(candidates) if candidates else None


def summary_for(when, hist=None):
    """One caption line for an observation at `when` (epoch seconds, a
    datetime, or a YYYY-MM-DD string), or None when nothing is known.

    e.g. "RSTN 1415 MHz local-noon flux, 13 Sep: San Vito 85, Palehua 73 SFU
    · F10.7 114 (NOAA SWPC)". A day with no reading yet - this morning's, or
    a weekend gap - falls back to the latest earlier day and says which.
    """
    if isinstance(when, (int, float)):
        day = datetime.fromtimestamp(float(when), tz=timezone.utc).strftime("%Y-%m-%d")
    elif isinstance(when, datetime):
        day = when.astimezone(timezone.utc).strftime("%Y-%m-%d") if when.tzinfo else when.strftime("%Y-%m-%d")
    else:
        day = str(when)[:10]
    hist = history() if hist is None else hist
    use = nearest_day(day, hist)
    if use is None:
        return None
    flux = values_for(use, hist)
    if not flux:
        return None
    stamp = datetime.strptime(use, "%Y-%m-%d").strftime("%-d %b")
    if use != day:
        stamp += " (latest available)"
    parts = ", ".join(f"{station} {value}" for station, value in flux.items())
    text = f"RSTN {FREQ_MHZ} MHz local-noon flux, {stamp}: {parts} SFU"
    f107 = values_for(use, hist, F107_MHZ)
    if f107:
        # Penticton observes three times a day; the 2000 UTC reading is the
        # one published as the day's F10.7 (it matches SWPC's daily index).
        noon = [v for k, v in f107.items() if "2000" in k]
        text += " · F10.7 %d" % (noon[0] if noon else round(sum(f107.values()) / len(f107)))
    return text + " (NOAA SWPC)"


if __name__ == "__main__":                            # pragma: no cover
    h = history(blocking=True)
    print(summary_for(time.time(), h))
