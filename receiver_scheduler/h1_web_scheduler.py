#!/usr/bin/env python3
"""
Web-based scheduler interface for H1 Receiver.
Provides an interactive HTML/JavaScript UI for managing observation schedules.

Run with: python h1_web_scheduler.py
Then open: http://localhost:5000
"""

import json
import os
import subprocess
import sys
import signal
import threading
import time
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import Optional
from pathlib import Path

from flask import Flask, render_template_string, jsonify, request, send_from_directory
import logging
import logging.handlers
import math
import urllib.request
import urllib.error
import urllib.parse

try:
    import ephem
    EPHEM_AVAILABLE = True
except ImportError:
    EPHEM_AVAILABLE = False

# Logging setup - logs to both console and rotating file
LOG_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(LOG_DIR, "scheduler.log")

log = logging.getLogger("scheduler")
log.setLevel(logging.DEBUG)

_fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(_fmt)
log.addHandler(_console)

_file = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3)
_file.setLevel(logging.DEBUG)
_file.setFormatter(_fmt)
log.addHandler(_file)

# Suppress noisy Flask/werkzeug request logs
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# Configuration
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEDULE_FILE = "h1_schedule.json"
CONFIG_FILE = os.path.join(_SCRIPT_DIR, "scheduler_config.json")
RECEIVER_SCRIPT = os.path.join(_SCRIPT_DIR, "b210_h1_receiver.py")

# Default configuration - overridden by scheduler_config.json if present
_DEFAULT_CONFIG = {
    "banner_name": "H1 Receiver Scheduler",
    "banner_subtitle": "Hydrogen Line (21cm) Observation Manager",
    "srt_controller_url": "http://192.168.0.149",
    "slew_timeout": 300,
    "position_tolerance": 0.5,
    "python_path": "",
    "data_output_folder": os.path.join(_SCRIPT_DIR, "data"),
    "log_lines": 100,
    "sound_enabled": True,
    "observer_lat": 55.902444,
    "observer_lon": -4.307861,
    "observer_elevation": 50,
    "min_elevation": 10.0,
}


def load_config() -> dict:
    """Load config from JSON file, merged with defaults."""
    cfg = dict(_DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                cfg.update(json.load(f))
        except Exception as e:
            log.error("Error loading config: %s", e)
    return cfg


def save_config(cfg: dict):
    """Save config to JSON file."""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)
    log.info("Configuration saved")


def get_config_value(key: str):
    """Get a single config value."""
    return load_config().get(key, _DEFAULT_CONFIG.get(key))


# Load initial config
_config = load_config()
SRT_CONTROLLER_URL = _config["srt_controller_url"] or None
SRT_SLEW_TIMEOUT = _config["slew_timeout"]
SRT_POSITION_TOLERANCE = _config["position_tolerance"]
PYTHON_PATH = _config["python_path"] or None

app = Flask(__name__)


# =============================================================================
# SRT Telescope Controller Integration
# =============================================================================

def dms_to_decimal(deg: int, min: int, sec: float, is_ra: bool = False) -> float:
    """Convert degrees/minutes/seconds to decimal.

    For RA (is_ra=True): input is hours/minutes/seconds, output is decimal hours.
    For Dec/Alt/Az/Galactic: input and output are degrees.
    """
    sign = -1 if deg < 0 else 1
    decimal = abs(deg) + min / 60.0 + sec / 3600.0
    return sign * decimal


def srt_api_call(endpoint: str, params: Optional[dict] = None) -> Optional[dict]:
    """Make an API call to the SRT controller.

    Returns JSON response as dict, or None on error.
    """
    if not SRT_CONTROLLER_URL:
        return None

    url = f"{SRT_CONTROLLER_URL}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.loads(response.read().decode())
    except urllib.error.URLError as e:
        log.warning("SRT connection error: %s", e)
        return None
    except Exception as e:
        log.error("SRT API error: %s", e)
        return None


def srt_get_settings() -> Optional[dict]:
    """Get controller settings (observer location, home position, etc)."""
    return srt_api_call("/settings")


def sync_observer_from_controller():
    """Fetch observer location from the SRT controller and update config."""
    if not SRT_CONTROLLER_URL:
        return
    settings = srt_get_settings()
    if not settings:
        return
    lat = settings.get('observer_lat')
    lon = settings.get('observer_lon')
    if lat is not None and lon is not None:
        cfg = load_config()
        if cfg.get('observer_lat') != lat or cfg.get('observer_lon') != lon:
            cfg['observer_lat'] = lat
            cfg['observer_lon'] = lon
            save_config(cfg)
            log.info("Observer location synced from controller: lat=%.6f lon=%.6f", lat, lon)


def srt_get_status() -> Optional[dict]:
    """Get current telescope status (position, tracking, etc)."""
    return srt_api_call("/status")


def srt_get_tracking() -> Optional[dict]:
    """Get current tracking state."""
    return srt_api_call("/tracking")


def srt_point_telescope(obs: dict) -> bool:
    """Command the telescope to point at the observation's target coordinates.

    Converts DMS coordinates to decimal and calls the appropriate ESP32 endpoint
    based on the coordinate system.

    Returns True if command was sent successfully.
    """
    if not SRT_CONTROLLER_URL:
        log.info("SRT controller URL not configured - skipping telescope control")
        return True  # Don't block observation if telescope control disabled

    coord_system = obs.get('coord_system', 'altaz')

    # Convert DMS to decimal
    coord1 = dms_to_decimal(
        obs.get('coord1_deg', 0),
        obs.get('coord1_min', 0),
        obs.get('coord1_sec', 0.0),
        is_ra=(coord_system == 'radec')
    )
    coord2 = dms_to_decimal(
        obs.get('coord2_deg', 0),
        obs.get('coord2_min', 0),
        obs.get('coord2_sec', 0.0),
        is_ra=False
    )

    # Call appropriate endpoint based on coordinate system
    if coord_system == 'altaz':
        # Alt/Az: use direct control (no tracking needed for fixed position)
        endpoint = "/direct"
        params = {"alt": coord1, "az": coord2}
        log.info("SRT commanding telescope to Alt=%.2f° Az=%.2f°", coord1, coord2)

    elif coord_system == 'radec':
        # RA/Dec: use tracking mode (follows as Earth rotates)
        endpoint = "/track/radec"
        params = {"ra": coord1, "dec": coord2}
        log.info("SRT commanding telescope to track RA=%.3fh Dec=%.2f°", coord1, coord2)

    elif coord_system == 'galactic':
        # Galactic: use tracking mode
        endpoint = "/track/galactic"
        params = {"l": coord1, "b": coord2}
        log.info("SRT commanding telescope to track Gal l=%.2f° b=%.2f°", coord1, coord2)

    elif coord_system == 'object':
        # Named solar system object: use dedicated endpoint
        object_name = obs.get('object_name', '').lower()
        if object_name == 'sun':
            endpoint = "/track/sun"
            params = {}
            log.info("SRT commanding telescope to track the Sun")
        elif object_name == 'moon':
            endpoint = "/track/moon"
            params = {}
            log.info("SRT commanding telescope to track the Moon")
        else:
            log.error("SRT unknown object: %s", object_name)
            return False

    elif coord_system == 'satellite':
        # Satellite: tracking thread handles continuous updates
        log.info("SRT satellite mode - tracking thread will send updates")
        return True

    else:
        log.error("SRT unknown coordinate system: %s", coord_system)
        return False

    result = srt_api_call(endpoint, params)
    if result and result.get('ok'):
        log.info("SRT command accepted")
        return True
    else:
        log.error("SRT command failed: %s", result)
        return False


def srt_wait_for_slew(timeout: Optional[int] = None) -> bool:
    """Wait for telescope to finish slewing.

    Polls /status until is_slewing is false, or timeout is reached.
    The is_slewing field is set by the mount when status contains 'Slewing'
    or a ' -> ' target indicator.

    Returns True if slew complete, False if timeout or error.
    """
    if not SRT_CONTROLLER_URL:
        return True

    timeout = timeout or SRT_SLEW_TIMEOUT
    start_time = time.time()

    log.info("SRT waiting for slew to complete...")

    # Brief delay to allow slew to begin before we start polling
    time.sleep(2)

    while time.time() - start_time < timeout:
        status = srt_get_status()
        if status:
            is_slewing = status.get('is_slewing', False)
            current_alt = status.get('alt', 0)
            current_az = status.get('az', 0)

            if not is_slewing:
                elapsed = time.time() - start_time
                log.info("SRT slew complete in %.1fs - at Alt=%.2f° Az=%.2f°", elapsed, current_alt, current_az)
                return True

        time.sleep(2)  # Poll every 2 seconds

    log.warning("SRT timeout waiting for slew to complete")
    return False


def srt_set_calibrator(on: bool) -> bool:
    """Turn the calibrator noise source on or off."""
    if not SRT_CONTROLLER_URL:
        return True
    result = srt_api_call("/calibrator", {"on": "1" if on else "0"})
    if result and result.get('ok'):
        log.info("Calibrator %s", "ON" if on else "OFF")
        return True
    else:
        log.error("Failed to set calibrator: %s", result)
        return False


def srt_go_position(name: str, alt: float, az: float) -> bool:
    """Send telescope to a named position."""
    if not SRT_CONTROLLER_URL:
        return True
    result = srt_api_call("/direct", {"alt": alt, "az": az})
    if result and result.get('ok'):
        log.info("Telescope going to %s (Alt=%.1f° Az=%.1f°)", name, alt, az)
        return True
    else:
        log.error("Failed to send telescope to %s: %s", name, result)
        return False


def srt_stop_tracking() -> bool:
    """Stop telescope tracking."""
    if not SRT_CONTROLLER_URL:
        return True

    result = srt_api_call("/track", {"enable": "0"})
    return bool(result and result.get('ok', False))


# =============================================================================
# Satellite Tracking (TLE)
# =============================================================================

def _get_observer() -> 'ephem.Observer':
    """Create a PyEphem Observer from config."""
    obs = ephem.Observer()
    obs.lat = str(get_config_value("observer_lat"))
    obs.lon = str(get_config_value("observer_lon"))
    obs.elevation = get_config_value("observer_elevation")
    return obs


def parse_tle(tle_text: str) -> tuple:
    """Parse TLE text into (name, line1, line2).

    Accepts 2-line or 3-line format (with or without name line).
    """
    lines = [l.strip() for l in tle_text.strip().splitlines() if l.strip()]
    if len(lines) == 3:
        return lines[0], lines[1], lines[2]
    elif len(lines) == 2:
        return "SATELLITE", lines[0], lines[1]
    else:
        raise ValueError(f"Expected 2 or 3 TLE lines, got {len(lines)}")


def predict_next_pass(tle_text: str) -> Optional[dict]:
    """Predict the next pass of a satellite above minimum elevation.

    Returns dict with rise/set times, max elevation, duration, etc.
    or None if no pass found.
    """
    if not EPHEM_AVAILABLE:
        return None

    name, line1, line2 = parse_tle(tle_text)
    sat = ephem.readtle(name, line1, line2)
    obs = _get_observer()
    min_el = get_config_value("min_elevation")

    # Search up to 24 hours ahead
    obs.date = ephem.now()
    for _ in range(48):  # up to 48 attempts to find a good pass
        try:
            rise_time, rise_az, max_time, max_el, set_time, set_az = obs.next_pass(sat)
        except Exception as e:
            log.error("Pass prediction error: %s", e)
            return None

        max_el_deg = math.degrees(max_el)
        if max_el_deg >= min_el:
            rise_dt = ephem.Date(rise_time).datetime()
            set_dt = ephem.Date(set_time).datetime()
            max_dt = ephem.Date(max_time).datetime()
            duration_min = (set_dt - rise_dt).total_seconds() / 60

            return {
                "name": name,
                "rise_time_utc": rise_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "rise_time_local": ephem.localtime(rise_time).strftime("%Y-%m-%d %H:%M:%S"),
                "rise_az": round(math.degrees(rise_az), 1),
                "max_time_utc": max_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "max_el": round(max_el_deg, 1),
                "set_time_utc": set_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "set_time_local": ephem.localtime(set_time).strftime("%Y-%m-%d %H:%M:%S"),
                "set_az": round(math.degrees(set_az), 1),
                "duration_minutes": round(duration_min, 1),
                "start_date": ephem.localtime(rise_time).strftime("%Y-%m-%d"),
                "start_time": ephem.localtime(rise_time).strftime("%H:%M"),
            }

        # Skip past this pass and try the next one
        obs.date = set_time + ephem.minute

    return None


# Satellite tracking thread
_sat_tracking_stop = threading.Event()
_sat_tracking_thread: Optional[threading.Thread] = None


def _satellite_tracking_loop(tle_text: str):
    """Background loop that sends alt/az updates every second for a satellite."""
    try:
        name, line1, line2 = parse_tle(tle_text)
        sat = ephem.readtle(name, line1, line2)
        obs = _get_observer()

        log.info("Satellite tracking started: %s", name)

        while not _sat_tracking_stop.is_set():
            obs.date = ephem.now()
            sat.compute(obs)
            alt_deg = math.degrees(sat.alt)
            az_deg = math.degrees(sat.az)

            if alt_deg > 0:
                srt_api_call("/direct", {"alt": round(alt_deg, 2), "az": round(az_deg, 2)})

            _sat_tracking_stop.wait(1.0)

        log.info("Satellite tracking stopped: %s", name)
    except Exception as e:
        log.error("Satellite tracking error: %s", e, exc_info=True)


def start_satellite_tracking(tle_text: str):
    """Start the satellite tracking background thread."""
    global _sat_tracking_thread
    stop_satellite_tracking()
    _sat_tracking_stop.clear()
    _sat_tracking_thread = threading.Thread(
        target=_satellite_tracking_loop, args=(tle_text,), daemon=True
    )
    _sat_tracking_thread.start()


def stop_satellite_tracking():
    """Stop the satellite tracking background thread."""
    global _sat_tracking_thread
    if _sat_tracking_thread and _sat_tracking_thread.is_alive():
        _sat_tracking_stop.set()
        _sat_tracking_thread.join(timeout=5)
    _sat_tracking_thread = None


