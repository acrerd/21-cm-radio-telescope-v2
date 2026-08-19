// Auto-generated - embedded index.html
#ifndef INDEX_HTML_H
#define INDEX_HTML_H

const char INDEX_HTML[] PROGMEM = R"rawliteral(<!DOCTYPE html>
<html>
<head>
    <title>SRT Controller</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; }
        body { font-family: Arial, sans-serif; margin: 0; padding: 15px; background: #1a1a2e; color: #eee; }
        .container { max-width: 1000px; margin: 0 auto; }
        h1 { color: #00d9ff; margin: 0 0 10px 0; font-size: 1.5em; }
        h3 { margin: 0 0 10px 0; color: #aaa; font-size: 1em; }
        h4 { margin: 12px 0 6px 0; color: #bbb; font-size: 0.9em; }
        .box { background: #16213e; padding: 12px; border-radius: 8px; margin-bottom: 10px; }
        .header-row { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
        .header-row h1 { margin: 0; }
        .header-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
        .header-link { color: #00d9ff; border: 1px solid #00d9ff; border-radius: 4px; padding: 6px 10px; text-decoration: none; font-size: 0.9em; }
        .header-link:hover { background: #00d9ff; color: #000; }
        .status-row { display: flex; justify-content: space-between; margin: 4px 0; }
        .label { color: #888; }
        .value { font-family: monospace; font-size: 1.1em; }
        input[type="number"], input[type="text"], input[type="password"], select {
            padding: 6px; background: #0f0f23; color: #fff; border: 1px solid #444;
            border-radius: 4px; margin: 2px;
        }
        input[type="number"] { width: 75px; }
        input[type="text"], input[type="password"], select { width: 180px; }
        button {
            background: #00d9ff; color: #000; border: none;
            padding: 8px 16px; cursor: pointer; margin: 3px; border-radius: 4px; font-size: 0.9em;
        }
        button:hover { background: #00b8d4; }
        button.stop { background: #ff4444; color: #fff; }
        button.stop:hover { background: #cc0000; }
        button.stop-all { background: #b00020; color: #fff; width: 100%; font-weight: bold; }
        button.stop-all:hover { background: #7f0017; }
        button.stop-move { background: #ff5a5f; color: #fff; width: 100%; }
        button.stop-move:hover { background: #d9363e; }
        button:disabled { background: #555; color: #aaa; cursor: not-allowed; }
        button:disabled:hover { background: #555; }
        button.secondary { background: #555; color: #fff; }
        button.secondary:hover { background: #666; }
        button.solar { background: #ffaa00; color: #000; }
        button.solar:hover { background: #cc8800; }
        button.lunar { background: #aaaacc; color: #000; }
        button.lunar:hover { background: #8888aa; }
        button.galactic-bulge { background: #f2e9ff; color: #2d124d; }
        button.galactic-bulge:hover { background: #d8b8ff; }
        button.cal-on { background: #00ff00; color: #000; }
        button.cal-on:hover { background: #00cc00; }
        .tracking { color: #00ff00; }
        .idle { color: #888; }
        .connected { color: #00ff00; }
        .disconnected { color: #ff8800; }
        .wifi-network { padding: 6px; margin: 3px 0; background: #0f0f23; border-radius: 4px; cursor: pointer; }
        .wifi-network:hover { background: #1a1a3e; }
        .wifi-signal { float: right; color: #888; }
        .hidden { display: none; }
        .tab-bar { display: flex; margin-bottom: 10px; }
        .tab { padding: 8px 20px; cursor: pointer; background: #16213e; border-radius: 8px 8px 0 0; margin-right: 2px; }
        .tab.active { background: #00d9ff; color: #000; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .target-info { font-size: 0.85em; color: #888; margin-top: 5px; }
        .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
        .coord-row { display: flex; gap: 10px; align-items: center; margin: 6px 0; flex-wrap: wrap; }
        .coord-row label { white-space: nowrap; }
        .btn-row { margin-top: 8px; }
        .stacked-actions button { display: block; margin: 6px 0; }
        .stop-row { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 6px; margin: 6px 0; }
        .stop-row button { width: 100%; margin: 0; padding-left: 8px; padding-right: 8px; }
        .action-grid { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; align-items: center; }
        .action-grid button { margin: 0; }
        .action-row { display: flex; justify-content: center; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
        .action-row button { margin: 0; }
        .quick-cal-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 8px; }
        .quick-cal-row button, .quick-cal-row .axis-switch { width: 100%; margin: 0; }
        .quick-cal-row.single { grid-template-columns: 1fr; }
        .axis-switch { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0; margin: 10px 0; border: 1px solid #444; border-radius: 6px; overflow: hidden; background: #0f0f23; }
        .cal-switch { grid-template-columns: 1fr 1fr; margin: 0; min-width: 150px; }
        .axis-fixed-row { display: none; align-items: center; gap: 8px; flex-wrap: wrap; margin: 8px 0 10px 0; padding: 8px; border: 1px solid #333; border-radius: 6px; background: #0f0f23; }
        .axis-fixed-row.active { display: flex; }
        .axis-fixed-row input { width: 90px; }
        .axis-option { margin: 0; border-radius: 0; background: transparent; color: #bbb; padding: 9px 8px; }
        .axis-option:hover { background: #1a2a4e; }
        .axis-option.active { background: #00d9ff; color: #000; }
        .fault-dependent.locked { opacity: 0.45; filter: grayscale(1); }
        button.target-active { background: #555; color: #aaa; cursor: not-allowed; filter: grayscale(1); }
        button.target-active:hover { background: #555; }
        .help-toggle { display: flex; align-items: center; gap: 6px; color: #ccc; }
        .help-bubble { position: fixed; max-width: 260px; background: #f4fbff; color: #111; border: 1px solid #00d9ff; border-radius: 8px; padding: 9px 11px; font-size: 0.85em; line-height: 1.35; box-shadow: 0 8px 24px rgba(0,0,0,0.35); z-index: 2000; display: none; }
        .help-bubble::after { content: ""; position: absolute; left: 18px; top: -7px; border-left: 7px solid transparent; border-right: 7px solid transparent; border-bottom: 7px solid #f4fbff; }
        @media (max-width: 800px) { .two-col { grid-template-columns: 1fr; } }
        @media (max-width: 600px) { .stop-row { grid-template-columns: 1fr; } }
        .serial-panel { position: fixed; bottom: 0; left: 0; right: 0; background: #0f0f23; border-top: 2px solid #00d9ff; transition: height 0.3s; }
        .serial-header { display: flex; justify-content: space-between; padding: 6px 15px; background: #16213e; cursor: pointer; }
        .serial-header:hover { background: #1a2a4e; }
        .serial-log { height: 150px; overflow-y: auto; font-family: monospace; font-size: 0.85em; padding: 5px 10px; }
        .serial-log.collapsed { height: 0; padding: 0; }
        .log-line { margin: 1px 0; white-space: nowrap; }
        .log-tx { color: #00d9ff; }
        .log-rx { color: #88ff88; }
        .log-esp { color: #ffaa00; }
        .log-time { color: #666; margin-right: 8px; }
        body { padding-bottom: 180px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header-row">
            <h1 id="page-title">SRT Controller</h1>
            <div class="header-actions">
                <a class="header-link" href="http://127.0.0.1:5000/" target="_blank" onclick="openScheduler(event)" data-help="Open the H1 scheduler website running on this computer. If it is not already running, the browser cannot start Python by itself.">Scheduler</a>
                <button class="secondary" onclick="updateFirmware()" data-help="Ask the local scheduler service to build and flash the ESP32 firmware over the network with PlatformIO OTA.">Update firmware</button>
            </div>
        </div>
        <div class="tab-bar">
            <div class="tab active" onclick="showTab('control')">Control</div>
            <div class="tab" onclick="showTab('network')">Network</div>
            <div class="tab" onclick="showTab('settings')">Settings</div>
            <div class="tab" onclick="showTab('help')">Help</div>
        </div>
        <div id="tab-control" class="tab-content active">
            <div class="two-col">
                <div class="left-col">
                    <div class="box">
                        <h3>Current Status</h3>
                        <div class="status-row"><span class="label">Altitude:</span><span class="value" id="alt">--</span></div>
                        <div class="status-row"><span class="label">Azimuth:</span><span class="value" id="az">--</span></div>
                        <div class="status-row"><span class="label">RA:</span><span class="value" id="cur_ra">--</span></div>
                        <div class="status-row"><span class="label">Dec:</span><span class="value" id="cur_dec">--</span></div>
                        <div class="status-row"><span class="label">Gal l:</span><span class="value" id="cur_gl">--</span></div>
                        <div class="status-row"><span class="label">Gal b:</span><span class="value" id="cur_gb">--</span></div>
                        <div class="status-row"><span class="label">Alt Motor:</span><span class="value" id="alt_a">-- A</span></div>
                        <div class="status-row"><span class="label">Az Motor:</span><span class="value" id="az_a">-- A</span></div>
                        <div class="status-row"><span class="label">Status:</span><span class="value" id="status">--</span></div>
                        <div class="status-row"><span class="label">Error Status:</span><span class="value" id="error_status">--</span></div>
                        <div class="status-row"><span class="label">Tracking:</span><span class="value" id="tracking_target">--</span></div>
                        <div class="status-row"><span class="label">Time:</span><span class="value" id="time_status">--</span></div>
                    </div>
                    <div class="box">
                        <h3>Quick Targets</h3>
                        <button class="solar fault-dependent" onclick="trackSun()" data-help="Track the Sun using the controller ephemeris and keep updating the telescope position as it moves.">Track Sun</button>
                        <button class="lunar fault-dependent" onclick="trackMoon()" data-help="Track the Moon using the controller ephemeris and keep updating the telescope position as it moves.">Track Moon</button>
                        <button class="galactic-bulge fault-dependent" onclick="trackGalacticBulge()" data-help="Track the Galactic Bulge when it is above the horizon. If it is below the horizon, track the nearest visible point on the galactic plane until the bulge rises.">Track Galactic Bulge</button>
                        <div class="target-info">Sun: <span id="sun_pos">--</span><br>Moon: <span id="moon_pos">--</span><br><span id="bulge_label">Bulge</span>: <span id="bulge_pos">--</span></div>
                    </div>
                    <div class="box">
                        <h3>Actions</h3>
                        <div class="stacked-actions">
                            <button class="stop-all fault-dependent" onclick="stopAll()" data-help="Emergency stop: stop telescope motion, cancel tracking, and ask the scheduler to stop the current run.">STOP all</button>
                            <div class="stop-row">
                                <button class="stop-move fault-dependent" onclick="stopSlewing()" data-help="Stop the current slew and pause automatic tracking commands for 10 seconds.">Stop slewing</button>
                                <button class="stop-move fault-dependent" onclick="stopTrackingOnly()" data-help="Cancel the active tracking target without sending a motor stop command.">Stop tracking</button>
                                <button class="stop-move fault-dependent" onclick="stopScheduledRun()" data-help="Ask the H1 scheduler on this computer to stop the currently running scheduled observation.">Stop current scheduled run</button>
                            </div>
                        </div>
                        <div class="axis-switch" id="axis_switch">
                            <button class="axis-option fault-dependent" id="axis_az" onclick="setAxisMode('az')" data-help="Track azimuth only while holding altitude at the current telescope altitude.">Only track azimuth</button>
                            <button class="axis-option active fault-dependent" id="axis_both" onclick="setAxisMode('both')" data-help="Track both altitude and azimuth for the selected target.">Track in both directions</button>
                            <button class="axis-option fault-dependent" id="axis_alt" onclick="setAxisMode('alt')" data-help="Track altitude only while holding azimuth at the current telescope azimuth.">Only track altitude</button>
                        </div>
                        <div class="axis-fixed-row" id="axis_fixed_row">
                            <label id="axis_fixed_label">Fixed coordinate</label>
                            <input class="fault-dependent" type="number" id="axis_fixed_value" step="0.5" min="0" max="360" value="0" data-help="Enter the coordinate to hold fixed while the other axis keeps tracking.">
                            <button class="fault-dependent" onclick="applyFixedAxis()" data-help="Apply this fixed coordinate while continuing one-axis tracking.">Apply</button>
                        </div>
                        <div class="action-row">
                            <button class="fault-dependent" onclick="goHome()" data-help="Slew directly to the Home Alt/Az saved in Settings.">Go home</button>
                            <button class="secondary" id="reset_btn" onclick="resetFault()" disabled data-help="Clear a mount fault after checking that the telescope is safe to move again.">Reset</button>
                            <button class="fault-dependent" onclick="runHoming()" data-help="Run the mount homing sequence on the Arduino Due controller.">Homing Sequence</button>
                        </div>
                    </div>
                </div>
                <div class="right-col">
                    <div class="box">
                        <h3>Controls</h3>
                        <h4>Direct Control (Alt/Az)</h4>
                        <div class="coord-row">
                            <label>Alt: <input class="fault-dependent" type="number" id="direct_alt" step="0.5" min="0" max="90" value="45"></label>
                            <label>Az: <input class="fault-dependent" type="number" id="direct_az" step="0.5" min="0" max="355" value="180"></label>
                        </div>
                        <div class="btn-row">
                            <button class="fault-dependent" onclick="goDirect()" data-help="Slew once to the Alt/Az coordinates above.">Go To</button>
                        </div>
                        <h4>Equatorial (RA/Dec) - J2000</h4>
                        <div class="coord-row">
                            <label>RA: <input class="fault-dependent" type="number" id="ra" step="0.5" min="0" max="24" value="0"></label>
                            <label>Dec: <input class="fault-dependent" type="number" id="dec" step="0.5" min="-90" max="90" value="0"></label>
                        </div>
                        <div class="btn-row">
                            <button class="fault-dependent" onclick="goToRaDec()" data-help="Slew once to the RA/Dec coordinates above.">Go To</button>
                            <button class="fault-dependent" onclick="trackRaDec()" data-help="Track the RA/Dec target continuously as the sky moves.">Track</button>
                        </div>
                        <h4>Galactic (l/b) - J2000</h4>
                        <div class="coord-row">
                            <label>l: <input class="fault-dependent" type="number" id="gal_l" step="0.5" min="0" max="360" value="0"></label>
                            <label>b: <input class="fault-dependent" type="number" id="gal_b" step="0.5" min="-90" max="90" value="0"></label>
                        </div>
                        <div class="btn-row">
                            <button class="fault-dependent" onclick="goToGalactic()" data-help="Convert the Galactic coordinates to RA/Dec, then slew once to that target.">Go To</button>
                            <button class="fault-dependent" onclick="trackGalactic()" data-help="Convert the Galactic coordinates to RA/Dec, then track that sky position continuously.">Track</button>
                        </div>
                        <div class="target-info" id="galactic_radec"></div>
                    </div>
                    <div class="box">
                        <h3>Pointing Offset (Scanning)</h3>
                        <div class="coord-row">
                            <label>dAlt: <input class="fault-dependent" type="number" id="offset_alt" step="0.5" min="-45" max="45" value="0"></label>
                            <label>dAz: <input class="fault-dependent" type="number" id="offset_az" step="0.5" min="-45" max="45" value="0"></label>
                        </div>
                        <div class="btn-row">
                            <button class="fault-dependent" onclick="setOffset()" data-help="Apply the Alt/Az offset to tracking commands for scanning or mapping.">Apply Offset</button>
                            <button class="secondary fault-dependent" onclick="clearOffset()" data-help="Clear the pointing offset and return tracking commands to the unshifted target.">Clear</button>
                        </div>
                        <div class="target-info">Current offset: <span id="current_offset">0.0 / 0.0</span></div>
                    </div>
                    <div class="box">
                        <h3>Quick Calibrations</h3>
                        <div class="quick-cal-row single">
                            <div class="axis-switch cal-switch" id="cal_switch">
                                <button class="axis-option active fault-dependent" id="cal_off" onclick="setCalibrator(false)" data-help="Turn the calibrator noise source off.">Cal Off</button>
                                <button class="axis-option fault-dependent" id="cal_on" onclick="setCalibrator(true)" data-help="Turn the calibrator noise source on for receiver calibration.">Cal On</button>
                            </div>
                        </div>
                        <div class="quick-cal-row">
                            <button class="fault-dependent" onclick="startQuickSunScan()" data-help="Ask the scheduler to start a Sun Scan using its default scan settings.">N-point / Sun Scan</button>
                            <button class="fault-dependent" onclick="startQuickCalDay()" data-help="Ask the scheduler to start a Calibration Day run using its default interval and scan settings.">Calibration Day Run</button>
                        </div>
                        <div class="target-info">For more configuration and settings, use the scheduler.</div>
                    </div>
                </div>
            </div>
        </div>
        <div id="tab-network" class="tab-content">
            <div class="two-col">
                <div>
                    <div class="box" id="eth-section">
                        <h3>Ethernet</h3>
                        <div class="status-row"><span class="label">Name:</span><span class="value" id="net_name">--</span></div>
                        <div class="status-row"><span class="label">Status:</span><span class="value" id="eth_status">--</span></div>
                        <div class="status-row"><span class="label">IP Address:</span><span class="value" id="eth_ip">--</span></div>
                        <div class="status-row"><span class="label">MAC:</span><span class="value" id="eth_mac">--</span></div>
                        <div style="margin-top: 10px; padding-top: 10px; border-top: 1px solid #444;">
                            <div class="coord-row">
                                <label><input type="radio" name="eth_mode" id="eth_dhcp" onclick="toggleEthMode()" checked> DHCP</label>
                                <label><input type="radio" name="eth_mode" id="eth_static" onclick="toggleEthMode()"> Static IP</label>
                            </div>
                            <div id="eth-static-fields">
                                <div class="coord-row"><label>IP: <input type="text" id="eth_static_ip" placeholder="192.168.1.100"></label></div>
                                <div class="coord-row"><label>Gateway: <input type="text" id="eth_gateway" placeholder="192.168.1.1"></label></div>
                                <div class="coord-row"><label>Subnet: <input type="text" id="eth_subnet" placeholder="255.255.255.0"></label></div>
                                <div class="coord-row"><label>DNS: <input type="text" id="eth_dns" placeholder="8.8.8.8"></label></div>
                            </div>
                            <div class="btn-row">
                                <button onclick="saveEthSettings()" data-help="Save the Ethernet DHCP or static IP settings. Reboot the controller to apply them.">Save Ethernet</button>
                            </div>
                            <p id="eth-save-status" class="target-info"></p>
                        </div>
                    </div>
                    <div class="box" id="wifi-power-section" style="display:none;">
                        <h3>WiFi Power</h3>
                        <p style="color:#888;font-size:0.9em;margin:5px 0;">Disable WiFi to save ~100mA when using Ethernet</p>
                        <button id="wifi_power_btn" onclick="toggleWifiPower()" data-help="Turn WiFi on or off. WiFi cannot be disabled unless Ethernet is connected.">Disable WiFi</button>
                    </div>
                    <div class="box">
                        <h3>WiFi</h3>
                        <div class="status-row"><span class="label">Access Point:</span><span class="value" id="ap_status">--</span></div>
                        <div class="status-row"><span class="label">AP IP:</span><span class="value" id="ap_ip">--</span></div>
                        <div class="status-row"><span class="label">MAC:</span><span class="value" id="wifi_mac">--</span></div>
                        <div class="status-row"><span class="label">Station:</span><span class="value" id="sta_status">--</span></div>
                        <div class="status-row"><span class="label">Station IP:</span><span class="value" id="sta_ip">--</span></div>
                    </div>
                    <div class="box">
                        <h3>Saved Network</h3>
                        <p id="saved-network" style="color: #888; margin: 5px 0;">None</p>
                        <button class="secondary" onclick="forgetWifi()" data-help="Forget the saved WiFi network credentials from the controller.">Forget</button>
                    </div>
                </div>
                <div>
                    <div class="box">
                        <h3>Connect to Network</h3>
                        <div id="wifi-networks" style="max-height: 200px; overflow-y: auto;">
                            <p style="color: #888;">Click Scan to find networks...</p>
                        </div>
                        <div style="margin-top: 8px;"><button onclick="scanWifi()" data-help="Scan for nearby WiFi networks.">Scan</button></div>
                        <div id="wifi-connect-form" class="hidden" style="margin-top: 10px; padding-top: 10px; border-top: 1px solid #444;">
                            <div style="margin-bottom: 8px;"><label>Network: <strong id="selected-ssid"></strong></label></div>
                            <div style="margin-bottom: 8px;"><label>Password: <input type="password" id="wifi-password"></label></div>
                            <button onclick="connectWifi()" data-help="Connect the controller to the selected WiFi network using the password entered above.">Connect</button>
                            <button class="secondary" onclick="hideConnectForm()" data-help="Close the WiFi connection form without changing network settings.">Cancel</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        <div id="tab-settings" class="tab-content">
            <div class="two-col">
                <div>
                    <div class="box">
                        <h3>Observer Location</h3>
                        <div class="coord-row">
                            <label>Latitude: <input type="number" id="set_lat" step="0.000001" min="-90" max="90"></label>
                        </div>
                        <div class="coord-row">
                            <label>Longitude: <input type="number" id="set_lon" step="0.000001" min="-180" max="180"></label>
                        </div>
                    </div>
                    <div class="box">
                        <h3>Software Limits</h3>
                        <div class="coord-row">
                            <label>Az Min: <input type="number" id="set_az_min" step="0.5" min="0" max="360"></label>
                            <label>Az Max: <input type="number" id="set_az_max" step="0.5" min="0" max="360"></label>
                        </div>
                        <div class="coord-row">
                            <label>Alt Min: <input type="number" id="set_alt_min" step="0.5" min="0" max="90"></label>
                            <label>Alt Max: <input type="number" id="set_alt_max" step="0.5" min="0" max="90"></label>
                        </div>
                    </div>
                    <div class="box">
                        <h3>Home Position</h3>
                        <div class="coord-row">
                            <label>Home Alt: <input type="number" id="set_home_alt" step="0.5" min="0" max="90"></label>
                            <label>Home Az: <input type="number" id="set_home_az" step="0.5" min="0" max="360"></label>
                        </div>
                    </div>
                </div>
                <div>
                    <div class="box">
                        <h3>Update Tolerance</h3>
                        <div class="coord-row">
                            <label>Degrees: <input type="number" id="set_deadband" step="0.05" min="0.05" max="5"></label>
                        </div>
                    </div>
                    <div class="box">
                        <h3>Display</h3>
                        <div class="coord-row">
                            <label>Page Name: <input type="text" id="set_page_name" maxlength="31"></label>
                        </div>
                        <div class="coord-row">
                            <label class="help-toggle"><input type="checkbox" id="set_hover_help" onchange="setHoverHelp(this.checked)" checked> Hover Help</label>
                        </div>
                    </div>
                    <div class="box">
                        <h3>WiFi Access Point</h3>
                        <div class="coord-row">
                            <label>AP SSID: <input type="text" id="set_ap_ssid" maxlength="31"></label>
                        </div>
                        <div class="coord-row">
                            <label>AP Password: <input type="text" id="set_ap_pass" maxlength="63"></label>
                        </div>
                        <p class="target-info">Changes take effect after reboot</p>
                    </div>
                    <div class="box">
                        <div class="btn-row">
                            <button onclick="saveSettings()" data-help="Save observer location, limits, home position, display, and access point settings.">Save Settings</button>
                            <button class="secondary" onclick="loadSettings()" data-help="Reload settings from the controller and discard unsaved edits.">Reload</button>
                            <button class="stop" onclick="resetSettings()" data-help="Restore controller settings to their firmware defaults.">Reset to Defaults</button>
                        </div>
                        <p id="settings-status" class="target-info"></p>
                    </div>
                </div>
            </div>
        </div>
        <div id="tab-help" class="tab-content">
            <div class="two-col">
                <div>
                    <div class="box">
                        <h3>Coordinate Systems (J2000)</h3>
                        <p><strong>RA/Dec</strong>: Right Ascension 0-24h, Dec -90 to +90</p>
                        <p><strong>Galactic</strong>: l 0-360, b -90 to +90</p>
                        <p><strong>Alt/Az</strong>: Altitude 0-90, Azimuth 0-355</p>
                    </div>
                    <div class="box">
                        <h3>RA/Dec Input Formats</h3>
                        <p style="font-size:0.85em;"><strong>RA:</strong> 12.5 | 12h30m | 12h30m00s | 12:30:00</p>
                        <p style="font-size:0.85em;"><strong>Dec:</strong> -45.5 | -45d30m | +45d30m00s | 45:30:00</p>
                    </div>
                    <div class="box">
                        <h3>Tracking Modes</h3>
                        <p><strong>Go To:</strong> Slew to position once</p>
                        <p><strong>Track:</strong> Continuously follow as Earth rotates</p>
                    </div>
                    <div class="box">
                        <h3>Pointing Offset</h3>
                        <p style="font-size:0.9em;">Add Alt/Az offset for scanning or mapping. Offset is applied to all tracking commands until cleared.</p>
                    </div>
                    <div class="box">
                        <h3>Calibrator</h3>
                        <p style="font-size:0.9em;">Noise source for receiver calibration. Toggle via button or API. State shown in status bar.</p>
                    </div>
                </div>
                <div>
                    <div class="box">
                        <h3>Mount Limits</h3>
                        <p style="font-size:0.9em;"><strong>Altitude:</strong> 0 to 90 degrees</p>
                        <p style="font-size:0.9em;"><strong>Azimuth:</strong> 2 to 353 degrees (configurable)</p>
                        <p style="font-size:0.9em;">Targets outside limits go to home position.</p>
                    </div>
                    <div class="box">
                        <h3>Stellarium Setup</h3>
                        <ol style="margin: 5px 0; padding-left: 20px; font-size: 0.9em;">
                            <li>Configuration > Plugins > Telescope Control</li>
                            <li>Enable and restart Stellarium</li>
                            <li>Add telescope: Type "External", Host IP, Port 10001</li>
                            <li>Connect, then Ctrl+1 to slew to selected object</li>
                        </ol>
                    </div>
                    <div class="box">
                        <h3>API Endpoints</h3>
                        <p style="font-size:0.85em;"><code>/status</code> - Mount position &amp; state</p>
                        <p style="font-size:0.85em;"><code>/track/radec?ra=X&amp;dec=Y</code> - Track J2000</p>
                        <p style="font-size:0.85em;"><code>/track/galactic?l=X&amp;b=Y</code> - Track galactic</p>
                        <p style="font-size:0.85em;"><code>/tracking/axis?mode=az&amp;alt=X</code> - Track azimuth only</p>
                        <p style="font-size:0.85em;"><code>/tracking/axis?mode=alt&amp;az=X</code> - Track altitude only</p>
                        <p style="font-size:0.85em;"><code>/offset?alt=X&amp;az=Y</code> - Set pointing offset</p>
                        <p style="font-size:0.85em;"><code>/calibrator?on=1</code> - Control noise source</p>
                    </div>
                    <div class="box">
                        <h3>Due Serial Commands</h3>
                        <p style="font-size:0.85em;"><code>HOME</code> - Run homing sequence</p>
                        <p style="font-size:0.85em;"><code>STOP</code> - Emergency stop</p>
                        <p style="font-size:0.85em;"><code>STATUS</code> - Show position</p>
                        <p style="font-size:0.85em;"><code>CAL ON/OFF</code> - Calibrator control</p>
                    </div>
                    <div class="box">
                        <h3>About</h3>
                        <p style="font-size:0.9em;">SRT Controller v2.0<br>Acre Road Observatory, Glasgow</p>
                        <p style="font-size:0.85em;"><a href="https://github.com/acrerd/21-cm-radio-telescope-v2" target="_blank" style="color:#4fc3f7;">GitHub Repository</a></p>
                    </div>
                </div>
            </div>
        </div>
    </div>
    <div class="serial-panel" id="serial-panel">
        <div class="serial-header" onclick="toggleSerialPanel()">
            <span>Serial Monitor</span>
            <span id="serial-toggle">▼</span>
        </div>
        <div class="serial-log" id="serial-log"></div>
    </div>
    <div class="help-bubble" id="hover-help"></div>
<script>
let selectedSsid='';
const SCHEDULER_URL='http://127.0.0.1:5000';
const SCHEDULER_URLS=['http://127.0.0.1:5000','http://localhost:5000'];
let hoverHelpEnabled=localStorage.getItem('hoverHelp')!=='false';
let hoverHelpTimer=null;
let hoverHelpTarget=null;
let currentTrackingTarget='';
let faultLocked=false;
let currentAxisMode='both';
function schedulerFetch(path,opts){return fetch(SCHEDULER_URL+path,opts||{});}
function findScheduler(){let i=0;return new Promise((resolve,reject)=>{const next=()=>{if(i>=SCHEDULER_URLS.length){reject();return;}const url=SCHEDULER_URLS[i++];fetch(url+'/api/status',{cache:'no-store'}).then(()=>resolve(url)).catch(next);};next();});}
function openScheduler(e){e.preventDefault();findScheduler().then(url=>window.open(url+'/','_blank')).catch(()=>{window.open(SCHEDULER_URL+'/','_blank');alert('Scheduler is not responding. A web page cannot start Python unless a local scheduler service is already running. Start it with:\\n/home/astro/radioconda/bin/python /home/astro/21-cm-radio-telescope-v2/receiver_scheduler/h1_web_scheduler.py --host 0.0.0.0 --port 5000');});}
function setHoverHelp(on){hoverHelpEnabled=on;localStorage.setItem('hoverHelp',on?'true':'false');hideHoverHelp();}
function hideHoverHelp(){if(hoverHelpTimer){clearTimeout(hoverHelpTimer);hoverHelpTimer=null;}const b=document.getElementById('hover-help');if(b)b.style.display='none';hoverHelpTarget=null;}
function showHoverHelp(el){if(!hoverHelpEnabled||!el||el.disabled)return;const text=el.getAttribute('data-help');if(!text)return;const b=document.getElementById('hover-help');const r=el.getBoundingClientRect();b.textContent=text;b.style.left=Math.min(r.left,window.innerWidth-280)+'px';b.style.top=Math.min(r.bottom+10,window.innerHeight-80)+'px';b.style.display='block';}
function initHoverHelp(){const cb=document.getElementById('set_hover_help');if(cb)cb.checked=hoverHelpEnabled;document.addEventListener('pointerover',e=>{const el=e.target.closest('[data-help]');if(!el||el===hoverHelpTarget)return;hideHoverHelp();hoverHelpTarget=el;hoverHelpTimer=setTimeout(()=>showHoverHelp(el),2000);});document.addEventListener('pointerout',e=>{if(hoverHelpTarget&&(!e.relatedTarget||!hoverHelpTarget.contains(e.relatedTarget)))hideHoverHelp();});document.addEventListener('scroll',hideHoverHelp,true);}
function showTab(name){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));event.target.classList.add('active');document.getElementById('tab-'+name).classList.add('active');if(name==='network')updateNetworkStatus();if(name==='settings')loadSettings();}
function formatRA(h){const hh=Math.floor(h);const m=Math.floor((h-hh)*60);const s=Math.floor(((h-hh)*60-m)*60);return hh+'h'+String(m).padStart(2,'0')+'m'+String(s).padStart(2,'0')+'s';}
function formatDec(d){const sign=d>=0?'+':'-';d=Math.abs(d);const dd=Math.floor(d);const m=Math.floor((d-dd)*60);return sign+dd+'\u00b0'+String(m).padStart(2,'0')+"'";}
function parseRA(s){s=s.trim().toLowerCase();let m=s.match(/^(\d+(?:\.\d+)?)\s*h\s*(?:(\d+(?:\.\d+)?)\s*m?\s*)?(?:(\d+(?:\.\d+)?)\s*s?\s*)?$/);if(m)return(parseFloat(m[1])||0)+(parseFloat(m[2])||0)/60+(parseFloat(m[3])||0)/3600;m=s.match(/^(\d+(?:\.\d+)?)[\s:]+(\d+(?:\.\d+)?)(?:[\s:]+(\d+(?:\.\d+)?))?$/);if(m)return(parseFloat(m[1])||0)+(parseFloat(m[2])||0)/60+(parseFloat(m[3])||0)/3600;return parseFloat(s)||0;}
function parseDec(s){s=s.trim().toLowerCase();let sign=1;if(s.startsWith('-')){sign=-1;s=s.substring(1);}else if(s.startsWith('+')){s=s.substring(1);}let m=s.match(/^(\d+(?:\.\d+)?)\s*[d\u00b0]\s*(?:(\d+(?:\.\d+)?)\s*[m'\u2032]?\s*)?(?:(\d+(?:\.\d+)?)\s*[s"\u2033]?\s*)?$/);if(m)return sign*((parseFloat(m[1])||0)+(parseFloat(m[2])||0)/60+(parseFloat(m[3])||0)/3600);m=s.match(/^(\d+(?:\.\d+)?)[\s:]+(\d+(?:\.\d+)?)(?:[\s:]+(\d+(?:\.\d+)?))?$/);if(m)return sign*((parseFloat(m[1])||0)+(parseFloat(m[2])||0)/60+(parseFloat(m[3])||0)/3600);return parseFloat(s)||0;}
function syncBrowserTime(){fetch('/time/set?timestamp='+Math.floor(Date.now()/1000)).then(r=>r.json()).then(d=>{if(d.ok)console.log('Time synced');});}
function checkAndSyncTime(){fetch('/time/status').then(r=>r.json()).then(d=>{if(!d.synced)syncBrowserTime();});}
function updateEphemeris(){fetch('/ephemeris').then(r=>r.json()).then(d=>{document.getElementById('sun_pos').textContent='Alt '+d.sun.alt.toFixed(1)+'\u00b0 Az '+d.sun.az.toFixed(1)+'\u00b0';document.getElementById('moon_pos').textContent='Alt '+d.moon.alt.toFixed(1)+'\u00b0 Az '+d.moon.az.toFixed(1)+'\u00b0';const bl=document.getElementById('bulge_label');const bp=document.getElementById('bulge_pos');if(d.bulge&&d.bulge.found){bl.textContent=d.bulge.bulge_visible?'Bulge':'Gal plane';bp.textContent=(d.bulge.bulge_visible?'':'l '+d.bulge.l.toFixed(1)+'\u00b0, ')+'Alt '+d.bulge.alt.toFixed(1)+'\u00b0 Az '+d.bulge.az.toFixed(1)+'\u00b0';}else{bl.textContent='Gal plane';bp.textContent='Below horizon';}});}
function toggleEthMode(){const isDhcp=document.getElementById('eth_dhcp').checked;const fields=document.getElementById('eth-static-fields');fields.style.opacity=isDhcp?'0.5':'1';const inputs=fields.querySelectorAll('input');inputs.forEach(i=>i.disabled=isDhcp);}
function saveEthSettings(){const dhcp=document.getElementById('eth_dhcp').checked?'1':'0';const params=new URLSearchParams();params.append('dhcp',dhcp);params.append('ip',document.getElementById('eth_static_ip').value);params.append('gateway',document.getElementById('eth_gateway').value);params.append('subnet',document.getElementById('eth_subnet').value);params.append('dns',document.getElementById('eth_dns').value);fetch('/eth/save?'+params.toString()).then(r=>r.json()).then(d=>{const st=document.getElementById('eth-save-status');if(d.ok){st.textContent='Saved! Reboot to apply.';st.style.color='#ffaa00';}else{st.textContent='Error: '+(d.error||'Unknown');st.style.color='#ff4444';}});}
function updateNetworkStatus(){fetch('/wifi/status').then(r=>r.json()).then(d=>{const ethSec=document.getElementById('eth-section');const wifiPowerSec=document.getElementById('wifi-power-section');if(d.eth_available){ethSec.style.display='block';document.getElementById('net_name').textContent=d.mdns||d.hostname||'--';if(d.eth_connected){document.getElementById('eth_status').textContent='Connected';document.getElementById('eth_status').className='value connected';document.getElementById('eth_ip').textContent=d.eth_ip;wifiPowerSec.style.display='block';const btn=document.getElementById('wifi_power_btn');if(d.wifi_enabled){btn.textContent='Disable WiFi';btn.className='btn';}else{btn.textContent='Enable WiFi';btn.className='btn btn-active';}}else{document.getElementById('eth_status').textContent='Disconnected';document.getElementById('eth_status').className='value disconnected';document.getElementById('eth_ip').textContent='--';wifiPowerSec.style.display='none';}document.getElementById('eth_mac').textContent=d.eth_mac||'--';document.getElementById('eth_dhcp').checked=d.eth_dhcp;document.getElementById('eth_static').checked=!d.eth_dhcp;document.getElementById('eth_static_ip').value=d.eth_static_ip||'';document.getElementById('eth_gateway').value=d.eth_gateway||'';document.getElementById('eth_subnet').value=d.eth_subnet||'';document.getElementById('eth_dns').value=d.eth_dns||'';toggleEthMode();}else{ethSec.style.display='none';wifiPowerSec.style.display='none';}const wifiStatusText=d.wifi_enabled?'':'(DISABLED) ';document.getElementById('ap_status').textContent=d.wifi_enabled?(d.ap_ssid||'--'):'Disabled';document.getElementById('ap_ip').textContent=d.wifi_enabled?(d.ap_ip||'--'):'--';document.getElementById('wifi_mac').textContent=d.wifi_mac||'--';if(d.wifi_enabled&&d.sta_connected){document.getElementById('sta_status').textContent=d.sta_ssid;document.getElementById('sta_status').className='value connected';document.getElementById('sta_ip').textContent=d.sta_ip;}else{document.getElementById('sta_status').textContent=d.wifi_enabled?'Not connected':'Disabled';document.getElementById('sta_status').className='value disconnected';document.getElementById('sta_ip').textContent='--';}document.getElementById('saved-network').textContent=d.saved_ssid||'None';});}
function scanWifi(){document.getElementById('wifi-networks').innerHTML='<p style="color:#888;">Scanning...</p>';fetch('/wifi/scan?restart=1').then(r=>r.json()).then(()=>pollWifiScan(0));}
function pollWifiScan(tries){const box=document.getElementById('wifi-networks');if(tries>20){box.innerHTML='<p style="color:#888;">Scan timed out</p>';return;}fetch('/wifi/scan').then(r=>r.json()).then(d=>{if(d.status==='running'){setTimeout(()=>pollWifiScan(tries+1),1000);return;}if(d.status==='failed'){box.textContent='Scan failed'+(d.error?(': '+d.error):'')+(d.start_rc!==undefined?(' (rc '+d.start_rc+'/'+d.complete_rc+')'):'');return;}const networks=d.networks||[];if(networks.length===0){box.innerHTML='<p style="color:#888;">No networks found</p>';return;}box.innerHTML='';networks.forEach(n=>{const sig=n.rssi>-50?'####':n.rssi>-60?'###-':n.rssi>-70?'##--':'#---';const div=document.createElement('div');div.className='wifi-network';div.setAttribute('data-help','Select this WiFi network and open the password form.');div.textContent=(n.secure?'[+] ':'')+n.ssid;const sp=document.createElement('span');sp.className='wifi-signal';sp.textContent=sig;div.appendChild(sp);div.onclick=()=>selectNetwork(n.ssid);box.appendChild(div);});}).catch(()=>setTimeout(()=>pollWifiScan(tries+1),1000));}
function selectNetwork(ssid){selectedSsid=ssid;document.getElementById('selected-ssid').textContent=ssid;document.getElementById('wifi-password').value='';document.getElementById('wifi-connect-form').classList.remove('hidden');}
function hideConnectForm(){document.getElementById('wifi-connect-form').classList.add('hidden');}
function connectWifi(){const pw=document.getElementById('wifi-password').value;const st=document.getElementById('wifi-networks');fetch('/wifi/connect?ssid='+encodeURIComponent(selectedSsid)+'&password='+encodeURIComponent(pw)).then(r=>r.json()).then(d=>{if(!d.ok){alert('Failed: '+(d.error||'Unknown'));return;}hideConnectForm();if(st)st.innerHTML='<p style="color:#888;">Connecting to '+'…'+'</p>';pollWifiConnect(0);}).catch(()=>{/* the AP may drop us as the radio retunes - keep polling anyway */hideConnectForm();pollWifiConnect(0);});}
// The controller cannot answer "did it work?" in the connect response: joining
// a network in AP+STA mode retunes the softAP, so a browser on the AP is
// dropped mid-request. Poll for the outcome instead, and tolerate errors while
// the radio settles.
function pollWifiConnect(tries){const st=document.getElementById('wifi-networks');if(tries>20){if(st)st.innerHTML='<p style="color:#888;">Still not connected - check the password and try again</p>';updateNetworkStatus();return;}fetch('/wifi/status').then(r=>r.json()).then(d=>{if(d.sta_connected){if(st)st.innerHTML='<p style="color:#5c5;">Connected'+(d.sta_ip?(' - IP '+d.sta_ip):'')+'</p>';updateNetworkStatus();return;}setTimeout(()=>pollWifiConnect(tries+1),1000);}).catch(()=>setTimeout(()=>pollWifiConnect(tries+1),1000));}
function forgetWifi(){if(confirm('Forget saved WiFi?')){fetch('/wifi/forget').then(()=>updateNetworkStatus());}}
function toggleWifiPower(){fetch('/wifi/status').then(r=>r.json()).then(d=>{const enable=!d.wifi_enabled;if(!enable&&!d.eth_connected){alert('Cannot disable WiFi without Ethernet connection');return;}fetch('/wifi/power?enable='+(enable?'1':'0')).then(r=>r.json()).then(r=>{if(r.ok){updateNetworkStatus();}else{alert('Error: '+(r.error||'Unknown'));}});});}
function trackSun(){if(currentTrackingTarget==='Sun')return;fetch('/track/sun').then(()=>updateStatus());}
function trackMoon(){if(currentTrackingTarget==='Moon')return;fetch('/track/moon').then(()=>updateStatus());}
function trackGalacticBulge(){if(currentTrackingTarget==='Galactic Bulge')return;handleTrackResponse(fetch('/track/galactic-bulge'));}
function handleTrackResponse(promise){promise.then(r=>r.json()).then(d=>{if(!d.ok){alert(d.error||'Track failed');}updateStatus();});}
function setAxisMode(mode){const params=new URLSearchParams();params.append('mode',mode);handleTrackResponse(fetch('/tracking/axis?'+params.toString()));}
function updateAxisMode(d){const azOnly=!!d.az_only;const altOnly=!!d.alt_only;document.getElementById('axis_az').classList.toggle('active',azOnly);document.getElementById('axis_alt').classList.toggle('active',altOnly);document.getElementById('axis_both').classList.toggle('active',!azOnly&&!altOnly);currentAxisMode=azOnly?'az':altOnly?'alt':'both';updateFixedAxisRow(d);}
function updateFixedAxisRow(d){const row=document.getElementById('axis_fixed_row');const label=document.getElementById('axis_fixed_label');const input=document.getElementById('axis_fixed_value');const apply=row?row.querySelector('button'):null;if(!row||!label||!input)return;const editing=document.activeElement===input;if(d.az_only){row.classList.add('active');label.textContent='Go to altitude';input.min='0';input.max='90';if(!editing)input.value=Number(d.az_only_alt||0).toFixed(1);input.setAttribute('data-help','Altitude to hold while only azimuth tracks.');if(apply)apply.setAttribute('data-help','Go to this altitude and keep tracking azimuth.');}else if(d.alt_only){row.classList.add('active');label.textContent='Go to azimuth';input.min='0';input.max='360';if(!editing)input.value=Number(d.alt_only_az||0).toFixed(1);input.setAttribute('data-help','Azimuth to hold while only altitude tracks.');if(apply)apply.setAttribute('data-help','Go to this azimuth and keep tracking altitude.');}else{row.classList.remove('active');}}
function applyFixedAxis(){if(currentAxisMode!=='az'&&currentAxisMode!=='alt')return;const v=document.getElementById('axis_fixed_value').value;const params=new URLSearchParams();params.append('mode',currentAxisMode);if(currentAxisMode==='az')params.append('alt',v);else params.append('az',v);handleTrackResponse(fetch('/tracking/axis?'+params.toString()));}
function updateTargetButtons(target){currentTrackingTarget=target||'';const sun=document.querySelector('button[onclick="trackSun()"]');const moon=document.querySelector('button[onclick="trackMoon()"]');const bulge=document.querySelector('button[onclick="trackGalacticBulge()"]');if(sun){const active=currentTrackingTarget==='Sun';sun.classList.toggle('target-active',active);sun.setAttribute('aria-disabled',active?'true':'false');sun.setAttribute('data-help',active?'The telescope is already tracking the Sun. Use Stop tracking or choose another target before starting it again.':'Track the Sun using the controller ephemeris and keep updating the telescope position as it moves.');}if(moon){const active=currentTrackingTarget==='Moon';moon.classList.toggle('target-active',active);moon.setAttribute('aria-disabled',active?'true':'false');moon.setAttribute('data-help',active?'The telescope is already tracking the Moon. Use Stop tracking or choose another target before starting it again.':'Track the Moon using the controller ephemeris and keep updating the telescope position as it moves.');}if(bulge){const active=currentTrackingTarget==='Galactic Bulge';bulge.classList.toggle('target-active',active);bulge.setAttribute('aria-disabled',active?'true':'false');bulge.setAttribute('data-help',active?'The telescope is already tracking the Galactic Bulge or nearest visible galactic plane point.':'Track the Galactic Bulge when visible, otherwise the nearest visible point on the galactic plane.');}}
function setCalibrator(on){fetch('/calibrator?on='+(on?'1':'0')).then(r=>r.json()).then(d=>{updateCalButton(d.calibrator);});}
function toggleCalibrator(){const isOn=document.getElementById('cal_on').classList.contains('active');setCalibrator(!isOn);}
function updateCalButton(on){document.getElementById('cal_on').classList.toggle('active',!!on);document.getElementById('cal_off').classList.toggle('active',!on);}
function goToRaDec(){const ra=parseRA(document.getElementById('ra').value);const dec=parseDec(document.getElementById('dec').value);fetch('/goto?ra='+ra+'&dec='+dec).then(r=>r.json()).then(d=>{if(!d.ok){alert(d.error||'Goto failed');}updateStatus();});}
function trackRaDec(){const ra=parseRA(document.getElementById('ra').value);const dec=parseDec(document.getElementById('dec').value);fetch('/track/radec?ra='+ra+'&dec='+dec).then(r=>r.json()).then(d=>{if(!d.ok){alert(d.error||'Track failed');}updateStatus();});}
function goToGalactic(){const l=document.getElementById('gal_l').value;const b=document.getElementById('gal_b').value;fetch('/goto/galactic?l='+l+'&b='+b).then(r=>r.json()).then(d=>{if(!d.ok){alert(d.error||'Goto failed');}else{document.getElementById('galactic_radec').textContent='RA: '+formatRA(d.ra)+' Dec: '+formatDec(d.dec);}updateStatus();});}
function trackGalactic(){const l=document.getElementById('gal_l').value;const b=document.getElementById('gal_b').value;fetch('/track/galactic?l='+l+'&b='+b).then(r=>r.json()).then(d=>{if(!d.ok){alert(d.error||'Track failed');}else{document.getElementById('galactic_radec').textContent='RA: '+formatRA(d.ra)+' Dec: '+formatDec(d.dec);}updateStatus();});}
function stopTracking(){stopAll();}
function stopSlewing(){fetch('/stop/slewing').then(()=>updateStatus());}
function stopMovement(){stopSlewing();}
function stopTrackingOnly(){fetch('/stop/tracking').then(()=>updateStatus());}
function stopScheduledRun(){schedulerFetch('/api/stop',{method:'POST'}).then(r=>r.json()).then(d=>{if(!d.success)alert('No scheduled run was stopped.');}).catch(()=>alert('Scheduler is not responding on '+SCHEDULER_URL));}
function startQuickSunScan(){schedulerFetch('/api/sunscan/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(r=>r.json()).then(d=>{if(!d.success){alert(d.error||'Sun Scan did not start.');return;}alert('Sun Scan started. Open the scheduler for progress and settings.');}).catch(()=>alert('Scheduler is not responding on '+SCHEDULER_URL));}
function startQuickCalDay(){schedulerFetch('/api/calday/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(r=>r.json()).then(d=>{if(!d.success){alert(d.error||'Calibration Day did not start.');return;}alert('Calibration Day run started. Open the scheduler for progress and settings.');}).catch(()=>alert('Scheduler is not responding on '+SCHEDULER_URL));}
function stopAll(){fetch('/stop/all').then(()=>updateStatus());schedulerFetch('/api/stop_all',{method:'POST'}).catch(()=>console.log('Scheduler stop_all not available'));}
function updateFirmware(){const msg='This is not a normal website update. It will build and flash the ESP32 firmware over the network using PlatformIO OTA. Only continue if you have changed firmware files, you expect them to build, and you are sure the telescope can safely stop while the ESP32 reboots. After upload success, the controller website may be unavailable while Ethernet restarts; allow up to about 100 seconds before refreshing.';if(!confirm(msg))return;schedulerFetch('/api/firmware/update',{method:'POST'}).then(r=>r.json()).then(d=>{if(!d.success){alert(d.error||'Firmware update did not start.');return;}alert('Firmware update started. Watch the scheduler Log tab for progress. After the upload reports OK, the ESP32 reboots and the controller website may be unavailable for up to about 100 seconds.');}).catch(()=>alert('Scheduler is not responding on '+SCHEDULER_URL+'. Start the scheduler first, then click Update firmware again.'));}
function resetFault(){fetch('/reset').then(r=>r.json()).then(d=>{if(!d.ok){alert(d.error||'Reset failed');}updateStatus();});}
function goDirect(){const alt=document.getElementById('direct_alt').value;const az=document.getElementById('direct_az').value;fetch('/direct?alt='+alt+'&az='+az).then(()=>updateStatus());}
function goHome(){fetch('/go-home').then(()=>updateStatus());}
function runHoming(){fetch('/home').then(r=>r.json()).then(d=>{if(!d.ok){alert(d.error||'Homing failed');}updateStatus();});}
function setOffset(){const alt=document.getElementById('offset_alt').value;const az=document.getElementById('offset_az').value;fetch('/offset?alt='+alt+'&az='+az).then(r=>r.json()).then(d=>{document.getElementById('current_offset').textContent=d.offset_alt.toFixed(1)+'\u00b0 / '+d.offset_az.toFixed(1)+'\u00b0';});}
function clearOffset(){fetch('/offset/clear').then(r=>r.json()).then(d=>{document.getElementById('offset_alt').value='0';document.getElementById('offset_az').value='0';document.getElementById('current_offset').textContent='0.0\u00b0 / 0.0\u00b0';});}
let isSlewing=false;let refreshInterval=null;
function setFaultLocked(locked){faultLocked=locked;document.querySelectorAll('#tab-control .fault-dependent').forEach(el=>{el.disabled=locked;el.classList.toggle('locked',locked);});}
function scheduleRefresh(){if(refreshInterval)clearInterval(refreshInterval);refreshInterval=setInterval(updateStatus,isSlewing?500:1000);}
function updateStatus(){fetch('/status').then(r=>r.json()).then(d=>{document.getElementById('alt').textContent=d.alt.toFixed(2)+'\u00b0';document.getElementById('az').textContent=d.az.toFixed(2)+'\u00b0';if(d.ra!==undefined){document.getElementById('cur_ra').textContent=formatRA(d.ra);document.getElementById('cur_dec').textContent=formatDec(d.dec);document.getElementById('cur_gl').textContent=d.gal_l.toFixed(2)+'\u00b0';document.getElementById('cur_gb').textContent=d.gal_b.toFixed(2)+'\u00b0';}document.getElementById('alt_a').textContent=d.alt_current_a.toFixed(2)+' A';document.getElementById('az_a').textContent=d.az_current_a.toFixed(2)+' A';document.getElementById('status').textContent=d.status;document.getElementById('status').className='value '+(d.is_slewing?'tracking':'idle');document.getElementById('error_status').textContent=d.fault_active?(d.fault||'FAULT'):'Clear';document.getElementById('error_status').className='value '+(d.fault_active?'disconnected':'connected');const resetBtn=document.getElementById('reset_btn');resetBtn.disabled=!d.fault_active;resetBtn.className=d.fault_active?'stop':'secondary';setFaultLocked(!!d.fault_active);if(d.is_slewing!==isSlewing){isSlewing=d.is_slewing;scheduleRefresh();}updateCalButton(d.calibrator);});fetch('/tracking').then(r=>r.json()).then(d=>{updateAxisMode(d);updateTargetButtons(d.enabled?d.target_name:'');if(d.enabled){let info=d.target_name||'RA/Dec';info+=': '+formatRA(d.ra)+' '+formatDec(d.dec);if(d.az_only){info+=' [Az only, Alt '+d.az_only_alt.toFixed(1)+'\u00b0]';}if(d.alt_only){info+=' [Alt only, Az '+d.alt_only_az.toFixed(1)+'\u00b0]';}if(d.waiting_for_rise){info+=' [Below horizon]';document.getElementById('tracking_target').className='value disconnected';}else if(d.waiting_for_wrap){info+=' [Az limits]';document.getElementById('tracking_target').className='value disconnected';}else{document.getElementById('tracking_target').className='value tracking';}if(d.offset_alt!==0||d.offset_az!==0){info+=' [+'+d.offset_alt.toFixed(1)+'/'+d.offset_az.toFixed(1)+']';}document.getElementById('tracking_target').textContent=info;}else{document.getElementById('tracking_target').textContent='Off';document.getElementById('tracking_target').className='value idle';}document.getElementById('current_offset').textContent=d.offset_alt.toFixed(1)+'\u00b0 / '+d.offset_az.toFixed(1)+'\u00b0';});fetch('/time/status').then(r=>r.json()).then(d=>{let ts=d.utc+' UTC';let cls='value connected';if(d.sync_state==='ok'){ts+=' (NTP '+formatAge(d.last_sync_age_s)+' ago)';}else if(d.sync_state==='stale'){ts+=' (STALE - no NTP for '+formatAge(d.last_sync_age_s)+')';cls='value disconnected';}else if(d.sync_state==='unverified'){ts+=' (browser set, NOT NTP verified)';cls='value disconnected';}else{ts='NOT SYNCED';cls='value disconnected';}document.getElementById('time_status').className=cls;document.getElementById('time_status').textContent=ts;});}
function formatAge(s){if(s===undefined||s<0)return'never';if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m';return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m';}
function loadSettings(){fetch('/settings').then(r=>r.json()).then(d=>{document.getElementById('set_lat').value=d.observer_lat;document.getElementById('set_lon').value=d.observer_lon;document.getElementById('set_az_min').value=d.mount_az_min;document.getElementById('set_az_max').value=d.mount_az_max;document.getElementById('set_alt_min').value=d.mount_alt_min;document.getElementById('set_alt_max').value=d.mount_alt_max;document.getElementById('set_home_alt').value=d.home_alt;document.getElementById('set_home_az').value=d.home_az;document.getElementById('set_deadband').value=d.position_deadband;document.getElementById('set_ap_ssid').value=d.ap_ssid;document.getElementById('set_ap_pass').value=d.ap_password;document.getElementById('set_page_name').value=d.page_name;const cb=document.getElementById('set_hover_help');if(cb)cb.checked=hoverHelpEnabled;document.getElementById('page-title').textContent=d.page_name;document.title=d.page_name;document.getElementById('settings-status').textContent='';});}
function saveSettings(){const params=new URLSearchParams();params.append('observer_lat',document.getElementById('set_lat').value);params.append('observer_lon',document.getElementById('set_lon').value);params.append('mount_az_min',document.getElementById('set_az_min').value);params.append('mount_az_max',document.getElementById('set_az_max').value);params.append('mount_alt_min',document.getElementById('set_alt_min').value);params.append('mount_alt_max',document.getElementById('set_alt_max').value);params.append('home_alt',document.getElementById('set_home_alt').value);params.append('home_az',document.getElementById('set_home_az').value);params.append('position_deadband',document.getElementById('set_deadband').value);params.append('ap_ssid',document.getElementById('set_ap_ssid').value);params.append('ap_password',document.getElementById('set_ap_pass').value);params.append('page_name',document.getElementById('set_page_name').value);fetch('/settings/save?'+params.toString()).then(r=>r.json()).then(d=>{document.getElementById('settings-status').textContent=d.ok?'Settings saved!':'Save failed';document.getElementById('settings-status').style.color=d.ok?'#00ff00':'#ff4444';if(d.ok){document.getElementById('page-title').textContent=document.getElementById('set_page_name').value;document.title=document.getElementById('set_page_name').value;}});}
function resetSettings(){if(confirm('Reset all settings to defaults?')){fetch('/settings/reset').then(r=>r.json()).then(d=>{if(d.ok){loadSettings();document.getElementById('settings-status').textContent='Reset to defaults';document.getElementById('settings-status').style.color='#ffaa00';}});}}
function loadPageName(){fetch('/settings').then(r=>r.json()).then(d=>{document.getElementById('page-title').textContent=d.page_name;document.title=d.page_name;});}
let serialExpanded=true;
function toggleSerialPanel(){const log=document.getElementById('serial-log');const tog=document.getElementById('serial-toggle');serialExpanded=!serialExpanded;log.classList.toggle('collapsed',!serialExpanded);tog.textContent=serialExpanded?'\u25BC':'\u25B2';}
function updateSerialLog(){fetch('/serial/log').then(r=>r.json()).then(entries=>{const log=document.getElementById('serial-log');const wasAtBottom=log.scrollHeight-log.scrollTop<=log.clientHeight+5;let html='';entries.forEach(e=>{html+='<div class="log-line log-'+e.dir.toLowerCase()+'"><span class="log-time">'+e.time+'</span><span class="log-dir">['+e.dir+']</span> '+e.msg+'</div>';});log.innerHTML=html;if(wasAtBottom)log.scrollTop=log.scrollHeight;});}
setInterval(updateEphemeris,10000);setInterval(updateSerialLog,1000);initHoverHelp();scheduleRefresh();updateStatus();updateEphemeris();checkAndSyncTime();loadPageName();updateSerialLog();
</script>
</body>
</html>)rawliteral";

#endif
