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
import urllib.request
import urllib.error
import urllib.parse

# Configuration
SCHEDULE_FILE = "h1_schedule.json"
RECEIVER_SCRIPT = "b210_h1_receiver.py"
# Set PYTHON_PATH to use a specific Python (e.g., radioconda), or None to use sys.executable
PYTHON_PATH = None  # e.g., r"C:\Users\graha\radioconda\python.exe"

# SRT Controller (ESP32) configuration
# Set to None to disable telescope control, or the URL of the ESP32 controller
SRT_CONTROLLER_URL = "http://192.168.4.1"  # Default AP IP, change if using Ethernet/WiFi
SRT_SLEW_TIMEOUT = 300  # Max seconds to wait for telescope to reach target
SRT_POSITION_TOLERANCE = 0.5  # Degrees - consider "arrived" when within this tolerance

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
        print(f"[SRT] Connection error: {e}")
        return None
    except Exception as e:
        print(f"[SRT] API error: {e}")
        return None


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
        print("[SRT] Controller URL not configured - skipping telescope control")
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
        print(f"[SRT] Commanding telescope to Alt={coord1:.2f}° Az={coord2:.2f}°")

    elif coord_system == 'radec':
        # RA/Dec: use tracking mode (follows as Earth rotates)
        endpoint = "/track/radec"
        params = {"ra": coord1, "dec": coord2}
        print(f"[SRT] Commanding telescope to track RA={coord1:.3f}h Dec={coord2:.2f}°")

    elif coord_system == 'galactic':
        # Galactic: use tracking mode
        endpoint = "/track/galactic"
        params = {"l": coord1, "b": coord2}
        print(f"[SRT] Commanding telescope to track Gal l={coord1:.2f}° b={coord2:.2f}°")

    else:
        print(f"[SRT] Unknown coordinate system: {coord_system}")
        return False

    result = srt_api_call(endpoint, params)
    if result and result.get('ok'):
        print("[SRT] Command accepted")
        return True
    else:
        print(f"[SRT] Command failed: {result}")
        return False


def srt_wait_for_position(target_alt: float, target_az: float,
                          timeout: Optional[int] = None) -> bool:
    """Wait for telescope to reach target position.

    Polls the telescope status until it's within tolerance of the target,
    or timeout is reached.

    Returns True if position reached, False if timeout or error.
    """
    if not SRT_CONTROLLER_URL:
        return True

    timeout = timeout or SRT_SLEW_TIMEOUT
    start_time = time.time()

    print(f"[SRT] Waiting for telescope to reach Alt={target_alt:.1f}° Az={target_az:.1f}°...")

    while time.time() - start_time < timeout:
        status = srt_get_status()
        if status:
            current_alt = status.get('alt', 0)
            current_az = status.get('az', 0)

            alt_error = abs(current_alt - target_alt)
            az_error = abs(current_az - target_az)

            # Check if within tolerance
            if alt_error <= SRT_POSITION_TOLERANCE and az_error <= SRT_POSITION_TOLERANCE:
                print(f"[SRT] Telescope in position: Alt={current_alt:.2f}° Az={current_az:.2f}°")
                return True

            # Check if still slewing
            is_slewing = status.get('is_slewing', False)
            if not is_slewing and (alt_error > SRT_POSITION_TOLERANCE or az_error > SRT_POSITION_TOLERANCE):
                # Not slewing but not at position - might be tracking mode
                # For tracking, position constantly changes, so just check if tracking is enabled
                tracking = srt_get_tracking()
                if tracking and tracking.get('enabled'):
                    print("[SRT] Tracking active - proceeding with observation")
                    return True

        time.sleep(2)  # Poll every 2 seconds

    print("[SRT] Timeout waiting for telescope position")
    return False


def srt_stop_tracking() -> bool:
    """Stop telescope tracking."""
    if not SRT_CONTROLLER_URL:
        return True

    result = srt_api_call("/track", {"enable": "0"})
    return bool(result and result.get('ok', False))


# Current running observation
current_process: Optional[subprocess.Popen] = None
current_observation: Optional[dict] = None
observation_end_time: Optional[datetime] = None
process_lock = threading.Lock()
scheduler_running = True


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
    "enabled": True,
}