# Current running observation
current_process: Optional[subprocess.Popen] = None
current_observation: Optional[dict] = None
observation_end_time: Optional[datetime] = None
process_lock = threading.Lock()
scheduler_running = True

# Sun scan state
sun_scan_thread: Optional[threading.Thread] = None
sun_scan_cancel = threading.Event()
sun_scan_state: dict = {
    "running": False,
    "progress": 0,
    "total": 0,
    "point_info": None,
    "result": None,
    "error": None,
    "image_path": None,
}

# Calibration day state
cal_day_thread: Optional[threading.Thread] = None
cal_day_cancel = threading.Event()
cal_day_state: dict = {
    "running": False,
    "scans_completed": 0,
    "next_scan_time": None,
    "interval_minutes": 30,
    "error": None,
}


# Default observation template
DEFAULT_OBSERVATION = {
    "name": "New Observation",
    "coord_system": "altaz",  # altaz, radec, galactic
    "coord1_deg": 45,         # Alt, RA (as degrees), or Gal Lon
    "coord1_min": 0,
    "coord1_sec": 0.0,
    "coord2_deg": 180,        # Az, Dec, or Gal Lat
    "coord2_min": 0,
    "coord2_sec": 0.0,
    "start_date": "",         # YYYY-MM-DD, empty = today
    "start_time": "12:00",
    "duration_minutes": 30,
    "center_freq_mhz": 1420.405,
    "bandwidth_mhz": 2.4,
    "gain_db": 40,
    "channels": 4096,
    "integration_time_s": 3.0,
    "filename": "",  # Auto-generated if empty
    "sdr_type": "b210",
    "calibrator": False,
    "end_action": "none",
    "enabled": True,
}


def find_clashes(schedule: list) -> list:
    """Check for overlapping enabled observations. Returns list of clash descriptions."""
    clashes = []
    enabled = [obs for obs in schedule if obs.get('enabled', True) and obs.get('start_time')]
    for a_idx, a in enumerate(enabled):
        a_date = a.get('start_date') or datetime.now().strftime('%Y-%m-%d')
        try:
            a_start = datetime.strptime(f"{a_date} {a['start_time']}", '%Y-%m-%d %H:%M')
        except ValueError:
            continue
        a_end = a_start + timedelta(minutes=a.get('duration_minutes', 30))
        for b_idx in range(a_idx + 1, len(enabled)):
            b = enabled[b_idx]
            b_date = b.get('start_date') or datetime.now().strftime('%Y-%m-%d')
            try:
                b_start = datetime.strptime(f"{b_date} {b['start_time']}", '%Y-%m-%d %H:%M')
            except ValueError:
                continue
            b_end = b_start + timedelta(minutes=b.get('duration_minutes', 30))
            if a_start < b_end and b_start < a_end:
                clashes.append(f"'{a.get('name')}' and '{b.get('name')}'")
    return clashes