def load_schedule() -> list:
    """Load schedule from JSON file."""
    if os.path.exists(SCHEDULE_FILE):
        try:
            with open(SCHEDULE_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading schedule: {e}")
    return []


def save_schedule(schedule: list):
    """Save schedule to JSON file."""
    with open(SCHEDULE_FILE, 'w') as f:
        json.dump(schedule, f, indent=2)


def generate_filename(obs: dict) -> str:
    """Generate output filename for observation."""
    if obs.get('filename'):
        return obs['filename']
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = obs.get('name', 'obs').replace(' ', '_').lower()
    return f"h1_{name}_{timestamp}.h5"


def start_observation(obs: dict, duration_override: int = None) -> bool:
    """Start an observation.

    1. Commands the telescope to point at target coordinates
    2. Starts the SDR receiver to capture data
    """
    global current_process, current_observation, observation_end_time

    with process_lock:
        if current_process is not None and current_process.poll() is None:
            return False

        # Point telescope at target before starting observation
        if SRT_CONTROLLER_URL:
            if not srt_point_telescope(obs):
                print(f"[SRT] Failed to command telescope - aborting observation")
                return False

            # For RA/Dec and Galactic, tracking is enabled automatically
            # For Alt/Az, we could optionally wait for position
            # (but tracking mode means position changes, so just proceed)

        output_file = generate_filename(obs)
        env = os.environ.copy()
        env['H1_OUTPUT_FILE'] = output_file
        env['H1_CENTER_FREQ'] = str(obs.get('center_freq_mhz', 1420.405) * 1e6)
        env['H1_FFT_SIZE'] = str(obs.get('channels', 4096))
        env['H1_INTEGRATION_TIME'] = str(obs.get('integration_time_s', 3.0))

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
            print(f"[{now.strftime('%H:%M:%S')}] Started: {obs.get('name')} (ends at {observation_end_time.strftime('%H:%M:%S')})")
            return True
        except Exception as e:
            print(f"Error starting observation: {e}")
            return False


def stop_observation() -> bool:
    """Stop current observation."""
    global current_process, current_observation, observation_end_time

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

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Stopped: {name}")
        current_process = None
        current_observation = None
        observation_end_time = None
        return True


def scheduler_thread():
    """Background thread that checks schedule and starts/stops observations."""
    global scheduler_running

    print("[Scheduler] Background scheduler started")
    last_debug = 0

    while scheduler_running:
        try:
            now = datetime.now()
            schedule = load_schedule()

            # Debug output every 30 seconds
            if now.second % 30 < 5 and time.time() - last_debug > 25:
                last_debug = time.time()
                print(f"[Scheduler] {now.strftime('%H:%M:%S')} - {len(schedule)} observations loaded")
                for obs in schedule:
                    obs_date = obs.get('start_date', '') or now.strftime('%Y-%m-%d')
                    obs_time = obs.get('start_time', '')
                    enabled = obs.get('enabled', True)
                    print(f"  - {obs.get('name')}: {obs_date} {obs_time} (enabled={enabled})")

            # Check if current observation should end
            with process_lock:
                if observation_end_time and now >= observation_end_time:
                    print(f"[{now.strftime('%H:%M:%S')}] Duration complete")

            if observation_end_time and now >= observation_end_time:
                stop_observation()

            # Check if any scheduled observation should start
            with process_lock:
                is_running = current_process is not None and current_process.poll() is None

            if not is_running:
                for obs in schedule:
                    if not obs.get('enabled', True):
                        continue

                    # Parse scheduled time
                    obs_date = obs.get('start_date', '')
                    obs_time = obs.get('start_time', '')
                    if not obs_time:
                        continue

                    # If no date specified, use today
                    if not obs_date:
                        obs_date = now.strftime('%Y-%m-%d')

                    try:
                        scheduled = datetime.strptime(f"{obs_date} {obs_time}", '%Y-%m-%d %H:%M')
                    except ValueError as e:
                        print(f"[Scheduler] Parse error for {obs.get('name')}: {e}")
                        continue

                    # Start if within 60 seconds of scheduled time (increased window)
                    diff = (now - scheduled).total_seconds()
                    if 0 <= diff < 60:
                        print(f"[{now.strftime('%H:%M:%S')}] Scheduled start: {obs.get('name')} (diff={diff:.1f}s)")
                        start_observation(obs)
                        break

        except Exception as e:
            print(f"[Scheduler] Error: {e}")
            import traceback
            traceback.print_exc()

        time.sleep(5)  # Check every 5 seconds

    print("[Scheduler] Background scheduler stopped")


# HTML Template
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>H1 Receiver Scheduler</title>
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
    </style>
</head>
<body>
    <div class="container">
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <div>
                <h1>H1 Receiver Scheduler</h1>
                <p class="subtitle">Hydrogen Line (21cm) Observation Manager</p>
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
        <div class="toolbar">
            <button class="btn btn-primary" onclick="openAddModal()">+ Add Observation</button>
            <button class="btn btn-secondary" onclick="saveSchedule()">Save Schedule</button>
            <button class="btn btn-secondary" onclick="document.getElementById('loadFile').click()">Load</button>
            <input type="file" id="loadFile" class="file-input" accept=".json" onchange="loadFile(event)">
            <button class="btn btn-secondary" onclick="exportSchedule()">Export JSON</button>
        </div>
        <div class="schedule-list" id="scheduleList">
            <div class="empty-state">No observations scheduled.</div>
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
                        <input type="time" id="obsStartTime" required>
                    </div>
                    <div class="form-group">
                        <label>Duration (minutes)</label>
                        <input type="number" id="obsDuration" min="1" max="1440" required>
                    </div>
                </div>

                <div class="section-title">Target Coordinates</div>
                <div class="coord-section">
                    <div class="form-grid">
                        <div class="form-group">
                            <label>Coordinate System</label>
                            <select id="obsCoordSystem" onchange="updateCoordLabels()">
                                <option value="altaz">Alt/Az (Horizontal)</option>
                                <option value="radec">RA/Dec (Equatorial J2000)</option>
                                <option value="galactic">Galactic (l, b)</option>
                            </select>
                        </div>
                    </div>
                    <div class="form-grid" style="margin-top:15px">
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
        });

        document.getElementById('obsForm').addEventListener('submit', e => {
            e.preventDefault();
            saveObservation();
        });

        function updateCoordLabels() {
            const sys = document.getElementById('obsCoordSystem').value;
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
                        <div class="field"><div class="field-label">Date/Time</div><div class="field-value">${obs.start_date || 'Today'} ${obs.start_time}</div></div>
                        <div class="field"><div class="field-label">Duration</div><div class="field-value">${obs.duration_minutes} min</div></div>
                        <div class="field"><div class="field-label">Coordinates</div><div class="field-value">${formatCoordDisplay(obs)}</div></div>
                        <div class="field"><div class="field-label">Frequency</div><div class="field-value">${obs.center_freq_mhz} MHz</div></div>
                        <div class="field"><div class="field-label">BW / Gain</div><div class="field-value">${obs.bandwidth_mhz} MHz / ${obs.gain_db} dB</div></div>
                        <div class="field"><div class="field-label">Channels</div><div class="field-value">${obs.channels}</div></div>
                        <div class="field"><div class="field-label">Integration</div><div class="field-value">${obs.integration_time_s}s</div></div>
                    </div>
                    <div class="schedule-actions">
                        <button class="btn btn-success btn-icon" onclick="runNow(${i})" title="Run Now">▶</button>
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

        function editObs(i) {
            document.getElementById('modalTitle').textContent = 'Edit Observation';
            document.getElementById('obsIndex').value = i;
            fillForm(schedule[i]);
            document.getElementById('obsModal').classList.add('active');
        }

        function fillForm(obs) {
            document.getElementById('obsName').value = obs.name || DEFAULTS.name;
            document.getElementById('obsCoordSystem').value = obs.coord_system || DEFAULTS.coord_system;
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
            document.getElementById('obsFilename').value = obs.filename || '';
            updateCoordLabels();
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
            const i = parseInt(document.getElementById('obsIndex').value);
            const obs = {
                name: document.getElementById('obsName').value,
                coord_system: document.getElementById('obsCoordSystem').value,
                coord1_deg: parseInt(document.getElementById('coord1Deg').value) || 0,
                coord1_min: parseInt(document.getElementById('coord1Min').value) || 0,
                coord1_sec: parseFloat(document.getElementById('coord1Sec').value) || 0,
                coord2_deg: parseInt(document.getElementById('coord2Deg').value) || 0,
                coord2_min: parseInt(document.getElementById('coord2Min').value) || 0,
                coord2_sec: parseFloat(document.getElementById('coord2Sec').value) || 0,
                start_date: document.getElementById('obsStartDate').value,
                start_time: document.getElementById('obsStartTime').value,
                duration_minutes: parseInt(document.getElementById('obsDuration').value),
                center_freq_mhz: parseFloat(document.getElementById('obsCenterFreq').value),
                bandwidth_mhz: parseFloat(document.getElementById('obsBandwidth').value),
                gain_db: parseFloat(document.getElementById('obsGain').value),
                channels: parseInt(document.getElementById('obsChannels').value),
                integration_time_s: parseFloat(document.getElementById('obsIntegration').value),
                sdr_type: document.getElementById('obsSdrType').value,
                filename: document.getElementById('obsFilename').value,
                enabled: i >= 0 ? schedule[i].enabled : true
            };
            if (i >= 0) { schedule[i] = obs; } else { schedule.push(obs); }
            closeModal();
            renderSchedule();
            autoSave();
        }

        function deleteObs(i) {
            if (confirm('Delete this observation?')) {
                schedule.splice(i, 1);
                renderSchedule();
                autoSave();
            }
        }

        function toggleEnabled(i) {
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
                    currentObs = data.observation;
                } else {
                    dot.classList.remove('running');
                    text.textContent = 'Idle - Scheduler Active';
                    btn.style.display = 'none';
                    currentObs = null;
                }
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
    </script>
</body>
</html>
'''


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/schedule', methods=['GET'])
def get_schedule():
    return jsonify(load_schedule())


@app.route('/api/schedule', methods=['POST'])
def post_schedule():
    schedule = request.json
    save_schedule(schedule)
    return jsonify({'success': True})


@app.route('/api/status', methods=['GET'])
def get_status():
    with process_lock:
        running = current_process is not None and current_process.poll() is None
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