def load_schedule() -> list:
    """Load schedule from JSON file."""
    if os.path.exists(SCHEDULE_FILE):
        try:
            with open(SCHEDULE_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            log.error("Error loading schedule: %s", e)
    return []


def save_schedule(schedule: list):
    """Save schedule to JSON file."""
    with open(SCHEDULE_FILE, 'w') as f:
        json.dump(schedule, f, indent=2)


def generate_filename(obs: dict) -> str:
    """Generate output filename for observation, placed in the data output folder."""
    data_folder = get_config_value("data_output_folder")
    os.makedirs(data_folder, exist_ok=True)
    if obs.get('filename'):
        return os.path.join(data_folder, obs['filename'])
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = obs.get('name', 'obs').replace(' ', '_').lower()
    cal = "_cal" if obs.get('calibrator') else ""
    return os.path.join(data_folder, f"h1_{name}{cal}_{timestamp}.h5")


def start_observation(obs: dict, duration_override: int = None) -> bool:
    """Start an observation.

    For calibration observations: runs repeated sun scans.
    For all others: commands telescope + starts SDR receiver.
    """
    global current_process, current_observation, observation_end_time

    # Calibration day: runs as a background thread, not a subprocess
    if obs.get('coord_system') == 'calibration':
        return _start_calibration_observation(obs, duration_override)

    with process_lock:
        if current_process is not None and current_process.poll() is None:
            return False

        # Point telescope at target and wait for slew before recording
        if SRT_CONTROLLER_URL:
            if not srt_point_telescope(obs):
                log.error("SRT failed to command telescope - aborting observation")
                return False

            if obs.get('coord_system') == 'satellite':
                # Start satellite tracking thread (sends /direct every second)
                tle_text = obs.get('tle_text', '')
                if tle_text:
                    start_satellite_tracking(tle_text)
                else:
                    log.error("No TLE data for satellite observation")
                    return False
            elif not srt_wait_for_slew():
                log.warning("SRT slew timeout - starting observation at current position")

        # Set calibrator state
        if SRT_CONTROLLER_URL:
            srt_set_calibrator(obs.get('calibrator', False))

        output_file = generate_filename(obs)
        env = os.environ.copy()
        env['H1_OUTPUT_FILE'] = output_file
        env['H1_CENTER_FREQ'] = str(obs.get('center_freq_mhz', 1420.405) * 1e6)
        env['H1_FFT_SIZE'] = str(obs.get('channels', 4096))
        env['H1_INTEGRATION_TIME'] = str(obs.get('integration_time_s', 3.0))
        env['H1_OBS_METADATA'] = json.dumps({
            'obs_name': obs.get('name', ''),
            'coord_system': obs.get('coord_system', ''),
            'object_name': obs.get('object_name', ''),
            'coord1_deg': obs.get('coord1_deg', 0),
            'coord1_min': obs.get('coord1_min', 0),
            'coord1_sec': obs.get('coord1_sec', 0.0),
            'coord2_deg': obs.get('coord2_deg', 0),
            'coord2_min': obs.get('coord2_min', 0),
            'coord2_sec': obs.get('coord2_sec', 0.0),
            'calibrator': obs.get('calibrator', False),
            'duration_minutes': obs.get('duration_minutes', 30),
            'start_date': obs.get('start_date', ''),
            'start_time': obs.get('start_time', ''),
            'tle_text': obs.get('tle_text', ''),
        })

        python_exe = PYTHON_PATH or sys.executable
        cmd = [
            python_exe,
            RECEIVER_SCRIPT,
            '--sdr', obs.get('sdr_type', 'b210'),
            '--gain', str(obs.get('gain_db', 40)),
            '--sample-rate', str(obs.get('bandwidth_mhz', 2.4) * 1e6),
        ]

        try:
            current_process = subprocess.Popen(cmd, env=env)
            now = datetime.now()
            duration = duration_override or obs.get('duration_minutes', 30)
            observation_end_time = now + timedelta(minutes=duration)
            current_observation = {
                **obs,
                'output_file': output_file,
                'started_at': now.isoformat(),
                'ends_at': observation_end_time.isoformat()
            }
            log.info("Started: %s (ends at %s)", obs.get('name'), observation_end_time.strftime('%H:%M:%S'))
            return True
        except Exception as e:
            log.error("Error starting observation: %s", e)
            return False


def stop_observation() -> bool:
    """Stop current observation."""
    global current_process, current_observation, observation_end_time

    # Handle calibration observations (thread-based, not subprocess)
    if current_observation and current_observation.get('coord_system') == 'calibration':
        name = current_observation.get('name', '?')
        cal_day_cancel.set()
        sun_scan_cancel.set()
        log.info("Stopped calibration: %s", name)
        current_observation = None
        observation_end_time = None
        return True

    with process_lock:
        if current_process is None:
            return False

        name = current_observation.get('name', '?') if current_observation else '?'

        if sys.platform == 'win32':
            current_process.terminate()
        else:
            current_process.send_signal(signal.SIGTERM)

        try:
            current_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            current_process.kill()
            current_process.wait()

        # Stop satellite tracking if active
        stop_satellite_tracking()

        # Ensure calibrator is off when observation ends
        if SRT_CONTROLLER_URL and current_observation and current_observation.get('calibrator'):
            srt_set_calibrator(False)

        # Return telescope to home/stow if requested
        if SRT_CONTROLLER_URL and current_observation:
            end_action = current_observation.get('end_action', 'none')
            if end_action == 'home':
                srt_go_position("home", 0, 0)
            elif end_action == 'stow':
                srt_go_position("stow", 90, 180)

        log.info("Stopped: %s", name)
        current_process = None
        current_observation = None
        observation_end_time = None
        return True


def _start_calibration_observation(obs: dict, duration_override: int = None) -> bool:
    """Start a calibration day as a scheduled observation.

    Uses the calibration day thread but tracks it as a running observation
    so the scheduler knows it's active.
    """
    global current_observation, observation_end_time, cal_day_thread

    if cal_day_state["running"]:
        log.warning("Calibration day already running")
        return False

    with process_lock:
        if current_process is not None and current_process.poll() is None:
            log.warning("Receiver busy — cannot start calibration")
            return False

    duration = duration_override or obs.get('duration_minutes', 480)
    now = datetime.now()
    observation_end_time = now + timedelta(minutes=duration)
    current_observation = {
        **obs,
        'started_at': now.isoformat(),
        'ends_at': observation_end_time.isoformat(),
    }

    params = {
        "n": obs.get("cal_grid_n", 5),
        "grid_spacing_deg": obs.get("cal_spacing_deg", 1.5),
        "integration_time_s": obs.get("integration_time_s", 3.0),
        "center_freq_mhz": obs.get("center_freq_mhz", 1420.405),
        "bandwidth_mhz": obs.get("bandwidth_mhz", 2.4),
        "gain_db": obs.get("gain_db", 40),
        "sdr_type": obs.get("sdr_type", "b210"),
        "beam_fwhm_deg": 3.0,
        "interval_minutes": obs.get("cal_interval_min", 30),
    }

    cal_day_cancel.clear()
    cal_day_thread = threading.Thread(target=_run_calibration_day, args=(params,),
                                      daemon=True)
    cal_day_thread.start()

    log.info("Started calibration day: %s (ends at %s)",
             obs.get('name'), observation_end_time.strftime('%H:%M:%S'))
    return True


def scheduler_thread():
    """Background thread that checks schedule and starts/stops observations."""
    global scheduler_running

    log.info("Background scheduler started")
    last_debug = 0

    while scheduler_running:
        try:
            now = datetime.now()
            schedule = load_schedule()

            # Debug output every 30 seconds
            if now.second % 30 < 5 and time.time() - last_debug > 25:
                last_debug = time.time()
                log.debug("%d observations loaded", len(schedule))
                for obs in schedule:
                    obs_date = obs.get('start_date', '') or now.strftime('%Y-%m-%d')
                    obs_time = obs.get('start_time', '')
                    enabled = obs.get('enabled', True)
                    log.debug("  - %s: %s %s (enabled=%s)", obs.get('name'), obs_date, obs_time, enabled)

            # Check if current observation should end
            with process_lock:
                if observation_end_time and now >= observation_end_time:
                    log.info("Duration complete")

            if observation_end_time and now >= observation_end_time:
                stop_observation()

            # Find which observation should be active right now
            due_obs = None
            due_scheduled = None
            due_remaining = None
            for obs in schedule:
                if not obs.get('enabled', True):
                    continue
                obs_date = obs.get('start_date', '')
                obs_time = obs.get('start_time', '')
                if not obs_time:
                    continue
                if not obs_date:
                    obs_date = now.strftime('%Y-%m-%d')
                try:
                    scheduled = datetime.strptime(f"{obs_date} {obs_time}", '%Y-%m-%d %H:%M')
                except ValueError:
                    continue
                duration_sec = obs.get('duration_minutes', 30) * 60
                end_time = scheduled + timedelta(seconds=duration_sec)
                diff = (now - scheduled).total_seconds()
                remaining_sec = (end_time - now).total_seconds()
                if diff >= 0 and remaining_sec > 60:
                    due_obs = obs
                    due_scheduled = scheduled
                    due_remaining = int(remaining_sec / 60)
                    break

            with process_lock:
                is_running = current_process is not None and current_process.poll() is None
                running_name = current_observation.get('name', '') if current_observation else ''
            # Also count calibration day as running
            if not is_running and current_observation and current_observation.get('coord_system') == 'calibration':
                is_running = cal_day_state["running"]

            if due_obs:
                if is_running and running_name == due_obs.get('name', ''):
                    # Already running the correct observation
                    pass
                elif is_running:
                    # Preempt: stop current, start the one that's due
                    log.info("Preempting '%s' for '%s'", running_name, due_obs.get('name'))
                    stop_observation()
                    start_observation(due_obs, duration_override=due_remaining)
                else:
                    # Nothing running, start the due observation
                    diff = (now - due_scheduled).total_seconds()
                    if diff < 60:
                        log.info("Scheduled start: %s", due_obs.get('name'))
                    else:
                        log.info("Late start: %s (%dmin remaining)", due_obs.get('name'), due_remaining)
                    start_observation(due_obs, duration_override=due_remaining)

        except Exception as e:
            log.error("Scheduler error: %s", e, exc_info=True)

        time.sleep(5)  # Check every 5 seconds

    log.info("Background scheduler stopped")


# HTML Template
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ banner_name }}</title>
    <style>
        * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
        body { margin: 0; padding: 20px; background: #1a1a2e; color: #eee; min-height: 100vh; }
        h1 { color: #00d4ff; margin-bottom: 10px; }
        .subtitle { color: #888; margin-bottom: 20px; }
        .container { max-width: 1400px; margin: 0 auto; }
        .status-bar { background: #16213e; padding: 15px; border-radius: 8px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }
        .status-indicator { display: flex; align-items: center; gap: 10px; }
        .status-dot { width: 12px; height: 12px; border-radius: 50%; background: #666; }
        .status-dot.running { background: #00ff88; animation: pulse 1s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
        .btn { padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; font-size: 14px; font-weight: 500; transition: all 0.2s; }
        .btn-primary { background: #00d4ff; color: #000; }
        .btn-primary:hover { background: #00a8cc; }
        .btn-danger { background: #ff4757; color: #fff; }
        .btn-danger:hover { background: #ff3344; }
        .btn-secondary { background: #444; color: #fff; }
        .btn-secondary:hover { background: #555; }
        .btn-success { background: #00ff88; color: #000; }
        .btn-success:hover { background: #00cc6a; }
        .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 15px; }
        .form-group { display: flex; flex-direction: column; }
        .form-group label { font-size: 11px; color: #888; margin-bottom: 5px; text-transform: uppercase; }
        .form-group input, .form-group select { padding: 8px; border: 1px solid #333; border-radius: 5px; background: #0f0f23; color: #fff; font-size: 14px; }
        .form-group input:focus, .form-group select:focus { outline: none; border-color: #00d4ff; }
        .form-group.wide { grid-column: span 2; }
        .coord-row { display: flex; gap: 5px; align-items: center; }
        .coord-row input { width: 60px; text-align: center; }
        .coord-row span { color: #888; font-size: 12px; }
        .schedule-list { margin-top: 20px; }
        .schedule-item { background: #0f0f23; border: 1px solid #333; border-radius: 8px; padding: 15px; margin-bottom: 10px; display: grid; grid-template-columns: auto 1fr auto; gap: 15px; align-items: center; }
        .schedule-item.disabled { opacity: 0.5; }
        .schedule-item .checkbox { width: 20px; height: 20px; cursor: pointer; }
        .schedule-info { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; }
        .schedule-info .field { font-size: 12px; }
        .schedule-info .field-label { color: #666; font-size: 10px; }
        .schedule-info .field-value { color: #fff; }
        .schedule-actions { display: flex; gap: 8px; }
        .btn-icon { padding: 6px 10px; font-size: 12px; }
        .toolbar { display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; }
        .file-input { display: none; }
        .current-obs { background: #1a3a1a; border: 1px solid #00ff88; }
        .empty-state { text-align: center; padding: 40px; color: #666; }
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); justify-content: center; align-items: center; z-index: 1000; }
        .modal.active { display: flex; }
        .modal-content { background: #16213e; border-radius: 8px; padding: 25px; width: 95%; max-width: 800px; max-height: 90vh; overflow-y: auto; }
        .modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .modal-title { font-size: 20px; color: #00d4ff; }
        .close-btn { background: none; border: none; color: #888; font-size: 24px; cursor: pointer; }
        .close-btn:hover { color: #fff; }
        .modal-footer { display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px; }
        .section-title { color: #00d4ff; font-size: 14px; margin: 20px 0 10px 0; padding-bottom: 5px; border-bottom: 1px solid #333; }
        .coord-section { background: #0f0f23; padding: 15px; border-radius: 8px; margin-bottom: 15px; }
        .tabs { display: flex; gap: 0; margin-bottom: 20px; border-bottom: 2px solid #333; }
        .tab { padding: 10px 25px; cursor: pointer; color: #888; border-bottom: 2px solid transparent; margin-bottom: -2px; transition: all 0.2s; font-size: 14px; }
        .tab:hover { color: #ccc; }
        .tab.active { color: #00d4ff; border-bottom-color: #00d4ff; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .config-form { background: #0f0f23; border: 1px solid #333; border-radius: 8px; padding: 20px; max-width: 700px; }
        .config-form .form-group { margin-bottom: 15px; }
        .config-form input { width: 100%; }
        .config-saved { color: #00ff88; font-size: 13px; display: none; margin-left: 10px; }
        .log-container { background: #0a0a1a; border: 1px solid #333; border-radius: 8px; padding: 15px; font-family: monospace; font-size: 12px; line-height: 1.6; max-height: 70vh; overflow-y: auto; white-space: pre-wrap; word-break: break-all; color: #ccc; }
        .log-controls { display: flex; gap: 10px; align-items: center; margin-bottom: 10px; }
        .log-controls select, .log-controls input { padding: 6px 10px; border: 1px solid #333; border-radius: 5px; background: #0f0f23; color: #fff; font-size: 13px; }
    </style>
</head>
<body>
    <div class="container">
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <div>
                <h1>{{ banner_name }}</h1>
                <p class="subtitle">{{ banner_subtitle }}</p>
            </div>
            <div style="text-align:right;">
                <div id="currentDate" style="color:#888; font-size:14px;"></div>
                <div style="display:flex; gap:20px; justify-content:flex-end; align-items:baseline;">
                    <div>
                        <div style="color:#888; font-size:10px; text-transform:uppercase;">Local Time</div>
                        <div id="currentTime" style="color:#00d4ff; font-size:32px; font-family:monospace; font-weight:bold;"></div>
                    </div>
                    <div>
                        <div style="color:#888; font-size:10px; text-transform:uppercase;">UTC</div>
                        <div id="utcTime" style="color:#ff9500; font-size:24px; font-family:monospace; font-weight:bold;"></div>
                    </div>
                </div>
            </div>
        </div>
        <div class="status-bar">
            <div class="status-indicator">
                <div class="status-dot" id="statusDot"></div>
                <span id="statusText">Idle</span>
            </div>
            <div class="status-indicator" style="margin-left:30px;">
                <div class="status-dot" id="telescopeDot"></div>
                <span id="telescopeText">Telescope: --</span>
            </div>
            <div>
                <button class="btn btn-danger" id="stopBtn" style="display:none" onclick="stopObs()">Stop</button>
            </div>
        </div>
        <div class="tabs">
            <div class="tab active" onclick="switchTab('scheduler')">Scheduler</div>
            <div class="tab" onclick="switchTab('sunscan')">Sun Scan</div>
            <div class="tab" onclick="switchTab('config')">Configuration</div>
            <div class="tab" onclick="switchTab('log')">Log</div>
        </div>

        <div class="tab-content active" id="tab-scheduler">
            <div class="toolbar">
                <button class="btn btn-primary" onclick="openAddModal()">+ Add Observation</button>
                <button class="btn btn-secondary" onclick="saveSchedule()">Save Schedule</button>
                <button class="btn btn-secondary" onclick="document.getElementById('loadFile').click()">Load</button>
                <input type="file" id="loadFile" class="file-input" accept=".json" onchange="loadFile(event)">
                <button class="btn btn-secondary" onclick="exportSchedule()">Export JSON</button>
                <button class="btn btn-secondary" onclick="clearPast()">Clear Past</button>
            </div>
            <div class="schedule-list" id="scheduleList">
                <div class="empty-state">No observations scheduled.</div>
            </div>
        </div>

        <div class="tab-content" id="tab-sunscan">
            <div style="display:flex; gap:20px; flex-wrap:wrap;">
                <div class="config-form" style="flex:0 0 340px;">
                    <div class="section-title">Scan Parameters</div>
                    <div class="form-group">
                        <label>Grid Size (n x n)</label>
                        <input type="number" id="ssGridN" min="3" max="15" step="2" value="5">
                    </div>
                    <div class="form-group">
                        <label>Grid Spacing (degrees)</label>
                        <input type="number" id="ssSpacing" min="0.1" max="10" step="0.1" value="1.5">
                    </div>
                    <div class="form-group">
                        <label>Beam FWHM Hint (degrees)</label>
                        <input type="number" id="ssBeamFwhm" min="0.5" max="20" step="0.1" value="3.0">
                    </div>
                    <div class="section-title">Receiver Settings</div>
                    <div class="form-group">
                        <label>Integration Time per Point (s)</label>
                        <input type="number" id="ssIntegration" min="0.1" max="60" step="0.1" value="3.0">
                    </div>
                    <div class="form-group">
                        <label>Center Frequency (MHz)</label>
                        <input type="number" id="ssCenterFreq" step="0.001" value="1420.405">
                    </div>
                    <div class="form-group">
                        <label>Bandwidth (MHz)</label>
                        <input type="number" id="ssBandwidth" step="0.1" value="2.4">
                    </div>
                    <div class="form-group">
                        <label>Gain (dB)</label>
                        <input type="number" id="ssGain" min="0" max="80" value="40">
                    </div>
                    <div class="form-group">
                        <label>SDR Type</label>
                        <select id="ssSdrType">
                            <option value="b210">Ettus B210</option>
                            <option value="rtlsdr">RTL-SDR</option>
                            <option value="demo">Demo (Simulated)</option>
                        </select>
                    </div>
                    <div style="margin-top:15px; display:flex; gap:10px;">
                        <button class="btn btn-primary" id="ssStartBtn" onclick="startSunScan()">Start Sun Scan</button>
                        <button class="btn btn-danger" id="ssStopBtn" style="display:none" onclick="stopSunScan()">Stop</button>
                    </div>
                </div>
                <div style="flex:1; min-width:300px;">
                    <div class="section-title">Status</div>
                    <div id="ssStatus" style="background:#0f0f23; border:1px solid #333; border-radius:8px; padding:15px; margin-bottom:15px;">
                        <span style="color:#888;">Idle — configure parameters and click Start.</span>
                    </div>
                    <div id="ssProgress" style="display:none; margin-bottom:15px;">
                        <div style="background:#333; border-radius:4px; height:20px; overflow:hidden;">
                            <div id="ssProgressBar" style="background:#00d4ff; height:100%; width:0%; transition:width 0.3s;"></div>
                        </div>
                        <div id="ssProgressText" style="color:#888; font-size:12px; margin-top:5px;">0 / 0</div>
                    </div>
                    <div class="section-title">Results</div>
                    <div id="ssResults" style="background:#0f0f23; border:1px solid #333; border-radius:8px; padding:15px; margin-bottom:15px;">
                        <span style="color:#888;">No results yet.</span>
                    </div>
                    <div id="ssImageContainer" style="text-align:center;">
                    </div>
                </div>
            </div>

            <div style="margin-top:30px; border-top:2px solid #333; padding-top:20px;">
                <div style="display:flex; gap:20px; flex-wrap:wrap;">
                    <div class="config-form" style="flex:0 0 340px;">
                        <div class="section-title">Calibration Day</div>
                        <p style="color:#888; font-size:12px; margin-bottom:15px;">
                            Run repeated sun scans to determine the effective observer lat/lon
                            (correcting for mount tilt) and constant alt/az offsets.
                        </p>
                        <div class="form-group">
                            <label>Scan Interval (minutes)</label>
                            <input type="number" id="cdInterval" min="5" max="120" value="30">
                        </div>
                        <div style="margin-top:15px; display:flex; gap:10px; flex-wrap:wrap;">
                            <button class="btn btn-success" id="cdStartBtn" onclick="startCalDay()">Start Calibration Day</button>
                            <button class="btn btn-danger" id="cdStopBtn" style="display:none" onclick="stopCalDay()">Stop</button>
                            <button class="btn btn-primary" id="cdFitBtn" onclick="fitModel()">Fit Model</button>
                            <button class="btn btn-secondary" onclick="clearCalData()">Clear Data</button>
                        </div>
                    </div>
                    <div style="flex:1; min-width:300px;">
                        <div class="section-title">Calibration Status</div>
                        <div id="cdStatus" style="background:#0f0f23; border:1px solid #333; border-radius:8px; padding:15px; margin-bottom:15px;">
                            <span style="color:#888;">Idle.</span>
                        </div>
                        <div class="section-title">Pointing Model</div>
                        <div id="cdModel" style="background:#0f0f23; border:1px solid #333; border-radius:8px; padding:15px; margin-bottom:15px;">
                            <span style="color:#888;">No model fitted yet.</span>
                        </div>
                        <div style="display:flex; gap:10px; margin-bottom:15px;">
                            <button class="btn btn-success" id="cdApplyBtn" style="display:none" onclick="applyModel()">Apply to Telescope</button>
                        </div>
                        <div id="cdPlotContainer" style="text-align:center;">
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <div class="tab-content" id="tab-config">
            <div class="config-form">
                <div class="section-title">Appearance</div>
                <div class="form-group">
                    <label>Banner Name</label>
                    <input type="text" id="cfgBannerName" placeholder="H1 Receiver Scheduler">
                </div>
                <div class="form-group">
                    <label>Banner Subtitle</label>
                    <input type="text" id="cfgBannerSubtitle" placeholder="Hydrogen Line (21cm) Observation Manager">
                </div>

                <div class="section-title">SRT Telescope Controller</div>
                <div class="form-group">
                    <label>Controller URL (leave empty to disable)</label>
                    <input type="text" id="cfgControllerUrl" placeholder="http://192.168.0.149">
                </div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Slew Timeout (seconds)</label>
                        <input type="number" id="cfgSlewTimeout" min="10" max="600">
                    </div>
                    <div class="form-group">
                        <label>Position Tolerance (degrees)</label>
                        <input type="number" id="cfgPositionTolerance" step="0.1" min="0.1" max="5">
                    </div>
                </div>

                <div class="section-title">Observer Location</div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Latitude (degrees, +N)</label>
                        <input type="number" id="cfgObsLat" step="0.001" min="-90" max="90">
                    </div>
                    <div class="form-group">
                        <label>Longitude (degrees, +E)</label>
                        <input type="number" id="cfgObsLon" step="0.001" min="-180" max="180">
                    </div>
                    <div class="form-group">
                        <label>Elevation (metres)</label>
                        <input type="number" id="cfgObsElev" step="1" min="0" max="9000">
                    </div>
                    <div class="form-group">
                        <label>Min Elevation for passes (degrees)</label>
                        <input type="number" id="cfgMinElev" step="1" min="0" max="90">
                    </div>
                </div>

                <div class="section-title">Receiver</div>
                <div class="form-group">
                    <label>Python Executable (leave empty for default)</label>
                    <input type="text" id="cfgPythonPath" placeholder="e.g., C:\\Users\\graha\\radioconda\\python.exe">
                </div>

                <div class="section-title">Data Output</div>
                <div class="form-group">
                    <label>Output Folder</label>
                    <input type="text" id="cfgDataFolder" placeholder="Path to store observation data files">
                </div>

                <div class="section-title">Log</div>
                <div class="form-group">
                    <label>Log Lines to Display</label>
                    <input type="number" id="cfgLogLines" min="20" max="1000" step="10">
                </div>

                <div class="section-title">Notifications</div>
                <div class="form-group">
                    <label>Sound on Start/Stop</label>
                    <select id="cfgSoundEnabled">
                        <option value="true">Enabled</option>
                        <option value="false">Disabled</option>
                    </select>
                </div>

                <div style="margin-top:20px; display:flex; align-items:center;">
                    <button class="btn btn-primary" type="button" onclick="saveConfig()">Save Configuration</button>
                    <span class="config-saved" id="configSaved">Saved!</span>
                </div>
            </div>
        </div>

        <div class="tab-content" id="tab-log">
            <div class="log-controls">
                <button class="btn btn-secondary" onclick="loadLog()">Refresh</button>
                <label style="color:#888; font-size:12px;">
                    <input type="checkbox" id="logAutoRefresh" onchange="toggleLogRefresh()" checked> Auto-refresh (5s)
                </label>
            </div>
            <div class="log-container" id="logContent">Loading log...</div>
        </div>
    </div>

    <div class="modal" id="obsModal">
        <div class="modal-content">
            <div class="modal-header">
                <h2 class="modal-title" id="modalTitle">Add Observation</h2>
                <button class="close-btn" onclick="closeModal()">&times;</button>
            </div>
            <form id="obsForm">
                <input type="hidden" id="obsIndex" value="-1">

                <div class="form-grid">
                    <div class="form-group wide">
                        <label>Observation Name</label>
                        <input type="text" id="obsName" required placeholder="e.g., Galactic Center Survey">
                    </div>
                </div>

                <div class="section-title">Schedule <span style="font-weight:normal; font-size:11px; color:#888;">(Local Time)</span></div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Start Date</label>
                        <input type="date" id="obsStartDate">
                    </div>
                    <div class="form-group">
                        <label>Start Time</label>
                        <input type="time" id="obsStartTime" required onchange="updateEndTime()">
                    </div>
                    <div class="form-group">
                        <label>Duration (minutes)</label>
                        <input type="number" id="obsDuration" min="1" max="1440" required onchange="updateEndTime()">
                    </div>
                    <div class="form-group">
                        <label>End Time</label>
                        <input type="time" id="obsEndTime" disabled style="background:#2a2a4a; color:#aaa;">
                    </div>
                </div>
                <div id="clashWarning" style="display:none; color:#ff4757; background:#3a1a1a; padding:8px 12px; border-radius:5px; margin-top:5px; font-size:13px;"></div>

                <div class="section-title">Target Coordinates</div>
                <div class="coord-section">
                    <div class="form-grid">
                        <div class="form-group">
                            <label>Coordinate System</label>
                            <select id="obsCoordSystem" onchange="updateCoordLabels()">
                                <option value="altaz">Alt/Az (Horizontal)</option>
                                <option value="radec">RA/Dec (Equatorial J2000)</option>
                                <option value="galactic">Galactic (l, b)</option>
                                <option value="object">Solar System Object</option>
                                <option value="satellite">Satellite (TLE)</option>
                                <option value="calibration">Calibration Day (Sun Scan)</option>
                            </select>
                        </div>
                    </div>
                    <div class="form-grid" id="objectSelector" style="margin-top:15px; display:none">
                        <div class="form-group">
                            <label>Object</label>
                            <select id="obsObjectName">
                                <option value="sun">Sun</option>
                                <option value="moon">Moon</option>
                            </select>
                        </div>
                    </div>
                    <div id="satelliteInput" style="margin-top:15px; display:none">
                        <div style="display:flex; gap:10px; align-items:flex-end; margin-bottom:10px;">
                            <div class="form-group" style="flex:1; margin:0;">
                                <label>Search CelesTrak by name or NORAD ID</label>
                                <input type="text" id="tleSearch" placeholder="e.g. ISS, NOAA 19, 25544" style="width:100%;">
                            </div>
                            <button class="btn btn-primary" type="button" onclick="fetchTle()" style="white-space:nowrap;">Fetch TLE</button>
                        </div>
                        <div id="tleResults" style="display:none; margin-bottom:10px;">
                            <div class="form-group">
                                <label>Select satellite</label>
                                <select id="tleResultSelect" onchange="selectTleResult()" style="width:100%;"></select>
                            </div>
                        </div>
                        <div class="form-group">
                            <label>TLE (paste, search above, or load from file)</label>
                            <textarea id="obsTleText" rows="4" style="width:100%; padding:8px; border:1px solid #333; border-radius:5px; background:#0f0f23; color:#fff; font-family:monospace; font-size:12px; resize:vertical;" placeholder="ISS (ZARYA)
1 25544U 98067A   ...
2 25544  51.6400  ..."></textarea>
                        </div>
                        <div style="display:flex; gap:10px; margin-top:8px; align-items:center; flex-wrap:wrap;">
                            <button class="btn btn-primary" type="button" onclick="predictPass()">Compute Next Pass</button>
                            <label class="btn btn-secondary" style="margin:0; cursor:pointer;">
                                Load TLE File <input type="file" accept=".tle,.txt" style="display:none" onchange="loadTleFile(event)">
                            </label>
                            <span id="passInfo" style="color:#888; font-size:12px;"></span>
                        </div>
                        <div id="passDetails" style="display:none; margin-top:10px; padding:10px; background:#0f0f23; border-radius:5px; font-size:12px; color:#ccc;">
                        </div>
                    </div>
                    <div id="calibrationInput" style="margin-top:15px; display:none">
                        <p style="color:#888; font-size:12px; margin-bottom:10px;">
                            Runs repeated sun scans for the observation duration to determine
                            effective observer lat/lon and pointing offsets.
                        </p>
                        <div class="form-grid">
                            <div class="form-group">
                                <label>Grid Size (n x n)</label>
                                <input type="number" id="obsCalGridN" min="3" max="15" step="2" value="5">
                            </div>
                            <div class="form-group">
                                <label>Grid Spacing (degrees)</label>
                                <input type="number" id="obsCalSpacing" min="0.1" max="10" step="0.1" value="1.5">
                            </div>
                            <div class="form-group">
                                <label>Scan Interval (minutes)</label>
                                <input type="number" id="obsCalInterval" min="5" max="120" value="30">
                            </div>
                        </div>
                    </div>
                    <div class="form-grid" id="coordInputs" style="margin-top:15px">
                        <div class="form-group">
                            <label id="coord1Label">Altitude</label>
                            <div class="coord-row">
                                <input type="number" id="coord1Deg" min="-90" max="360" value="45">
                                <span id="coord1Unit1">deg</span>
                                <input type="number" id="coord1Min" min="0" max="59" value="0">
                                <span>min</span>
                                <input type="number" id="coord1Sec" min="0" max="59.99" step="0.01" value="0">
                                <span>sec</span>
                            </div>
                        </div>
                        <div class="form-group">
                            <label id="coord2Label">Azimuth</label>
                            <div class="coord-row">
                                <input type="number" id="coord2Deg" min="-90" max="360" value="180">
                                <span>deg</span>
                                <input type="number" id="coord2Min" min="0" max="59" value="0">
                                <span>min</span>
                                <input type="number" id="coord2Sec" min="0" max="59.99" step="0.01" value="0">
                                <span>sec</span>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="section-title">Receiver Settings</div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Center Frequency (MHz)</label>
                        <input type="number" id="obsCenterFreq" step="0.001" required value="1420.405">
                    </div>
                    <div class="form-group">
                        <label>Bandwidth (MHz)</label>
                        <input type="number" id="obsBandwidth" step="0.1" required value="2.4">
                    </div>
                    <div class="form-group">
                        <label>Gain (dB)</label>
                        <input type="number" id="obsGain" min="0" max="80" required value="40">
                    </div>
                    <div class="form-group">
                        <label>Channels (FFT)</label>
                        <select id="obsChannels">
                            <option value="1024">1024</option>
                            <option value="2048">2048</option>
                            <option value="4096" selected>4096</option>
                            <option value="8192">8192</option>
                            <option value="16384">16384</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Integration Time (s)</label>
                        <input type="number" id="obsIntegration" step="0.1" min="0.1" required value="3.0">
                    </div>
                    <div class="form-group">
                        <label>SDR Type</label>
                        <select id="obsSdrType">
                            <option value="b210">Ettus B210</option>
                            <option value="rtlsdr">RTL-SDR</option>
                            <option value="demo">Demo (Simulated)</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Calibrator (Noise Source)</label>
                        <select id="obsCalibrator">
                            <option value="off">Off</option>
                            <option value="on">On</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>When Done</label>
                        <select id="obsEndAction">
                            <option value="none">Stay</option>
                            <option value="home">Go Home (Alt 0, Az 0)</option>
                            <option value="stow">Stow (Alt 90, Az 180)</option>
                        </select>
                    </div>
                </div>

                <div class="section-title">Output</div>
                <div class="form-grid">
                    <div class="form-group wide">
                        <label>Filename (leave empty for auto)</label>
                        <input type="text" id="obsFilename" placeholder="auto-generated if empty">
                    </div>
                </div>

                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                    <button type="submit" class="btn btn-primary">Save Observation</button>
                </div>
            </form>
        </div>
    </div>

    <script>
        let schedule = [];
        let currentObs = null;

        const COORD_CONFIG = {
            altaz: {
                c1: 'Altitude', c2: 'Azimuth', u1: 'deg',
                c1_min: 0, c1_max: 90,      // Alt: 0 to 90 (above horizon)
                c2_min: 0, c2_max: 359      // Az: 0 to 359
            },
            radec: {
                c1: 'Right Ascension', c2: 'Declination', u1: 'h',
                c1_min: 0, c1_max: 23,      // RA: 0h to 23h (+ min/sec)
                c2_min: -90, c2_max: 90     // Dec: -90 to +90
            },
            galactic: {
                c1: 'Galactic Longitude (l)', c2: 'Galactic Latitude (b)', u1: 'deg',
                c1_min: 0, c1_max: 359,     // l: 0 to 359
                c2_min: -90, c2_max: 90     // b: -90 to +90
            }
        };

        const DEFAULTS = {
            name: "New Observation",
            coord_system: "altaz",
            coord1_deg: 45, coord1_min: 0, coord1_sec: 0,
            coord2_deg: 180, coord2_min: 0, coord2_sec: 0,
            start_date: "", start_time: "12:00",
            duration_minutes: 30,
            center_freq_mhz: 1420.405,
            bandwidth_mhz: 2.4,
            gain_db: 40,
            channels: 4096,
            integration_time_s: 3.0,
            filename: "",
            sdr_type: "b210",
            calibrator: false,
            end_action: "none",
            enabled: true
        };

        function updateClock() {
            const now = new Date();
            const date = now.toLocaleDateString('en-US', {weekday:'long', year:'numeric', month:'long', day:'numeric'});
            const localTime = now.toLocaleTimeString('en-US', {hour12:false, hour:'2-digit', minute:'2-digit', second:'2-digit'});
            const utcTime = now.toISOString().substring(11, 19);
            document.getElementById('currentDate').textContent = date;
            document.getElementById('currentTime').textContent = localTime;
            document.getElementById('utcTime').textContent = utcTime;
        }

        document.addEventListener('DOMContentLoaded', () => {
            updateClock();
            setInterval(updateClock, 1000);
            loadSchedule();
            updateStatus();
            updateTelescope();
            setInterval(updateStatus, 2000);
            setInterval(updateTelescope, 5000);
            fetch('/api/config').then(r => r.json()).then(cfg => {
                soundEnabled = cfg.sound_enabled !== false;
            });
        });

        document.getElementById('obsForm').addEventListener('submit', e => {
            e.preventDefault();
            saveObservation();
        });

        function updateEndTime() {
            const date = document.getElementById('obsStartDate').value || new Date().toISOString().slice(0,10);
            const time = document.getElementById('obsStartTime').value;
            const dur = parseInt(document.getElementById('obsDuration').value) || 0;
            if (!time) return;
            const start = new Date(`${date}T${time}`);
            const end = new Date(start.getTime() + dur * 60000);
            const hh = String(end.getHours()).padStart(2,'0');
            const mm = String(end.getMinutes()).padStart(2,'0');
            document.getElementById('obsEndTime').value = `${hh}:${mm}`;
            checkClash();
        }

        function getObsInterval(obs) {
            const date = obs.start_date || new Date().toISOString().slice(0,10);
            const start = new Date(`${date}T${obs.start_time}`);
            const end = new Date(start.getTime() + (obs.duration_minutes || 0) * 60000);
            return {start, end};
        }

        function checkClash() {
            const editIdx = parseInt(document.getElementById('obsIndex').value);
            const date = document.getElementById('obsStartDate').value || new Date().toISOString().slice(0,10);
            const time = document.getElementById('obsStartTime').value;
            const dur = parseInt(document.getElementById('obsDuration').value) || 0;
            if (!time) return;
            const newStart = new Date(`${date}T${time}`);
            const newEnd = new Date(newStart.getTime() + dur * 60000);
            const warn = document.getElementById('clashWarning');
            const clashes = [];
            schedule.forEach((obs, i) => {
                if (i === editIdx || !obs.enabled || !obs.start_time) return;
                const {start, end} = getObsInterval(obs);
                if (newStart < end && newEnd > start) {
                    clashes.push(obs.name);
                }
            });
            if (clashes.length > 0) {
                warn.textContent = 'Clashes with: ' + clashes.join(', ');
                warn.style.display = 'block';
            } else {
                warn.style.display = 'none';
            }
            return clashes.length > 0;
        }

        function updateCoordLabels() {
            const sys = document.getElementById('obsCoordSystem').value;
            const isObject = sys === 'object';
            const isSat = sys === 'satellite';
            const isCal = sys === 'calibration';
            document.getElementById('objectSelector').style.display = isObject ? '' : 'none';
            document.getElementById('satelliteInput').style.display = isSat ? '' : 'none';
            document.getElementById('calibrationInput').style.display = isCal ? '' : 'none';
            document.getElementById('coordInputs').style.display = (isObject || isSat || isCal) ? 'none' : '';
            if (isObject || isSat || isCal) return;
            const cfg = COORD_CONFIG[sys];
            document.getElementById('coord1Label').textContent = cfg.c1;
            document.getElementById('coord2Label').textContent = cfg.c2;
            document.getElementById('coord1Unit1').textContent = cfg.u1;
            // Set min/max limits
            const c1 = document.getElementById('coord1Deg');
            const c2 = document.getElementById('coord2Deg');
            c1.min = cfg.c1_min; c1.max = cfg.c1_max;
            c2.min = cfg.c2_min; c2.max = cfg.c2_max;
            // Clamp current values to valid range
            c1.value = Math.max(cfg.c1_min, Math.min(cfg.c1_max, c1.value));
            c2.value = Math.max(cfg.c2_min, Math.min(cfg.c2_max, c2.value));
        }

        function formatCoord(deg, min, sec, isRA) {
            const d = parseInt(deg) || 0;
            const m = parseInt(min) || 0;
            const s = parseFloat(sec) || 0;
            if (isRA) {
                return `${d}h ${m}m ${s.toFixed(1)}s`;
            }
            const sign = d < 0 ? '-' : '+';
            return `${sign}${Math.abs(d)}° ${m}' ${s.toFixed(1)}"`;
        }

        function formatCoordDisplay(obs) {
            const sys = obs.coord_system || 'altaz';
            if (sys === 'object') {
                const name = obs.object_name || 'unknown';
                return `Object: ${name.charAt(0).toUpperCase() + name.slice(1)}`;
            }
            if (sys === 'satellite') {
                const tle = obs.tle_text || '';
                const name = tle.split('\\n')[0] || 'Satellite';
                return `Sat: ${name.substring(0, 20)}`;
            }
            if (sys === 'calibration') {
                const n = obs.cal_grid_n || 5;
                const interval = obs.cal_interval_min || 30;
                return `Cal: ${n}x${n} every ${interval}min`;
            }
            const isRA = sys === 'radec';
            const c1 = formatCoord(obs.coord1_deg, obs.coord1_min, obs.coord1_sec, isRA);
            const c2 = formatCoord(obs.coord2_deg, obs.coord2_min, obs.coord2_sec, false);
            const labels = {altaz: 'Alt/Az', radec: 'RA/Dec', galactic: 'Gal'};
            return `${labels[sys]}: ${c1}, ${c2}`;
        }

        function loadSchedule() {
            fetch('/api/schedule').then(r => r.json()).then(data => {
                schedule = data;
                renderSchedule();
            });
        }

        function saveSchedule() {
            fetch('/api/schedule', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(schedule)
            }).then(() => alert('Schedule saved!'));
        }

        function formatEndTime(obs) {
            // For the currently running observation, show the actual end time from the server
            if (currentObs && currentObs.name === obs.name && currentObs.ends_at) {
                const end = new Date(currentObs.ends_at);
                const hh = String(end.getHours()).padStart(2,'0');
                const mm = String(end.getMinutes()).padStart(2,'0');
                return hh + ':' + mm + ' (live)';
            }
            if (obs.end_time) {
                return (obs.end_date !== obs.start_date ? obs.end_date + ' ' : '') + obs.end_time;
            }
            return obs.duration_minutes + ' min';
        }

        function renderSchedule() {
            const list = document.getElementById('scheduleList');
            if (schedule.length === 0) {
                list.innerHTML = '<div class="empty-state">No observations scheduled.</div>';
                return;
            }
            list.innerHTML = schedule.map((obs, i) => `
                <div class="schedule-item ${obs.enabled ? '' : 'disabled'} ${currentObs?.name === obs.name ? 'current-obs' : ''}">
                    <input type="checkbox" class="checkbox" ${obs.enabled ? 'checked' : ''} onchange="toggleEnabled(${i})">
                    <div class="schedule-info">
                        <div class="field"><div class="field-label">Name</div><div class="field-value">${obs.name}</div></div>
                        <div class="field"><div class="field-label">Start</div><div class="field-value">${obs.start_date || 'Today'} ${obs.start_time}</div></div>
                        <div class="field"><div class="field-label">End</div><div class="field-value">${formatEndTime(obs)}</div></div>
                        <div class="field"><div class="field-label">Coordinates</div><div class="field-value">${formatCoordDisplay(obs)}</div></div>
                        <div class="field"><div class="field-label">Frequency</div><div class="field-value">${obs.center_freq_mhz} MHz</div></div>
                        <div class="field"><div class="field-label">BW / Gain</div><div class="field-value">${obs.bandwidth_mhz} MHz / ${obs.gain_db} dB</div></div>
                        <div class="field"><div class="field-label">Cal / End</div><div class="field-value">${obs.calibrator ? 'CAL' : '-'} / ${({home:'Home',stow:'Stow'})[obs.end_action] || '-'}</div></div>
                        <div class="field"><div class="field-label">Channels</div><div class="field-value">${obs.channels}</div></div>
                        <div class="field"><div class="field-label">Integration</div><div class="field-value">${obs.integration_time_s}s</div></div>
                    </div>
                    <div class="schedule-actions">
                        <button class="btn btn-success btn-icon" onclick="runNow(${i})" title="Run Now">▶</button>
                        <button class="btn btn-secondary btn-icon" onclick="cloneObs(${i})" title="Clone">⧉</button>
                        <button class="btn btn-secondary btn-icon" onclick="editObs(${i})" title="Edit">✎</button>
                        <button class="btn btn-danger btn-icon" onclick="deleteObs(${i})" title="Delete">✕</button>
                    </div>
                </div>
            `).join('');
        }

        function openAddModal() {
            document.getElementById('modalTitle').textContent = 'Add Observation';
            document.getElementById('obsIndex').value = -1;
            fillForm(DEFAULTS);
            document.getElementById('obsModal').classList.add('active');
        }

        function isRunning(obs) {
            return currentObs && currentObs.name === obs.name;
        }

        function editObs(i) {
            if (isRunning(schedule[i])) {
                alert('Cannot edit while running. Stop the observation first.');
                return;
            }
            document.getElementById('modalTitle').textContent = 'Edit Observation';
            document.getElementById('obsIndex').value = i;
            fillForm(schedule[i]);
            document.getElementById('obsModal').classList.add('active');
        }

        function cloneObs(i) {
            document.getElementById('modalTitle').textContent = 'Clone Observation';
            document.getElementById('obsIndex').value = -1;
            const clone = Object.assign({}, schedule[i]);
            clone.name = clone.name + ' (copy)';
            clone.start_date = '';
            clone.start_time = '';
            clone.end_date = '';
            clone.end_time = '';
            clone.filename = '';
            fillForm(clone);
            document.getElementById('obsModal').classList.add('active');
        }

        function fillForm(obs) {
            document.getElementById('obsName').value = obs.name || DEFAULTS.name;
            document.getElementById('obsCoordSystem').value = obs.coord_system || DEFAULTS.coord_system;
            document.getElementById('obsObjectName').value = obs.object_name || 'sun';
            document.getElementById('obsTleText').value = obs.tle_text || '';
            document.getElementById('coord1Deg').value = obs.coord1_deg ?? DEFAULTS.coord1_deg;
            document.getElementById('coord1Min').value = obs.coord1_min ?? DEFAULTS.coord1_min;
            document.getElementById('coord1Sec').value = obs.coord1_sec ?? DEFAULTS.coord1_sec;
            document.getElementById('coord2Deg').value = obs.coord2_deg ?? DEFAULTS.coord2_deg;
            document.getElementById('coord2Min').value = obs.coord2_min ?? DEFAULTS.coord2_min;
            document.getElementById('coord2Sec').value = obs.coord2_sec ?? DEFAULTS.coord2_sec;
            document.getElementById('obsStartDate').value = obs.start_date || '';
            document.getElementById('obsStartTime').value = obs.start_time || DEFAULTS.start_time;
            document.getElementById('obsDuration').value = obs.duration_minutes ?? DEFAULTS.duration_minutes;
            document.getElementById('obsCenterFreq').value = obs.center_freq_mhz ?? DEFAULTS.center_freq_mhz;
            document.getElementById('obsBandwidth').value = obs.bandwidth_mhz ?? DEFAULTS.bandwidth_mhz;
            document.getElementById('obsGain').value = obs.gain_db ?? DEFAULTS.gain_db;
            document.getElementById('obsChannels').value = obs.channels || DEFAULTS.channels;
            document.getElementById('obsIntegration').value = obs.integration_time_s ?? DEFAULTS.integration_time_s;
            document.getElementById('obsSdrType').value = obs.sdr_type || DEFAULTS.sdr_type;
            document.getElementById('obsCalibrator').value = obs.calibrator ? 'on' : 'off';
            document.getElementById('obsEndAction').value = obs.end_action || 'none';
            document.getElementById('obsFilename').value = obs.filename || '';
            document.getElementById('obsCalGridN').value = obs.cal_grid_n || 5;
            document.getElementById('obsCalSpacing').value = obs.cal_spacing_deg || 1.5;
            document.getElementById('obsCalInterval').value = obs.cal_interval_min || 30;
            updateCoordLabels();
            updateEndTime();
        }

        function closeModal() {
            document.getElementById('obsModal').classList.remove('active');
        }

        function autoSave() {
            // Auto-save schedule to server whenever changes are made
            fetch('/api/schedule', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(schedule)
            }).then(() => console.log('Schedule auto-saved'));
        }

        function saveObservation() {
            if (checkClash()) {
                alert('Cannot save: this observation clashes with another scheduled observation.');
                return;
            }
            const i = parseInt(document.getElementById('obsIndex').value);
            const startDate = document.getElementById('obsStartDate').value || new Date().toISOString().slice(0,10);
            const startTime = document.getElementById('obsStartTime').value;
            const duration = parseInt(document.getElementById('obsDuration').value);
            const startDt = new Date(`${startDate}T${startTime}`);
            const endDt = new Date(startDt.getTime() + duration * 60000);
            const endDate = endDt.toISOString().slice(0,10);
            const endTime = String(endDt.getHours()).padStart(2,'0') + ':' + String(endDt.getMinutes()).padStart(2,'0');
            const obs = {
                name: document.getElementById('obsName').value,
                coord_system: document.getElementById('obsCoordSystem').value,
                object_name: document.getElementById('obsObjectName').value,
                tle_text: document.getElementById('obsTleText').value,
                coord1_deg: parseInt(document.getElementById('coord1Deg').value) || 0,
                coord1_min: parseInt(document.getElementById('coord1Min').value) || 0,
                coord1_sec: parseFloat(document.getElementById('coord1Sec').value) || 0,
                coord2_deg: parseInt(document.getElementById('coord2Deg').value) || 0,
                coord2_min: parseInt(document.getElementById('coord2Min').value) || 0,
                coord2_sec: parseFloat(document.getElementById('coord2Sec').value) || 0,
                start_date: startDate,
                start_time: startTime,
                end_date: endDate,
                end_time: endTime,
                duration_minutes: duration,
                center_freq_mhz: parseFloat(document.getElementById('obsCenterFreq').value),
                bandwidth_mhz: parseFloat(document.getElementById('obsBandwidth').value),
                gain_db: parseFloat(document.getElementById('obsGain').value),
                channels: parseInt(document.getElementById('obsChannels').value),
                integration_time_s: parseFloat(document.getElementById('obsIntegration').value),
                sdr_type: document.getElementById('obsSdrType').value,
                calibrator: document.getElementById('obsCalibrator').value === 'on',
                end_action: document.getElementById('obsEndAction').value,
                filename: document.getElementById('obsFilename').value,
                cal_grid_n: parseInt(document.getElementById('obsCalGridN').value) || 5,
                cal_spacing_deg: parseFloat(document.getElementById('obsCalSpacing').value) || 1.5,
                cal_interval_min: parseInt(document.getElementById('obsCalInterval').value) || 30,
                enabled: i >= 0 ? schedule[i].enabled : true
            };
            if (i >= 0) { schedule[i] = obs; } else { schedule.push(obs); }
            closeModal();
            renderSchedule();
            autoSave();
        }

        function deleteObs(i) {
            if (isRunning(schedule[i])) {
                alert('Cannot delete while running. Stop the observation first.');
                return;
            }
            if (confirm('Delete this observation?')) {
                schedule.splice(i, 1);
                renderSchedule();
                autoSave();
            }
        }

        function toggleEnabled(i) {
            if (isRunning(schedule[i])) {
                alert('Cannot disable while running. Stop the observation first.');
                return;
            }
            schedule[i].enabled = !schedule[i].enabled;
            renderSchedule();
            autoSave();
        }

        function runNow(i) {
            fetch('/api/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(schedule[i])
            }).then(r => r.json()).then(data => {
                if (data.success) updateStatus();
                else alert('Failed: ' + (data.error || 'Unknown'));
            });
        }

        function stopObs() {
            fetch('/api/stop', {method: 'POST'}).then(() => updateStatus());
        }

        function formatRemaining(seconds) {
            if (!seconds) return '';
            const m = Math.floor(seconds / 60);
            const s = Math.floor(seconds % 60);
            return ` (${m}m ${s}s remaining)`;
        }

        let wasRunning = null;
        let soundEnabled = true;

        function playTone(freqs, duration) {
            if (!soundEnabled) return;
            try {
                const ctx = new (window.AudioContext || window.webkitAudioContext)();
                const stepDur = duration / freqs.length;
                freqs.forEach((freq, i) => {
                    const osc = ctx.createOscillator();
                    const gain = ctx.createGain();
                    osc.type = 'sine';
                    osc.frequency.value = freq;
                    gain.gain.value = 0.15;
                    osc.connect(gain);
                    gain.connect(ctx.destination);
                    osc.start(ctx.currentTime + i * stepDur);
                    osc.stop(ctx.currentTime + (i + 1) * stepDur);
                });
            } catch(e) {}
        }

        function playStartSound() { playTone([440, 554, 659], 0.4); }
        function playStopSound()  { playTone([659, 554, 440], 0.4); }

        function nextObsCountdown() {
            const now = new Date();
            let nearest = null;
            let nearestName = '';
            schedule.forEach(obs => {
                if (!obs.enabled || !obs.start_time) return;
                const date = obs.start_date || now.toISOString().slice(0,10);
                const start = new Date(`${date}T${obs.start_time}`);
                if (start > now && (!nearest || start < nearest)) {
                    nearest = start;
                    nearestName = obs.name;
                }
            });
            if (!nearest) return '';
            const diff = Math.floor((nearest - now) / 1000);
            if (diff <= 0) return '';
            const h = Math.floor(diff / 3600);
            const m = Math.floor((diff % 3600) / 60);
            const s = diff % 60;
            let t = '';
            if (h > 0) t += h + 'h ';
            t += m + 'm ' + s + 's';
            return ` \u2014 Next: ${nearestName} in ${t}`;
        }

        function updateStatus() {
            fetch('/api/status').then(r => r.json()).then(data => {
                const dot = document.getElementById('statusDot');
                const text = document.getElementById('statusText');
                const btn = document.getElementById('stopBtn');
                if (data.running) {
                    dot.classList.add('running');
                    const remaining = formatRemaining(data.remaining_seconds);
                    text.textContent = `Running: ${data.observation?.name || '?'}${remaining}`;
                    btn.style.display = 'inline-block';
                    if (wasRunning === false) playStartSound();
                    currentObs = data.observation;
                } else {
                    dot.classList.remove('running');
                    text.textContent = 'Idle' + nextObsCountdown();
                    btn.style.display = 'none';
                    if (wasRunning === true) playStopSound();
                    currentObs = null;
                }
                wasRunning = data.running;
                renderSchedule();
            });
        }

        function updateTelescope() {
            fetch('/api/telescope').then(r => r.json()).then(data => {
                const dot = document.getElementById('telescopeDot');
                const text = document.getElementById('telescopeText');
                if (!data.configured) {
                    dot.style.background = '#666';
                    text.textContent = 'Telescope: Disabled';
                } else if (!data.connected) {
                    dot.style.background = '#ff4444';
                    text.textContent = 'Telescope: Offline';
                } else {
                    const s = data.status;
                    const t = data.tracking;
                    dot.style.background = '#00ff88';
                    let info = `Alt ${s.alt.toFixed(1)}° Az ${s.az.toFixed(1)}°`;
                    if (t && t.enabled) {
                        info += ' [Tracking]';
                    }
                    text.textContent = info;
                }
            }).catch(() => {
                const dot = document.getElementById('telescopeDot');
                const text = document.getElementById('telescopeText');
                dot.style.background = '#666';
                text.textContent = 'Telescope: --';
            });
        }

        function exportSchedule() {
            const blob = new Blob([JSON.stringify(schedule, null, 2)], {type: 'application/json'});
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'h1_schedule.json';
            a.click();
        }

        function clearPast() {
            const now = new Date();
            const before = schedule.length;
            schedule = schedule.filter(obs => {
                const date = obs.start_date || now.toISOString().slice(0,10);
                const dur = obs.duration_minutes || 0;
                const start = new Date(`${date}T${obs.start_time || '00:00'}`);
                const end = new Date(start.getTime() + dur * 60000);
                return end > now;
            });
            const removed = before - schedule.length;
            if (removed > 0) {
                renderSchedule();
                autoSave();
            }
            alert(removed > 0 ? `Removed ${removed} past observation(s).` : 'No past observations to clear.');
        }

        let tleResultsData = [];

        function fetchTle() {
            const query = document.getElementById('tleSearch').value.trim();
            if (!query) { alert('Enter a satellite name or NORAD ID.'); return; }
            const info = document.getElementById('passInfo');
            info.textContent = 'Fetching from CelesTrak...';
            info.style.color = '#888';
            fetch('/api/fetch_tle', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({query: query})
            }).then(r => r.json()).then(data => {
                if (data.success && data.results.length > 0) {
                    tleResultsData = data.results;
                    if (data.results.length === 1) {
                        // Single result - use it directly
                        document.getElementById('obsTleText').value = data.results[0].tle;
                        document.getElementById('tleResults').style.display = 'none';
                        info.textContent = 'TLE fetched - click Compute Next Pass';
                        info.style.color = '#00ff88';
                    } else {
                        // Multiple results - show dropdown
                        const sel = document.getElementById('tleResultSelect');
                        sel.innerHTML = data.results.map((r, i) =>
                            `<option value="${i}">${r.name}</option>`
                        ).join('');
                        document.getElementById('tleResults').style.display = '';
                        selectTleResult();
                        info.textContent = data.results.length + ' satellites found - select one';
                        info.style.color = '#00d4ff';
                    }
                } else {
                    document.getElementById('tleResults').style.display = 'none';
                    info.textContent = data.error || 'Not found';
                    info.style.color = '#ff4757';
                }
            }).catch(e => {
                info.textContent = 'Error: ' + e;
                info.style.color = '#ff4757';
            });
        }

        function selectTleResult() {
            const idx = parseInt(document.getElementById('tleResultSelect').value);
            if (tleResultsData[idx]) {
                document.getElementById('obsTleText').value = tleResultsData[idx].tle;
            }
        }

        function predictPass() {
            const tle = document.getElementById('obsTleText').value.trim();
            if (!tle) { alert('Paste or load a TLE first.'); return; }
            const info = document.getElementById('passInfo');
            info.textContent = 'Computing...';
            info.style.color = '#888';
            fetch('/api/predict_pass', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({tle_text: tle})
            }).then(r => r.json()).then(data => {
                if (data.success) {
                    const p = data.pass;
                    // Auto-fill schedule fields
                    document.getElementById('obsStartDate').value = p.start_date;
                    document.getElementById('obsStartTime').value = p.start_time;
                    document.getElementById('obsDuration').value = Math.ceil(p.duration_minutes);
                    document.getElementById('obsName').value = document.getElementById('obsName').value || p.name;
                    updateEndTime();
                    info.textContent = 'Pass found!';
                    info.style.color = '#00ff88';
                    document.getElementById('passDetails').style.display = 'block';
                    document.getElementById('passDetails').innerHTML =
                        `<b>${p.name}</b><br>` +
                        `Rise: ${p.rise_time_local} (Az ${p.rise_az}\\u00b0)<br>` +
                        `Max:  ${p.max_time_utc} UTC (El ${p.max_el}\\u00b0)<br>` +
                        `Set:  ${p.set_time_local} (Az ${p.set_az}\\u00b0)<br>` +
                        `Duration: ${p.duration_minutes} min`;
                } else {
                    info.textContent = data.error || 'No pass found';
                    info.style.color = '#ff4757';
                    document.getElementById('passDetails').style.display = 'none';
                }
            }).catch(e => {
                info.textContent = 'Error: ' + e;
                info.style.color = '#ff4757';
            });
        }

        function loadTleFile(e) {
            const file = e.target.files[0];
            if (file) {
                const reader = new FileReader();
                reader.onload = ev => {
                    document.getElementById('obsTleText').value = ev.target.result.trim();
                    document.getElementById('passInfo').textContent = 'TLE loaded from file';
                    document.getElementById('passInfo').style.color = '#00d4ff';
                };
                reader.readAsText(file);
            }
            e.target.value = '';
        }

        function loadFile(e) {
            const file = e.target.files[0];
            if (file) {
                const reader = new FileReader();
                reader.onload = ev => {
                    try {
                        schedule = JSON.parse(ev.target.result);
                        renderSchedule();
                        alert('Loaded!');
                    } catch { alert('Invalid JSON'); }
                };
                reader.readAsText(file);
            }
            e.target.value = '';
        }

        // ---- Tabs ----
        function switchTab(name) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            document.querySelector(`.tab[onclick="switchTab('${name}')"]`).classList.add('active');
            document.getElementById('tab-' + name).classList.add('active');
            if (name === 'config') loadConfig();
            if (name === 'log') loadLog();
            if (name === 'sunscan') { pollSunScan(); loadCalModel(); }
        }

        // ---- Sun Scan ----
        let ssPollTimer = null;

        function startSunScan() {
            const params = {
                n: parseInt(document.getElementById('ssGridN').value),
                grid_spacing_deg: parseFloat(document.getElementById('ssSpacing').value),
                integration_time_s: parseFloat(document.getElementById('ssIntegration').value),
                center_freq_mhz: parseFloat(document.getElementById('ssCenterFreq').value),
                bandwidth_mhz: parseFloat(document.getElementById('ssBandwidth').value),
                gain_db: parseFloat(document.getElementById('ssGain').value),
                sdr_type: document.getElementById('ssSdrType').value,
                beam_fwhm_deg: parseFloat(document.getElementById('ssBeamFwhm').value),
            };
            fetch('/api/sunscan/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(params)
            }).then(r => r.json()).then(data => {
                if (data.success) {
                    document.getElementById('ssStartBtn').style.display = 'none';
                    document.getElementById('ssStopBtn').style.display = 'inline-block';
                    document.getElementById('ssProgress').style.display = 'block';
                    document.getElementById('ssStatus').innerHTML = '<span style="color:#00d4ff;">Starting sun scan...</span>';
                    document.getElementById('ssImageContainer').innerHTML = '';
                    ssPollTimer = setInterval(pollSunScan, 2000);
                } else {
                    document.getElementById('ssStatus').innerHTML = '<span style="color:#ff4757;">Error: ' + (data.error || 'Unknown') + '</span>';
                }
            });
        }

        function stopSunScan() {
            fetch('/api/sunscan/stop', {method: 'POST'}).then(r => r.json()).then(data => {
                document.getElementById('ssStatus').innerHTML = '<span style="color:#ff4757;">Scan stopped.</span>';
            });
        }

        function pollSunScan() {
            fetch('/api/sunscan/status').then(r => r.json()).then(data => {
                if (data.running) {
                    document.getElementById('ssStartBtn').style.display = 'none';
                    document.getElementById('ssStopBtn').style.display = 'inline-block';
                    document.getElementById('ssProgress').style.display = 'block';
                    const pct = data.total > 0 ? (data.progress / data.total * 100) : 0;
                    document.getElementById('ssProgressBar').style.width = pct + '%';
                    document.getElementById('ssProgressText').textContent = data.progress + ' / ' + data.total + ' grid points';
                    let info = '<span style="color:#00d4ff;">Scanning...</span>';
                    if (data.point_info) {
                        info += '<br><span style="color:#ccc; font-size:13px;">'
                            + 'Point ' + data.point_info.point + '/' + data.point_info.total
                            + ' &mdash; offset (' + data.point_info.dalt.toFixed(1) + ', ' + data.point_info.daz_sky.toFixed(1) + ')&deg;'
                            + ' &rarr; Alt=' + data.point_info.cmd_alt.toFixed(1) + '&deg; Az=' + data.point_info.cmd_az.toFixed(1) + '&deg;'
                            + '</span>';
                    }
                    document.getElementById('ssStatus').innerHTML = info;
                } else {
                    // Scan finished or idle
                    document.getElementById('ssStartBtn').style.display = 'inline-block';
                    document.getElementById('ssStopBtn').style.display = 'none';
                    if (ssPollTimer) { clearInterval(ssPollTimer); ssPollTimer = null; }

                    if (data.error) {
                        document.getElementById('ssStatus').innerHTML = '<span style="color:#ff4757;">Error: ' + data.error + '</span>';
                        document.getElementById('ssProgress').style.display = 'none';
                    } else if (data.result) {
                        const r = data.result;
                        document.getElementById('ssProgress').style.display = 'none';
                        document.getElementById('ssStatus').innerHTML = '<span style="color:#00ff88;">Scan complete!</span>';
                        let html = '<table style="width:100%; font-size:13px; color:#ccc;">';
                        html += '<tr><td style="color:#888; padding:4px 8px;">Sun Position</td><td>Alt ' + r.sun_alt_deg.toFixed(2) + '&deg; &nbsp; Az ' + r.sun_az_deg.toFixed(2) + '&deg;</td></tr>';
                        html += '<tr><td style="color:#888; padding:4px 8px;">Pointing Error</td><td style="color:#00d4ff; font-size:16px; font-weight:bold;">&Delta;Alt = ' + (r.alt_error_deg >= 0 ? '+' : '') + r.alt_error_deg.toFixed(3) + '&deg; &nbsp; &Delta;Az = ' + (r.az_error_deg >= 0 ? '+' : '') + r.az_error_deg.toFixed(3) + '&deg;</td></tr>';
                        html += '<tr><td style="color:#888; padding:4px 8px;">Beam FWHM</td><td>' + r.beam_fwhm_deg.toFixed(2) + '&deg;</td></tr>';
                        html += '<tr><td style="color:#888; padding:4px 8px;">Fit Success</td><td>' + (r.fit.success ? '<span style="color:#00ff88;">Yes</span>' : '<span style="color:#ff4757;">No (peak pixel fallback)</span>') + '</td></tr>';
                        if (r.fit.fit_errors) {
                            html += '<tr><td style="color:#888; padding:4px 8px;">Fit Uncertainty</td><td>&plusmn;' + r.fit.fit_errors.alt_err.toFixed(3) + '&deg; alt, &plusmn;' + r.fit.fit_errors.az_err.toFixed(3) + '&deg; az</td></tr>';
                        }
                        html += '<tr><td style="color:#888; padding:4px 8px;">Grid</td><td>' + r.n + '&times;' + r.n + ' @ ' + r.grid_spacing_deg + '&deg; spacing</td></tr>';
                        html += '<tr><td style="color:#888; padding:4px 8px;">Integration</td><td>' + r.integration_time_s + 's per point</td></tr>';
                        html += '<tr><td style="color:#888; padding:4px 8px;">Timestamp</td><td>' + r.timestamp + '</td></tr>';
                        html += '</table>';
                        document.getElementById('ssResults').innerHTML = html;

                        if (data.has_image) {
                            document.getElementById('ssImageContainer').innerHTML =
                                '<img src="/api/sunscan/image?' + Date.now() + '" style="max-width:100%; border-radius:8px; border:1px solid #333; margin-top:10px;">';
                        }
                    } else {
                        document.getElementById('ssStatus').innerHTML = '<span style="color:#888;">Idle &mdash; configure parameters and click Start.</span>';
                    }
                }
            });
        }

        // ---- Calibration Day ----
        let cdPollTimer = null;

        function startCalDay() {
            const params = {
                n: parseInt(document.getElementById('ssGridN').value),
                grid_spacing_deg: parseFloat(document.getElementById('ssSpacing').value),
                integration_time_s: parseFloat(document.getElementById('ssIntegration').value),
                center_freq_mhz: parseFloat(document.getElementById('ssCenterFreq').value),
                bandwidth_mhz: parseFloat(document.getElementById('ssBandwidth').value),
                gain_db: parseFloat(document.getElementById('ssGain').value),
                sdr_type: document.getElementById('ssSdrType').value,
                beam_fwhm_deg: parseFloat(document.getElementById('ssBeamFwhm').value),
                interval_minutes: parseInt(document.getElementById('cdInterval').value),
            };
            fetch('/api/calday/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(params)
            }).then(r => r.json()).then(data => {
                if (data.success) {
                    document.getElementById('cdStartBtn').style.display = 'none';
                    document.getElementById('cdStopBtn').style.display = 'inline-block';
                    cdPollTimer = setInterval(pollCalDay, 3000);
                    pollCalDay();
                } else {
                    document.getElementById('cdStatus').innerHTML = '<span style="color:#ff4757;">Error: ' + (data.error || 'Unknown') + '</span>';
                }
            });
        }

        function stopCalDay() {
            fetch('/api/calday/stop', {method: 'POST'}).then(() => pollCalDay());
        }

        function pollCalDay() {
            fetch('/api/calday/status').then(r => r.json()).then(data => {
                if (data.running) {
                    document.getElementById('cdStartBtn').style.display = 'none';
                    document.getElementById('cdStopBtn').style.display = 'inline-block';
                    let info = '<span style="color:#00d4ff;">Running</span> &mdash; ';
                    info += data.scans_completed + ' scans completed';
                    if (data.scan_running) {
                        info += '<br><span style="color:#ccc;">Scan in progress (' + data.scan_progress + '/' + data.scan_total + ' points)</span>';
                    } else if (data.next_scan_time) {
                        const next = new Date(data.next_scan_time).toLocaleTimeString();
                        info += '<br><span style="color:#888;">Next scan at ' + next + '</span>';
                    }
                    document.getElementById('cdStatus').innerHTML = info;
                } else {
                    document.getElementById('cdStartBtn').style.display = 'inline-block';
                    document.getElementById('cdStopBtn').style.display = 'none';
                    if (cdPollTimer) { clearInterval(cdPollTimer); cdPollTimer = null; }
                    let info = '<span style="color:#888;">Idle</span>';
                    if (data.scans_completed > 0) {
                        info += ' &mdash; ' + data.scans_completed + ' scans collected';
                    }
                    if (data.error) {
                        info += '<br><span style="color:#ff4757;">' + data.error + '</span>';
                    }
                    document.getElementById('cdStatus').innerHTML = info;
                }
            });
        }

        function fitModel() {
            document.getElementById('cdModel').innerHTML = '<span style="color:#888;">Fitting model...</span>';
            fetch('/api/calday/fit', {method: 'POST'}).then(r => r.json()).then(m => {
                if (m.success) {
                    let html = '<table style="width:100%; font-size:13px; color:#ccc;">';
                    html += '<tr><td style="color:#888; padding:4px 8px;">Scans used</td><td>' + m.n_scans + '</td></tr>';
                    html += '<tr><td style="color:#888; padding:4px 8px;">Alt zero offset</td><td>' + (m.alt_offset_deg >= 0 ? '+' : '') + m.alt_offset_deg.toFixed(3) + '&deg;</td></tr>';
                    html += '<tr><td style="color:#888; padding:4px 8px;">Az zero offset</td><td>' + (m.az_offset_deg >= 0 ? '+' : '') + m.az_offset_deg.toFixed(3) + '&deg;</td></tr>';
                    html += '<tr><td style="color:#888; padding:4px 8px;">N-S tilt (AN)</td><td>' + (m.tilt_north_deg >= 0 ? '+' : '') + m.tilt_north_deg.toFixed(3) + '&deg;</td></tr>';
                    html += '<tr><td style="color:#888; padding:4px 8px;">E-W tilt (AE)</td><td>' + (m.tilt_east_deg >= 0 ? '+' : '') + m.tilt_east_deg.toFixed(3) + '&deg;</td></tr>';
                    html += '<tr><td style="color:#888; padding:4px 8px;">RMS residual</td><td>alt ' + m.rms_alt_deg.toFixed(3) + '&deg;, az ' + m.rms_az_deg.toFixed(3) + '&deg;</td></tr>';
                    if (m.effective_lat !== undefined) {
                        html += '<tr><td style="color:#888; padding:4px 8px;">Effective lat</td><td style="color:#00d4ff; font-weight:bold;">' + m.effective_lat.toFixed(6) + '&deg;</td></tr>';
                        html += '<tr><td style="color:#888; padding:4px 8px;">Effective lon</td><td style="color:#00d4ff; font-weight:bold;">' + m.effective_lon.toFixed(6) + '&deg;</td></tr>';
                    }
                    html += '</table>';
                    document.getElementById('cdModel').innerHTML = html;
                    document.getElementById('cdApplyBtn').style.display = 'inline-block';
                    // Show plot
                    document.getElementById('cdPlotContainer').innerHTML =
                        '<img src="/api/calday/plot?' + Date.now() + '" style="max-width:100%; border-radius:8px; border:1px solid #333; margin-top:10px;">';
                } else {
                    document.getElementById('cdModel').innerHTML = '<span style="color:#ff4757;">' + (m.error || 'Fit failed') + '</span>';
                }
            });
        }

        function clearCalData() {
            if (!confirm('Clear all accumulated pointing data?')) return;
            fetch('/api/calday/clear', {method: 'POST'}).then(() => {
                document.getElementById('cdModel').innerHTML = '<span style="color:#888;">No model fitted yet.</span>';
                document.getElementById('cdApplyBtn').style.display = 'none';
                document.getElementById('cdPlotContainer').innerHTML = '';
                document.getElementById('cdStatus').innerHTML = '<span style="color:#888;">Data cleared.</span>';
            });
        }

        function applyModel() {
            if (!confirm('Apply the effective lat/lon to the ESP32 controller and scheduler config?')) return;
            fetch('/api/calday/apply', {method: 'POST'}).then(r => r.json()).then(data => {
                if (data.success) {
                    alert('Applied!\\nEffective lat: ' + data.effective_lat.toFixed(6) + '\\nEffective lon: ' + data.effective_lon.toFixed(6));
                } else {
                    alert('Failed: ' + (data.error || 'Unknown error'));
                }
            });
        }

        // Load existing model on tab open
        function loadCalModel() {
            fetch('/api/calday/model').then(r => r.json()).then(m => {
                if (m.success) {
                    fitModel();  // re-render
                }
            });
            fetch('/api/calday/data').then(r => r.json()).then(d => {
                if (d.data && d.data.length > 0) {
                    document.getElementById('cdStatus').innerHTML = '<span style="color:#888;">Idle &mdash; ' + d.data.length + ' scans collected</span>';
                }
            });
        }

        // ---- Configuration ----
        function loadConfig() {
            fetch('/api/config').then(r => r.json()).then(cfg => {
                document.getElementById('cfgBannerName').value = cfg.banner_name || '';
                document.getElementById('cfgBannerSubtitle').value = cfg.banner_subtitle || '';
                document.getElementById('cfgControllerUrl').value = cfg.srt_controller_url || '';
                document.getElementById('cfgSlewTimeout').value = cfg.slew_timeout || 300;
                document.getElementById('cfgPositionTolerance').value = cfg.position_tolerance || 0.5;
                document.getElementById('cfgObsLat').value = cfg.observer_lat ?? 55.9;
                document.getElementById('cfgObsLon').value = cfg.observer_lon ?? -4.3;
                document.getElementById('cfgObsElev').value = cfg.observer_elevation ?? 50;
                document.getElementById('cfgMinElev').value = cfg.min_elevation ?? 10;
                document.getElementById('cfgPythonPath').value = cfg.python_path || '';
                document.getElementById('cfgDataFolder').value = cfg.data_output_folder || '';
                document.getElementById('cfgLogLines').value = cfg.log_lines || 100;
                document.getElementById('cfgSoundEnabled').value = cfg.sound_enabled !== false ? 'true' : 'false';
                soundEnabled = cfg.sound_enabled !== false;
            });
        }

        function saveConfig() {
            const cfg = {
                banner_name: document.getElementById('cfgBannerName').value,
                banner_subtitle: document.getElementById('cfgBannerSubtitle').value,
                srt_controller_url: document.getElementById('cfgControllerUrl').value,
                slew_timeout: parseInt(document.getElementById('cfgSlewTimeout').value) || 300,
                position_tolerance: parseFloat(document.getElementById('cfgPositionTolerance').value) || 0.5,
                observer_lat: parseFloat(document.getElementById('cfgObsLat').value) || 0,
                observer_lon: parseFloat(document.getElementById('cfgObsLon').value) || 0,
                observer_elevation: parseFloat(document.getElementById('cfgObsElev').value) || 0,
                min_elevation: parseFloat(document.getElementById('cfgMinElev').value) || 10,
                python_path: document.getElementById('cfgPythonPath').value,
                data_output_folder: document.getElementById('cfgDataFolder').value,
                log_lines: parseInt(document.getElementById('cfgLogLines').value) || 100,
                sound_enabled: document.getElementById('cfgSoundEnabled').value === 'true',
            };
            soundEnabled = cfg.sound_enabled;
            fetch('/api/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(cfg)
            }).then(r => r.json()).then(data => {
                if (data.success) {
                    const el = document.getElementById('configSaved');
                    el.style.display = 'inline';
                    setTimeout(() => el.style.display = 'none', 3000);
                    updateTelescope();
                } else {
                    alert('Error saving: ' + (data.error || 'Unknown'));
                }
            });
        }

        // ---- Log ----
        let logRefreshTimer = null;

        function loadLog() {
            fetch('/api/log').then(r => r.json()).then(data => {
                const el = document.getElementById('logContent');
                el.textContent = data.lines.join('\\n') || '(empty log)';
                el.scrollTop = el.scrollHeight;
            }).catch(() => {
                document.getElementById('logContent').textContent = 'Error loading log';
            });
        }

        function toggleLogRefresh() {
            if (document.getElementById('logAutoRefresh').checked) {
                logRefreshTimer = setInterval(loadLog, 5000);
            } else {
                clearInterval(logRefreshTimer);
                logRefreshTimer = null;
            }
        }

        // Start log auto-refresh
        logRefreshTimer = setInterval(loadLog, 5000);
    </script>
</body>
</html>
'''


# =============================================================================
# Sun Scan Integration
# =============================================================================

def _sun_scan_progress(idx, total, info):
    """Progress callback for sun_scan — updates global state."""
    sun_scan_state["progress"] = idx + 1
    sun_scan_state["total"] = total
    sun_scan_state["point_info"] = info


def _run_sun_scan(params: dict):
    """Run sun scan in a background thread."""
    global sun_scan_state
    sun_scan_state.update(running=True, progress=0, total=0,
                          point_info=None, result=None, error=None,
                          image_path=None)
    try:
        from sun_scan import sun_scan as do_sun_scan

        cfg = load_config()
        data_folder = get_config_value("data_output_folder")
        os.makedirs(data_folder, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        image_path = os.path.join(data_folder, f"sun_scan_{timestamp}.png")

        result = do_sun_scan(
            n=params.get("n", 5),
            grid_spacing_deg=params.get("grid_spacing_deg", 1.5),
            integration_time_s=params.get("integration_time_s", 3.0),
            srt_url=cfg.get("srt_controller_url") or None,
            lat=cfg.get("observer_lat"),
            lon=cfg.get("observer_lon"),
            elevation=cfg.get("observer_elevation", 50),
            sdr_type=params.get("sdr_type", "b210"),
            center_freq=params.get("center_freq_mhz", 1420.405) * 1e6,
            sample_rate=params.get("bandwidth_mhz", 2.4) * 1e6,
            gain=params.get("gain_db", 40.0),
            output_image=image_path,
            slew_timeout=cfg.get("slew_timeout", 300),
            beam_fwhm_deg=params.get("beam_fwhm_deg", 3.0),
            progress_callback=_sun_scan_progress,
            cancel_event=sun_scan_cancel,
        )

        # Convert numpy array to list for JSON serialisation
        result["power_grid"] = result["power_grid"].tolist()
        sun_scan_state["result"] = result
        sun_scan_state["image_path"] = image_path
        log.info("Sun scan complete: dAlt=%+.3f° dAz=%+.3f°",
                 result["alt_error_deg"], result["az_error_deg"])
    except Exception as exc:
        log.error("Sun scan failed: %s", exc)
        sun_scan_state["error"] = str(exc)
    finally:
        sun_scan_state["running"] = False


def _run_calibration_day(params: dict):
    """Run repeated sun scans at a fixed interval until sunset or cancelled."""
    from sun_scan import (sun_scan as do_sun_scan, get_sun_altaz,
                          save_scan_to_pointing_data)

    interval = params.get("interval_minutes", 30)
    cal_day_state.update(running=True, scans_completed=0, error=None,
                         interval_minutes=interval, next_scan_time=None)
    cal_day_cancel.clear()

    try:
        while not cal_day_cancel.is_set():
            cfg = load_config()
            lat = cfg.get("observer_lat", 55.9)
            lon = cfg.get("observer_lon", -4.3)
            elev = cfg.get("observer_elevation", 50)

            # Check sun is up — wait for sunrise if not
            sun_alt, sun_az = get_sun_altaz(lat, lon, elev)
            if sun_alt < 5.0:
                # If sun has already been up and set, we're done for the day
                if cal_day_state["scans_completed"] > 0:
                    log.info("Calibration day: sun has set (%.1f°), finishing",
                             sun_alt)
                    break
                # Otherwise wait for sunrise
                log.info("Calibration day: waiting for sun to rise (alt=%.1f°)",
                         sun_alt)
                cal_day_state["error"] = None
                while sun_alt < 5.0:
                    if cal_day_cancel.is_set():
                        return
                    time.sleep(30)
                    sun_alt, _ = get_sun_altaz(lat, lon, elev)
                log.info("Calibration day: sun is up (alt=%.1f°), starting scans",
                         sun_alt)
                continue

            # Wait for any running single scan to finish
            while sun_scan_state["running"]:
                if cal_day_cancel.is_set():
                    return
                time.sleep(1)

            # Run a scan
            log.info("Calibration day: starting scan %d",
                     cal_day_state["scans_completed"] + 1)
            sun_scan_cancel.clear()
            _run_sun_scan(params)

            # Save result to pointing data
            if sun_scan_state.get("result") and not sun_scan_state.get("error"):
                save_scan_to_pointing_data(sun_scan_state["result"])
                cal_day_state["scans_completed"] += 1
                log.info("Calibration day: scan %d complete",
                         cal_day_state["scans_completed"])

            # Wait for next interval
            next_time = datetime.now() + timedelta(minutes=interval)
            cal_day_state["next_scan_time"] = next_time.isoformat()
            log.info("Calibration day: next scan at %s",
                     next_time.strftime("%H:%M:%S"))

            while datetime.now() < next_time:
                if cal_day_cancel.is_set():
                    return
                time.sleep(5)

    except Exception as exc:
        log.error("Calibration day error: %s", exc)
        cal_day_state["error"] = str(exc)
    finally:
        cal_day_state["running"] = False
        cal_day_state["next_scan_time"] = None
        log.info("Calibration day ended (%d scans)",
                 cal_day_state["scans_completed"])


@app.route('/')
def index():
    cfg = load_config()
    return render_template_string(HTML_TEMPLATE,
        banner_name=cfg.get('banner_name', _DEFAULT_CONFIG['banner_name']),
        banner_subtitle=cfg.get('banner_subtitle', _DEFAULT_CONFIG['banner_subtitle']))


@app.route('/api/schedule', methods=['GET'])
def get_schedule():
    return jsonify(load_schedule())


@app.route('/api/schedule', methods=['POST'])
def post_schedule():
    schedule = request.json
    # Server-side clash validation
    clashes = find_clashes(schedule)
    if clashes:
        return jsonify({'success': False, 'error': f'Schedule has clashing observations: {clashes}'}), 400
    save_schedule(schedule)
    return jsonify({'success': True})


@app.route('/api/status', methods=['GET'])
def get_status():
    with process_lock:
        running = current_process is not None and current_process.poll() is None
    # Also count calibration day as running
    if not running and current_observation and current_observation.get('coord_system') == 'calibration':
        running = cal_day_state["running"]
    remaining = None
    if running and observation_end_time:
        remaining = max(0, (observation_end_time - datetime.now()).total_seconds())
    return jsonify({
        'running': running,
        'observation': current_observation if running else None,
        'remaining_seconds': remaining
    })


@app.route('/api/start', methods=['POST'])
def api_start():
    obs = request.json
    success = start_observation(obs)
    return jsonify({'success': success, 'error': None if success else 'Failed to start or already running'})


@app.route('/api/stop', methods=['POST'])
def api_stop():
    success = stop_observation()
    return jsonify({'success': success})


@app.route('/api/telescope', methods=['GET'])
def api_telescope():
    """Get telescope (SRT controller) status."""
    if not SRT_CONTROLLER_URL:
        return jsonify({
            'configured': False,
            'url': None,
            'connected': False,
            'status': None,
            'tracking': None
        })

    status = srt_get_status()
    tracking = srt_get_tracking()

    return jsonify({
        'configured': True,
        'url': SRT_CONTROLLER_URL,
        'connected': status is not None,
        'status': status,
        'tracking': tracking
    })


@app.route('/api/config', methods=['GET'])
def api_get_config():
    return jsonify(load_config())


@app.route('/api/config', methods=['POST'])
def api_post_config():
    global SRT_CONTROLLER_URL, SRT_SLEW_TIMEOUT, SRT_POSITION_TOLERANCE, PYTHON_PATH
    cfg = request.json
    save_config(cfg)
    # Apply to running process
    SRT_CONTROLLER_URL = cfg.get("srt_controller_url") or None
    SRT_SLEW_TIMEOUT = cfg.get("slew_timeout", 300)
    SRT_POSITION_TOLERANCE = cfg.get("position_tolerance", 0.5)
    PYTHON_PATH = cfg.get("python_path") or None
    # Re-sync observer location if controller URL changed
    sync_observer_from_controller()
    return jsonify({'success': True})


@app.route('/api/fetch_tle', methods=['POST'])
def api_fetch_tle():
    """Fetch TLE from CelesTrak by name or NORAD ID."""
    data = request.json
    query = data.get('query', '').strip()
    if not query:
        return jsonify({'success': False, 'error': 'No search query'}), 400

    # Try NORAD ID (numeric) or name search
    if query.isdigit():
        url = f"https://celestrak.org/NORAD/elements/gp.php?CATNR={query}&FORMAT=TLE"
    else:
        url = f"https://celestrak.org/NORAD/elements/gp.php?NAME={urllib.parse.quote(query)}&FORMAT=TLE"

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'H1-Scheduler/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode().strip()

        if not body or 'No GP data found' in body:
            return jsonify({'success': False, 'error': f'No TLE found for "{query}"'})

        # Parse all returned TLEs into a list
        lines = body.splitlines()
        results = []
        i = 0
        while i < len(lines):
            if i + 2 < len(lines) and lines[i+1].startswith('1 ') and lines[i+2].startswith('2 '):
                results.append({
                    'name': lines[i].strip(),
                    'tle': lines[i] + '\n' + lines[i+1] + '\n' + lines[i+2]
                })
                i += 3
            else:
                i += 1

        if not results:
            return jsonify({'success': False, 'error': 'Unexpected TLE format'})

        return jsonify({'success': True, 'results': results})
    except urllib.error.URLError as e:
        return jsonify({'success': False, 'error': f'Network error: {e}'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/predict_pass', methods=['POST'])
def api_predict_pass():
    """Predict next satellite pass from TLE."""
    if not EPHEM_AVAILABLE:
        return jsonify({'success': False, 'error': 'PyEphem not installed'}), 500
    data = request.json
    tle_text = data.get('tle_text', '')
    if not tle_text:
        return jsonify({'success': False, 'error': 'No TLE data provided'}), 400
    try:
        result = predict_next_pass(tle_text)
        if result:
            return jsonify({'success': True, 'pass': result})
        else:
            min_el = get_config_value("min_elevation")
            return jsonify({'success': False, 'error': f'No pass above {min_el}° found in next 24h'})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/log', methods=['GET'])
def api_get_log():
    n = get_config_value("log_lines") or 100
    try:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()
        lines = [l.rstrip() for l in all_lines[-n:]]
    except FileNotFoundError:
        lines = ["(no log file yet)"]
    except Exception as e:
        lines = [f"Error reading log: {e}"]
    return jsonify({'lines': lines})


# =============================================================================
# Sun Scan API
# =============================================================================

@app.route('/api/sunscan/start', methods=['POST'])
def api_sunscan_start():
    global sun_scan_thread
    if sun_scan_state["running"]:
        return jsonify({'success': False, 'error': 'Scan already running'})

    # Check receiver is not in use
    with process_lock:
        if current_process is not None and current_process.poll() is None:
            return jsonify({'success': False,
                            'error': 'Receiver is busy with an observation'})

    params = request.json or {}
    sun_scan_cancel.clear()
    sun_scan_thread = threading.Thread(target=_run_sun_scan, args=(params,),
                                       daemon=True)
    sun_scan_thread.start()
    return jsonify({'success': True})


@app.route('/api/sunscan/stop', methods=['POST'])
def api_sunscan_stop():
    sun_scan_cancel.set()
    return jsonify({'success': True})


@app.route('/api/sunscan/status', methods=['GET'])
def api_sunscan_status():
    return jsonify({
        'running': sun_scan_state["running"],
        'progress': sun_scan_state["progress"],
        'total': sun_scan_state["total"],
        'point_info': sun_scan_state["point_info"],
        'result': sun_scan_state["result"],
        'error': sun_scan_state["error"],
        'has_image': sun_scan_state["image_path"] is not None
                     and os.path.isfile(sun_scan_state["image_path"]),
    })


@app.route('/api/sunscan/image', methods=['GET'])
def api_sunscan_image():
    img = sun_scan_state.get("image_path")
    if img and os.path.isfile(img):
        from flask import send_file
        return send_file(img, mimetype='image/png')
    return ('', 404)


# =============================================================================
# Calibration Day API
# =============================================================================

@app.route('/api/calday/start', methods=['POST'])
def api_calday_start():
    global cal_day_thread
    if cal_day_state["running"]:
        return jsonify({'success': False, 'error': 'Calibration day already running'})
    with process_lock:
        if current_process is not None and current_process.poll() is None:
            return jsonify({'success': False,
                            'error': 'Receiver is busy with an observation'})
    params = request.json or {}
    cal_day_cancel.clear()
    cal_day_thread = threading.Thread(target=_run_calibration_day, args=(params,),
                                      daemon=True)
    cal_day_thread.start()
    return jsonify({'success': True})


@app.route('/api/calday/stop', methods=['POST'])
def api_calday_stop():
    cal_day_cancel.set()
    sun_scan_cancel.set()  # also stop any in-progress scan
    return jsonify({'success': True})


@app.route('/api/calday/status', methods=['GET'])
def api_calday_status():
    return jsonify({
        'running': cal_day_state["running"],
        'scans_completed': cal_day_state["scans_completed"],
        'next_scan_time': cal_day_state["next_scan_time"],
        'interval_minutes': cal_day_state["interval_minutes"],
        'error': cal_day_state["error"],
        'scan_running': sun_scan_state["running"],
        'scan_progress': sun_scan_state["progress"],
        'scan_total': sun_scan_state["total"],
    })


@app.route('/api/calday/data', methods=['GET'])
def api_calday_data():
    from sun_scan import load_pointing_data
    return jsonify({'data': load_pointing_data()})


@app.route('/api/calday/clear', methods=['POST'])
def api_calday_clear():
    from sun_scan import clear_pointing_data
    clear_pointing_data()
    return jsonify({'success': True})


@app.route('/api/calday/fit', methods=['POST'])
def api_calday_fit():
    from sun_scan import fit_pointing_model, save_pointing_model, generate_calibration_plot
    cfg = load_config()
    model = fit_pointing_model(
        true_lat=cfg.get("observer_lat"),
        true_lon=cfg.get("observer_lon"),
    )
    if model.get("success"):
        save_pointing_model(model)
        data_folder = get_config_value("data_output_folder")
        os.makedirs(data_folder, exist_ok=True)
        plot_path = os.path.join(data_folder, "calibration_day.png")
        try:
            generate_calibration_plot(model, plot_path)
            model["plot_path"] = plot_path
        except Exception as exc:
            log.warning("Could not generate calibration plot: %s", exc)
    return jsonify(model)


@app.route('/api/calday/plot', methods=['GET'])
def api_calday_plot():
    data_folder = get_config_value("data_output_folder")
    plot_path = os.path.join(data_folder, "calibration_day.png")
    if os.path.isfile(plot_path):
        from flask import send_file
        return send_file(plot_path, mimetype='image/png')
    return ('', 404)


@app.route('/api/calday/apply', methods=['POST'])
def api_calday_apply():
    """Apply the fitted pointing model to the ESP32 controller settings."""
    from sun_scan import load_pointing_model
    model = load_pointing_model()
    if not model or not model.get("success"):
        return jsonify({'success': False, 'error': 'No valid pointing model to apply'})

    eff_lat = model.get("effective_lat")
    eff_lon = model.get("effective_lon")
    if eff_lat is None or eff_lon is None:
        return jsonify({'success': False, 'error': 'Model has no effective lat/lon'})

    # Update ESP32 controller
    if SRT_CONTROLLER_URL:
        result = srt_api_call("/settings/save", {
            "observer_lat": f"{eff_lat:.6f}",
            "observer_lon": f"{eff_lon:.6f}",
        })
        if not (result and result.get("ok")):
            return jsonify({'success': False,
                            'error': f'Failed to update ESP32: {result}'})
        log.info("Applied effective lat=%.6f lon=%.6f to ESP32", eff_lat, eff_lon)

    # Update scheduler config too
    cfg = load_config()
    cfg["observer_lat"] = eff_lat
    cfg["observer_lon"] = eff_lon
    save_config(cfg)
    log.info("Updated scheduler config with effective lat/lon")

    return jsonify({
        'success': True,
        'effective_lat': eff_lat,
        'effective_lon': eff_lon,
        'alt_offset_deg': model["alt_offset_deg"],
        'az_offset_deg': model["az_offset_deg"],
    })


@app.route('/api/calday/model', methods=['GET'])
def api_calday_model():
    from sun_scan import load_pointing_model
    model = load_pointing_model()
    return jsonify(model or {'success': False, 'error': 'No model fitted yet'})


def main():
    global scheduler_running

    import argparse
    parser = argparse.ArgumentParser(description='H1 Receiver Web Scheduler')
    parser.add_argument('--port', type=int, default=5000, help='Port to run on')
    parser.add_argument('--host', default='127.0.0.1', help='Host to bind to')
    args = parser.parse_args()

    # Check if gnuradio is available
    try:
        import gnuradio
        gr_status = "OK"
    except ImportError:
        gr_status = "NOT FOUND - run from radioconda environment!"

    print(f"\n{'='*50}")
    print("H1 Receiver Web Scheduler")
    print(f"{'='*50}")
    print(f"GNU Radio: {gr_status}")
    print(f"Python: {sys.executable}")
    if SRT_CONTROLLER_URL:
        print(f"SRT Controller: {SRT_CONTROLLER_URL}")
    else:
        print("SRT Controller: DISABLED (telescope control off)")
    # Sync observer location from controller
    sync_observer_from_controller()

    print(f"\nOpen your browser to: http://{args.host}:{args.port}")
    print("Scheduler is ACTIVE - observations will start automatically")
    print("\nPress Ctrl+C to stop\n")

    # Suppress Flask request logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)

    # Start background scheduler thread
    sched_thread = threading.Thread(target=scheduler_thread, daemon=True)
    sched_thread.start()

    try:
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        scheduler_running = False
        if current_process:
            stop_observation()


if __name__ == '__main__':
    main()
