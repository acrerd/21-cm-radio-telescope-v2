// Auto-generated - embedded index.html
#ifndef INDEX_HTML_H
#define INDEX_HTML_H

const char INDEX_HTML[] PROGMEM = R"rawliteral(<!DOCTYPE html>
<html>
<head>
    <title>SRT Controller</title>
    <meta charset="utf-8">
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
/* Motor current bars: |I| against the Due's 5 A overcurrent limit, eased between polls. */
.ibar { flex: 1; height: 10px; margin: 0 10px; align-self: center; background: #1b1b2b; border: 1px solid #3a3a55; border-radius: 5px; overflow: hidden; position: relative; }
.ifill { position: absolute; left: 0; top: 0; bottom: 0; width: 0; background: #4caf50; border-radius: 5px; transition: width 0.45s ease, background-color 0.45s ease; }
.ifill.warm { background: #ffaa00; } .ifill.hot { background: #ff4757; }
@media (prefers-reduced-motion: reduce) { .ifill { transition: none; } }
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
        button.stop-all { background: #b00020; color: #fff; font-weight: bold; }
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
        button.galactic-plane { background: #f2e9ff; color: #2d124d; }
        button.galactic-plane:hover { background: #d8b8ff; }
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
                <button class="secondary" onclick="updateFirmware()" data-help="Ask the scheduler to build and flash this controller&#39;s firmware over the network. Not the Due: that is flashed over USB from the observatory computer (see Help).">Update firmware</button>
                <button class="secondary" onclick="restartController()" data-help="Reboot this controller without changing its firmware or settings - for example after saving Ethernet settings. Refused while tracking, homing or moving. The Arduino Due is not restarted.">Restart</button>
            </div>
        </div>
        <div class="tab-bar">
            <div class="tab active" onclick="showTab('control')">Control</div>
            <div class="tab" onclick="showTab('network')">Network</div>
            <div class="tab" onclick="showTab('pointing')">Pointing</div>
            <div class="tab" onclick="showTab('settings')">Settings</div>
            <div class="tab" onclick="showTab('help')">Help</div>
        </div>
        <div id="tab-control" class="tab-content active">
            <div class="two-col">
                <div class="left-col">
                    <div class="box">
                        <h3>Current Status</h3>
                        <div class="status-row"><span class="label">Altitude (sky):</span><span class="value" id="alt">--</span></div>
                        <div class="status-row"><span class="label">Azimuth (sky):</span><span class="value" id="az">--</span></div>
                        <div class="status-row"><span class="label">Drive Alt/Az:</span><span class="value" id="drive_pos">--</span></div>
                        <div class="status-row"><span class="label">RA:</span><span class="value" id="cur_ra">--</span></div>
                        <div class="status-row"><span class="label">Dec:</span><span class="value" id="cur_dec">--</span></div>
                        <div class="status-row"><span class="label">Gal l:</span><span class="value" id="cur_gl">--</span></div>
                        <div class="status-row"><span class="label">Gal b:</span><span class="value" id="cur_gb">--</span></div>
                        <div class="status-row"><span class="label">Alt Motor:</span><span class="ibar" data-help="Altitude motor current, its magnitude against the 5 A overcurrent limit: green under 3 A, amber to 4 A, red above."><span class="ifill" id="alt_a_bar"></span></span><span class="value" id="alt_a">-- A</span></div>
                        <div class="status-row"><span class="label">Az Motor:</span><span class="ibar" data-help="Azimuth motor current, its magnitude against the 5 A overcurrent limit: green under 3 A, amber to 4 A, red above."><span class="ifill" id="az_a_bar"></span></span><span class="value" id="az_a">-- A</span></div>
                        <div class="status-row"><span class="label">Status:</span><span class="value" id="status">--</span></div>
                        <div class="status-row"><span class="label">Error Status:</span><span class="value" id="error_status">--</span></div>
                        <div class="status-row"><span class="label">Tracking:</span><span class="value" id="tracking_target">--</span></div>
                        <div class="status-row"><span class="label">Time:</span><span class="value" id="time_status">--</span></div>
                    </div>
                    <div class="box">
                        <h3>Actions</h3>
                        <div class="action-row">
                            <button class="stop-all fault-dependent" onclick="stopAll()" data-help="Stop the mount now, cancel tracking, and stop the scheduler&#39;s current run.">STOP</button>
                            <button class="fault-dependent" onclick="goHome()" data-help="Park the dish at the stow position set in Settings.">Stow</button>
                            <button class="secondary" id="reset_btn" onclick="resetFault()" disabled data-help="Clear a fault, once the telescope is safe to move.">Reset</button>
                            <button class="fault-dependent" onclick="runHoming()" data-help="Cancel tracking and re-find the position: both axes run to their lower limit switches, and each counter is zeroed on the first encoder edge there. Takes 1-3 minutes.">Home</button>
                        </div>
                    </div>
                </div>
                <div class="right-col">
                    <div class="box">
                        <h3>Target</h3>
                        <div class="coord-row">
                            <label>Target: <select class="fault-dependent" id="target_kind" onchange="targetKindChanged()" data-help="What to point at. Everything on the sky is tracked; an Alt/Az position is a place to park.">
                                <option value="sun">Sun</option>
                                <option value="moon">Moon</option>
                                <option value="plane">Galactic plane</option>
                                <option value="radec">RA / Dec (J2000)</option>
                                <option value="gal">Galactic l / b</option>
                                <option value="altaz">Alt / Az (park)</option>
                            </select></label>
                        </div>
                        <div class="coord-row" id="target_coords">
                            <label><span id="t1_label">RA</span>: <input class="fault-dependent" type="text" id="t1" value="0"></label>
                            <label><span id="t2_label">Dec</span>: <input class="fault-dependent" type="text" id="t2" value="0"></label>
                        </div>
                        <div class="btn-row">
                            <button class="fault-dependent" id="target_btn" onclick="targetGo()">Track</button>
                        </div>
                        <div class="target-info" id="target_info"></div>
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
                                <div class="coord-row"><label>IP: <input type="text" id="eth_static_ip" placeholder="192.168.50.120"></label></div>
                                <div class="coord-row"><label>Gateway: <input type="text" id="eth_gateway" placeholder="192.168.50.1"></label></div>
                                <div class="coord-row"><label>Subnet: <input type="text" id="eth_subnet" placeholder="255.255.255.0"></label></div>
                                <div class="coord-row"><label>DNS: <input type="text" id="eth_dns" placeholder="192.168.50.1"></label></div>
                            </div>
                            <div class="btn-row">
                                <button onclick="saveEthSettings()" data-help="Save the Ethernet DHCP or static IP settings. Reboot the controller to apply them. Leave on DHCP: the observatory computer's DHCP reservation gives this controller 192.168.50.120 on the private link.">Save Ethernet</button>
                            </div>
                            <p id="eth-save-status" class="target-info"></p>
                        </div>
                    </div>
                    <div class="box" id="wifi-power-section" style="display:none;">
                        <h3>WiFi Power</h3>
                        <p style="color:#888;font-size:0.9em;margin:5px 0;">Disable WiFi to save ~100mA when using Ethernet</p>
                        <button id="wifi_power_btn" onclick="toggleWifiPower()" data-help="Turn WiFi on or off. WiFi cannot be disabled unless Ethernet is connected. Off also removes the 192.168.4.1 access point, the way in if the Ethernet link fails.">Disable WiFi</button>
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
        <div id="tab-pointing" class="tab-content">
            <div class="two-col">
                <div>
                    <div class="box">
                        <h3>Stored Pointing Model</h3>
                        <div class="status-row"><span class="label">State:</span><span class="value" id="pm_state">--</span></div>
                        <div class="status-row"><span class="label">Fitted:</span><span class="value" id="pm_fitted">--</span></div>
                        <div class="status-row"><span class="label">Scans:</span><span class="value" id="pm_scans">--</span></div>
                        <div class="status-row"><span class="label">Schema:</span><span class="value" id="pm_version">--</span></div>
                        <h4>Terms</h4>
                        <div class="status-row"><span class="label">IE (alt zero):</span><span class="value" id="pm_IE">--</span></div>
                        <div class="status-row"><span class="label">IA (az zero):</span><span class="value" id="pm_IA">--</span></div>
                        <div class="status-row"><span class="label">AN (tilt north):</span><span class="value" id="pm_AN">--</span></div>
                        <div class="status-row"><span class="label">AE (tilt east):</span><span class="value" id="pm_AE">--</span></div>
                        <div class="status-row"><span class="label">CA (collimation):</span><span class="value" id="pm_CA">--</span></div>
                        <div class="status-row"><span class="label">NPAE (axis skew):</span><span class="value" id="pm_NPAE">--</span></div>
                        <div class="status-row"><span class="label">TF (flexure):</span><span class="value" id="pm_TF">--</span></div>
                        <div class="status-row"><span class="label">AZSCALE (az encoder scale):</span><span class="value" id="pm_AZSCALE">--</span></div>
                        <div class="btn-row">
                            <button class="secondary" onclick="loadPointing()" data-help="Reload the stored pointing model from the controller.">Reload</button>
                            <button class="stop" onclick="clearPointing()" data-help="Erase the stored pointing model. The telescope then points with no calibration applied, which is what you want after mechanical work.">Clear calibration</button>
                        </div>
                        <p id="pointing-status" class="target-info"></p>
                    </div>
                </div>
                <div>
                    <div class="box">
                        <h3>Pointing Offset</h3>
                        <div class="coord-row">
                            <label>dAlt: <input type="number" id="offset_alt" step="0.5" min="-45" max="45" value="0"></label>
                            <label>dAz: <input type="number" id="offset_az" step="0.5" min="-45" max="45" value="0"></label>
                        </div>
                        <div class="btn-row">
                            <button onclick="setOffset()" data-help="Apply the Alt/Az offset to tracking commands for scanning or mapping.">Apply Offset</button>
                            <button class="secondary" onclick="clearOffset()" data-help="Clear the pointing offset and return tracking commands to the unshifted target.">Clear</button>
                        </div>
                        <div class="target-info">Current offset: <span id="current_offset">0.0 / 0.0</span>. A sky-frame shift on top of the pointing model for every tracked target, until cleared; it should read 0 / 0 in normal observing, and the Control tab shows it beside the target when it does not.</div>
                    </div>
                    <div class="box">
                        <h3>Load a Model</h3>
                        <p class="target-info">Upload a model document: JSON with <code>version</code> and <code>terms</code>, under 1536 bytes &mdash; what the scheduler builds and sends to <code>/pointing/apply</code> when it applies a fit. The scheduler&#39;s own <code>pointing_model.json</code> is the full fit record and is refused (no <code>version</code>, too large). Both routes write the same stored model.</p>
                        <div class="coord-row">
                            <input type="file" id="pm_file" accept=".json,application/json">
                        </div>
                        <div class="btn-row">
                            <button onclick="uploadPointing()" data-help="Send the selected pointing model file to the controller and store it in flash.">Upload model</button>
                        </div>
                    </div>
                    <div class="box">
                        <h3>What This Is</h3>
                        <p style="font-size:0.85em;">The model converts a <strong>true sky</strong> position into the <strong>drive</strong> position that puts the beam on it, and back again for display. It is stored in the controller&#39;s flash, in its own namespace, and survives a power cycle and Reset to Defaults.</p>
                        <p style="font-size:0.85em;">It is applied to every goto, track and direct Alt/Az move. The stow is not passed through it: stow is a drive position. The dAlt/dAz boxes on the Control tab are a separate, deliberate offset on the sky, layered on top of this - they should read 0/0 in normal observing.</p>
                        <p style="font-size:0.85em;">Refraction is applied whether or not a model is stored; it is physics, not calibration.</p>
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
                        <p class="target-info">The telescope&#39;s <strong>true</strong> position. Mount tilt does not belong here &mdash; it is a pointing term, shown below.</p>
                    </div>
                    <div class="box">
                        <h3>Pointing Calibration</h3>
                        <div class="status-row"><span class="label">State:</span><span class="value" id="pmset_state">--</span></div>
                        <div class="status-row"><span class="label">Fitted:</span><span class="value" id="pmset_fitted">--</span></div>
                        <div class="status-row"><span class="label">Scans:</span><span class="value" id="pmset_scans">--</span></div>
                        <div class="status-row"><span class="label">IE / IA (zero points):</span><span class="value" id="pmset_ieia">--</span></div>
                        <div class="status-row"><span class="label">AN / AE (axis tilt):</span><span class="value" id="pmset_anae">--</span></div>
                        <div class="status-row"><span class="label">CA / NPAE / TF:</span><span class="value" id="pmset_extra">--</span></div>
                        <div class="status-row"><span class="label">AZSCALE (az encoder scale):</span><span class="value" id="pmset_azscale">--</span></div>
                        <p class="target-info">Read-only here. These terms convert a true sky position into the drive position that puts the beam on it. Load or clear a model on the Pointing tab.</p>
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
                        <h3>Observing Horizon</h3>
                        <div class="coord-row">
                            <label>Min sky Alt: <input type="number" id="set_horizon_alt" step="0.5" min="0" max="90"></label>
                        </div>
                        <p class="target-info">Lowest true sky altitude worth observing - trees and buildings. Checked before the pointing model: a Go To or Track below it is refused, and a tracked target that sets below it parks the dish at the stow. The Alt Min above is a drive-frame mount limit, checked after the model.</p>
                        <div class="coord-row">
                            <label>Galactic plane acquire above: <input type="number" id="set_galactic_min_alt" step="0.5" min="0" max="90"></label>
                        </div>
                        <p class="target-info">Where Track Galactic Plane is allowed to start. It picks the point on the plane nearest the galactic centre that is this high, then follows it down until the horizon above parks the dish.</p>
                    </div>
                    <div class="box">
                        <h3>Stow Position (mount)</h3>
                        <div class="coord-row">
                            <label>Stow Alt: <input type="number" id="set_stow_alt" step="0.5" min="0" max="90"></label>
                            <label>Stow Az: <input type="number" id="set_stow_az" step="0.5" min="0" max="360"></label>
                        </div>
                        <p class="target-info">Where the dish parks when a tracked target sets below the observing horizon, and where the Stow button sends it. <strong>Mount coordinates</strong> &mdash; the pointing calibration is not applied, so the mount rests exactly here (clamped to the limits). Parking is mechanical, not an observation, and at the zenith azimuth points at no particular sky. Not the Due&#39;s HOMEALT/HOMEAZ: the limit switches are always counter zero, and those only say how far past the switches a homing drives before it stops and sets the counter to them. Both are 0, so a homing ends at the switches.</p>
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
                            <label>AP Password: <input type="text" id="set_ap_pass" maxlength="63" placeholder="(unchanged - enter 8-63 chars to replace)"></label>
                        </div>
                        <p class="target-info">Changes take effect after reboot</p>
                    </div>
                    <div class="box">
                        <div class="btn-row">
                            <button onclick="saveSettings()" data-help="Save observer location, limits, observing horizon, stow position, display, and access point settings.">Save Settings</button>
                            <button class="secondary" onclick="loadSettings()" data-help="Reload settings from the controller and discard unsaved edits.">Reload</button>
                            <button class="stop" onclick="resetSettings()" data-help="Restore controller settings to their firmware defaults. The pointing model is kept.">Reset to Defaults</button>
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
                        <p><strong>Alt/Az</strong>: Altitude 0-90, Azimuth 0-350</p>
                        <p style="font-size:0.85em;"><strong>Sky</strong> alt/az is where the dish looks. <strong>Drive</strong> alt/az is what the mount reads on its encoders. The Pointing tab holds the model that converts between them.</p>
                    </div>
                    <div class="box">
                        <h3>RA/Dec Input Formats</h3>
                        <p style="font-size:0.85em;"><strong>RA:</strong> 12.5 | 12h30m | 12h30m00s | 12:30:00</p>
                        <p style="font-size:0.85em;"><strong>Dec:</strong> -45.5 | -45d30m | +45d30m00s | 45:30:00</p>
                    </div>
                    <div class="box">
                        <h3>Tracking Modes</h3>
                        <p><strong>Track:</strong> every sky target (Sun, Moon, galactic plane, RA/Dec, l/b) is followed in both axes as the Earth rotates. An Alt/Az position is a fixed place to park, not tracked. Drift scans are booked through the scheduler, which parks on the drive grid and records the crossing time.</p>
                    </div>
                    <div class="box">
                        <h3>Pointing Offset</h3>
                        <p style="font-size:0.9em;">Add Alt/Az offset for scanning or mapping. Added to the tracked target&#39;s sky alt and az before the pointing model, until cleared; dAz is degrees of azimuth, not cross-elevation. Not applied to an Alt/Az park or the stow. Should read 0/0 in normal observing. Set and cleared on the Pointing tab; the Control tab shows any offset beside the tracked target.</p>
                    </div>
                </div>
                <div>
                    <div class="box">
                        <h3>Mount Limits</h3>
                        <p style="font-size:0.9em;"><strong>Altitude:</strong> 0 to 90 degrees (default)</p>
                        <p style="font-size:0.9em;"><strong>Azimuth:</strong> 2 to 350 degrees (default; the cabling is strained beyond 350)</p>
                        <p style="font-size:0.9em;">Drive-frame limits, set under Software Limits and applied after the pointing model. An Alt/Az park outside them is refused. While tracking, a target outside the azimuth range or above Alt Max is held where it is until it comes back; below Alt Min it is clamped. A target below the observing horizon parks the dish at the stow.</p>
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
                        <p style="font-size:0.85em;"><code>/status</code> - Drive alt/az, sky true_alt/true_az, RA/Dec and l/b from the sky position, state</p>
                        <p style="font-size:0.85em;"><code>/diag</code> - This boot and the previous one: reset reason, last loop stage, a panic's backtrace</p>
                        <p style="font-size:0.85em;"><code>/track/sun</code>, <code>/track/moon</code>, <code>/track/galactic-plane</code> - Track that target</p>
                        <p style="font-size:0.85em;"><code>/track/radec?ra=X&amp;dec=Y</code> - Track J2000</p>
                        <p style="font-size:0.85em;"><code>/track/galactic?l=X&amp;b=Y</code> - Track galactic</p>
                        <p style="font-size:0.85em;"><code>/direct?alt=X&amp;az=Y</code> - Park at a sky alt/az (model applied, not tracked)</p>
                        <p style="font-size:0.85em;"><code>/go-home</code> - Stow; <code>/home</code> - homing; <code>/stop/all</code> - stop; <code>/reset</code> - clear a fault</p>
                        <p style="font-size:0.85em;"><code>/tracking/axis?mode=az|alt|both</code> - Single-axis tracking; kept for scripts, not offered on this page</p>
                        <p style="font-size:0.85em;"><code>/offset?alt=X&amp;az=Y</code> - Set pointing offset</p>
                        <p style="font-size:0.85em;"><code>/pointing</code> - Stored pointing model</p>
                        <p style="font-size:0.85em;"><code>/pointing/apply</code> (POST) - Store a model document</p>
                        <p style="font-size:0.85em;"><code>/pointing/clear</code> - Erase the pointing model</p>
                    </div>
                    <div class="box">
                        <h3>Due Serial Commands</h3>
                        <p style="font-size:0.85em;"><code>&lt;alt&gt; &lt;az&gt;</code> - Drive to a drive position; answered on the controller link with <code>ACK DRIVE &lt;alt&gt; &lt;az&gt;</code> (as rounded to the pulse grid) or <code>ERR DRIVE fault|homing|limits</code></p>
                        <p style="font-size:0.85em;"><code>HOME</code> - Run the homing; announces &quot;(from an unknown position)&quot; after a reset or an interrupted homing</p>
                        <p style="font-size:0.85em;"><code>RESET</code> - Clear a fault</p>
                        <p style="font-size:0.85em;"><code>STOP</code> - Stop both axes. During a homing it aborts into a fault: RESET, then HOME</p>
                        <p style="font-size:0.85em;"><code>STATUS</code> - Drive position and state; answered during a homing too. The line ends in <code>*HH</code>, the XOR of every character before it, and the controller rejects a line whose checksum fails</p>
                    </div>
                    <div class="box">
                        <h3>Firmware Updates</h3>
                        <p style="font-size:0.9em;"><strong>Update firmware</strong> (top right) asks the scheduler on the observatory computer to build <em>this controller&#39;s</em> firmware from its copy of the repository and flash it over the network. The controller restarts, so tracking stops for about a minute; the scheduler re-points a running tracked observation. The build&#39;s ELF is kept for decoding a crash, and the build date shows under About. It is refused while an observation or scan is running or the mount is tracking, homing or moving, and does nothing if the controller already runs the same code (a hash of the source, reported under About). <strong>Restart</strong>, beside it, reboots the controller without flashing anything, under the same refusals; the Due carries on as it was.</p>
                        <p style="font-size:0.9em;">It does <strong>not</strong> update the Arduino Due, which drives the motors. The Due is flashed over its USB programming port from the observatory computer (<code>pio run -e due -t upload</code>). Every Due flash restarts it with its counters at zero wherever the dish is, and it homes at once; that homing is reported as &quot;from an unknown position&quot;. When a change touches the link between the two boards, flash the controller first and the Due second.</p>
                    </div>
                    <div class="box">
                        <h3>About</h3>
                        <p style="font-size:0.9em;">SRT Controller<br>Acre Road Observatory, Glasgow<br><span id="build_info" style="font-size:0.9em; color:#888;">firmware built --</span></p>
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
function showTab(name){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));event.target.classList.add('active');document.getElementById('tab-'+name).classList.add('active');if(name==='network')updateNetworkStatus();if(name==='settings'){loadSettings();loadPointing();}if(name==='pointing')loadPointing();}
function fmtTerm(v){return (typeof v==='number')?(v>=0?'+':'')+v.toFixed(4)+'\u00b0':'--';}
// AZSCALE is degrees of error per degree of azimuth, not an angle, so it gets
// percent and what that comes to across the mount's range rather than fmtTerm.
function fmtScale(v){if(typeof v!=='number')return '--';var pc=(v>=0?'+':'')+(v*100).toFixed(3)+'%';return pc+' ('+(v*355>=0?'+':'')+(v*355).toFixed(2)+'\u00b0 across 355\u00b0)';}
function showPointing(d){const t=d.terms||{};document.getElementById('pm_state').textContent=d.loaded?'Model applied':'No model - identity transform';document.getElementById('pm_state').className='value '+(d.loaded?'tracking':'idle');document.getElementById('pm_fitted').textContent=d.fitted_utc||'--';document.getElementById('pm_scans').textContent=d.n_scans||'--';document.getElementById('pm_version').textContent=d.version||'--';['IE','IA','AN','AE','CA','NPAE','TF'].forEach(k=>{document.getElementById('pm_'+k).textContent=fmtTerm(t[k]);});var az=document.getElementById('pm_AZSCALE');if(az)az.textContent=fmtScale(t.AZSCALE);const st=document.getElementById('pmset_state');if(st){st.textContent=d.loaded?'Model applied':'No model - identity transform';st.className='value '+(d.loaded?'tracking':'idle');document.getElementById('pmset_fitted').textContent=d.fitted_utc||'--';document.getElementById('pmset_scans').textContent=d.n_scans||'--';document.getElementById('pmset_ieia').textContent=fmtTerm(t.IE)+' / '+fmtTerm(t.IA);document.getElementById('pmset_anae').textContent=fmtTerm(t.AN)+' / '+fmtTerm(t.AE);document.getElementById('pmset_extra').textContent=fmtTerm(t.CA)+' / '+fmtTerm(t.NPAE)+' / '+fmtTerm(t.TF);var azs=document.getElementById('pmset_azscale');if(azs)azs.textContent=fmtScale(t.AZSCALE);}}
function loadPointing(){fetch('/pointing').then(r=>r.json()).then(d=>{showPointing(d);document.getElementById('pointing-status').textContent='';});}
function uploadPointing(){const f=document.getElementById('pm_file').files[0];const st=document.getElementById('pointing-status');if(!f){st.style.color='#ff4444';st.textContent='Choose a pointing_model.json file first.';return;}f.text().then(text=>fetch('/pointing/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:text})).then(r=>r.json()).then(d=>{if(d.ok){showPointing(d.model);st.style.color='#00ff00';st.textContent='Model stored in flash.';}else{st.style.color='#ff4444';st.textContent=d.error||'Model rejected.';}}).catch(e=>{st.style.color='#ff4444';st.textContent='Upload failed: '+e;});}
function clearPointing(){if(!confirm('Erase the stored pointing model? The telescope will point with no calibration applied until a new model is loaded.'))return;fetch('/pointing/clear').then(r=>r.json()).then(d=>{showPointing(d.model);const st=document.getElementById('pointing-status');st.style.color='#00ff00';st.textContent='Calibration cleared.';});}
function formatRA(h){const hh=Math.floor(h);const m=Math.floor((h-hh)*60);const s=Math.floor(((h-hh)*60-m)*60);return hh+'h'+String(m).padStart(2,'0')+'m'+String(s).padStart(2,'0')+'s';}
function formatDec(d){const sign=d>=0?'+':'-';d=Math.abs(d);const dd=Math.floor(d);const m=Math.floor((d-dd)*60);return sign+dd+'\u00b0'+String(m).padStart(2,'0')+"'";}
function parseRA(s){s=s.trim().toLowerCase();let m=s.match(/^(\d+(?:\.\d+)?)\s*h\s*(?:(\d+(?:\.\d+)?)\s*m?\s*)?(?:(\d+(?:\.\d+)?)\s*s?\s*)?$/);if(m)return(parseFloat(m[1])||0)+(parseFloat(m[2])||0)/60+(parseFloat(m[3])||0)/3600;m=s.match(/^(\d+(?:\.\d+)?)[\s:]+(\d+(?:\.\d+)?)(?:[\s:]+(\d+(?:\.\d+)?))?$/);if(m)return(parseFloat(m[1])||0)+(parseFloat(m[2])||0)/60+(parseFloat(m[3])||0)/3600;return parseFloat(s)||0;}
function parseDec(s){s=s.trim().toLowerCase();let sign=1;if(s.startsWith('-')){sign=-1;s=s.substring(1);}else if(s.startsWith('+')){s=s.substring(1);}let m=s.match(/^(\d+(?:\.\d+)?)\s*[d\u00b0]\s*(?:(\d+(?:\.\d+)?)\s*[m'\u2032]?\s*)?(?:(\d+(?:\.\d+)?)\s*[s"\u2033]?\s*)?$/);if(m)return sign*((parseFloat(m[1])||0)+(parseFloat(m[2])||0)/60+(parseFloat(m[3])||0)/3600);m=s.match(/^(\d+(?:\.\d+)?)[\s:]+(\d+(?:\.\d+)?)(?:[\s:]+(\d+(?:\.\d+)?))?$/);if(m)return sign*((parseFloat(m[1])||0)+(parseFloat(m[2])||0)/60+(parseFloat(m[3])||0)/3600);return parseFloat(s)||0;}
function syncBrowserTime(){fetch('/time/set?timestamp='+Math.floor(Date.now()/1000)).then(r=>r.json()).then(d=>{if(d.ok)console.log('Time synced');});}
function checkAndSyncTime(){fetch('/time/status').then(r=>r.json()).then(d=>{if(!d.synced)syncBrowserTime();});}
function updateEphemeris(){fetch('/ephemeris').then(r=>r.json()).then(d=>{lastEphemeris=d;updateTargetInfo();});}
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
function toggleWifiPower(){fetch('/wifi/status').then(r=>r.json()).then(d=>{const enable=!d.wifi_enabled;if(!enable&&!d.eth_connected){alert('Cannot disable WiFi without Ethernet connection');return;}fetch('/wifi/power?enable='+(enable?'1':'0')).then(r=>r.json()).then(r=>{if(!r.ok){alert('Error: '+(r.error||'Unknown'));return;}
// The controller acknowledges at once and actions the radio a moment later on
// its main loop, so re-check a few times rather than reading back the old state.
updateNetworkStatus();[600,1500,3000,6000].forEach(ms=>setTimeout(updateNetworkStatus,ms));});});}
// The Target box (2026-10-02): one form for every kind of target, replacing
// Quick Targets and three coordinate forms. Sky targets are tracked; Alt/Az
// is a park position. Each kind remembers its own two values while switching.
let lastEphemeris=null;
const TARGET_KINDS={sun:{labels:null,btn:'Track'},moon:{labels:null,btn:'Track'},plane:{labels:null,btn:'Track'},
 radec:{labels:['RA','Dec'],btn:'Track',vals:['0','0'],help:'RA as hours (3.55, 3h32m59s or 3:32:59), Dec as degrees (54.58, +54d34m43s or 54:34:43)'},
 gal:{labels:['l','b'],btn:'Track',vals:['0','0'],help:'Galactic longitude and latitude in degrees'},
 altaz:{labels:['Alt','Az'],btn:'Go to',vals:['45','180'],help:'Sky altitude and azimuth in degrees; the pointing model is applied and the dish stays there'}};
let targetKind='sun';
function targetKindChanged(){const old=TARGET_KINDS[targetKind];if(old.vals){old.vals=[document.getElementById('t1').value,document.getElementById('t2').value];}
 targetKind=document.getElementById('target_kind').value;const k=TARGET_KINDS[targetKind];const row=document.getElementById('target_coords');
 if(k.labels){row.style.display='';document.getElementById('t1_label').textContent=k.labels[0];document.getElementById('t2_label').textContent=k.labels[1];
  document.getElementById('t1').value=k.vals[0];document.getElementById('t2').value=k.vals[1];row.setAttribute('data-help',k.help);}else{row.style.display='none';}
 document.getElementById('target_btn').textContent=k.btn;updateTargetInfo();}
function fmtAltAz(o){return 'Alt '+o.alt.toFixed(1)+'\u00b0 Az '+o.az.toFixed(1)+'\u00b0';}
function updateTargetInfo(){const el=document.getElementById('target_info');if(!el)return;const d=lastEphemeris;let t='';
 if(targetKind==='sun'&&d)t='Sun now: '+fmtAltAz(d.sun);
 else if(targetKind==='moon'&&d)t='Moon now: '+fmtAltAz(d.moon);
 else if(targetKind==='plane'&&d)t=(d.plane&&d.plane.found)?'Nearest the galactic centre above the acquisition floor: l '+d.plane.l.toFixed(1)+'\u00b0, '+fmtAltAz(d.plane):'No point on the plane above '+(d.plane&&d.plane.min_alt!==undefined?d.plane.min_alt.toFixed(0):'the floor')+'\u00b0 just now';
 const names={sun:'Sun',moon:'Moon',plane:'Galactic Plane'};
 if(names[targetKind]&&currentTrackingTarget===names[targetKind])t+=(t?' \u2014 ':'')+'already tracking';
 el.textContent=t;}
function targetGo(){const k=targetKind;const v1=document.getElementById('t1').value,v2=document.getElementById('t2').value;
 if(k==='sun'){trackSun();return;}if(k==='moon'){trackMoon();return;}if(k==='plane'){trackGalacticPlane();return;}
 let url;if(k==='radec')url='/track/radec?ra='+parseRA(v1)+'&dec='+parseDec(v2);
 else if(k==='gal')url='/track/galactic?l='+parseFloat(v1)+'&b='+parseFloat(v2);
 else url='/direct?alt='+parseFloat(v1)+'&az='+parseFloat(v2);
 fetch(url).then(r=>r.json()).then(d=>{if(!d.ok){alert(d.error||'The controller refused the target');}else if(k==='gal'&&d.ra!==undefined){document.getElementById('target_info').textContent='RA '+formatRA(d.ra)+' Dec '+formatDec(d.dec);}updateStatus();}).catch(()=>alert('No answer from the controller'));}

function trackSun(){if(currentTrackingTarget==='Sun')return;fetch('/track/sun').then(()=>updateStatus());}
function trackMoon(){if(currentTrackingTarget==='Moon')return;fetch('/track/moon').then(()=>updateStatus());}
function trackGalacticPlane(){if(currentTrackingTarget==='Galactic Plane')return;handleTrackResponse(fetch('/track/galactic-plane'));}
function handleTrackResponse(promise){promise.then(r=>r.json()).then(d=>{if(!d.ok){alert(d.error||'Track failed');}updateStatus();});}
function updateAxisMode(d){currentAxisMode=d.az_only?'az':d.alt_only?'alt':'both';}
function updateTargetButtons(target){currentTrackingTarget=target||'';updateTargetInfo();}
function stopAll(){fetch('/stop/all').then(()=>updateStatus());schedulerFetch('/api/stop_all',{method:'POST'}).catch(()=>console.log('Scheduler stop_all not available'));}
function updateFirmware(){const msg='Build and flash this controller\u2019s (ESP32) firmware from the observatory computer\u2019s copy of the code, over the network. It does not update the Arduino Due, which is flashed over USB. The scheduler refuses while an observation or scan is running, or the mount is tracking, homing or moving, and does nothing if this controller already runs that code. After a flash the controller restarts; allow up to about 100 seconds before refreshing.';if(!confirm(msg))return;schedulerFetch('/api/firmware/update',{method:'POST'}).then(r=>r.json()).then(d=>{if(!d.success){alert(d.error||'Firmware update did not start.');return;}if(d.unchanged){alert(d.message);return;}alert('Firmware update started. Watch the scheduler Log tab for progress. After the upload reports OK, the ESP32 reboots and the controller website may be unavailable for up to about 100 seconds.');}).catch(()=>alert('Scheduler is not responding on '+SCHEDULER_URL+'. Start the scheduler first, then click Update firmware again.'));}
function restartController(){if(!confirm('Restart this controller (the ESP32)? Its firmware and settings are unchanged, and the Arduino Due is not restarted. Refused while tracking, homing or moving. The page will be unavailable for up to about 100 seconds.'))return;fetch('/restart',{method:'POST'}).then(r=>r.json()).then(d=>{if(!d.ok){alert(d.error||'Not restarted.');return;}alert('Restarting. Refresh this page in about a minute.');}).catch(()=>alert('The controller did not answer the restart request.'));}
function resetFault(){fetch('/reset').then(r=>r.json()).then(d=>{if(!d.ok){alert(d.error||'Reset failed');}updateStatus();});}
function goHome(){fetch('/go-home').then(()=>updateStatus());}

function runHoming(){fetch('/home').then(r=>r.json()).then(d=>{if(!d.ok){alert(d.error||'Homing failed');}updateStatus();});}
function setOffset(){const alt=document.getElementById('offset_alt').value;const az=document.getElementById('offset_az').value;fetch('/offset?alt='+alt+'&az='+az).then(r=>r.json()).then(d=>{document.getElementById('current_offset').textContent=d.offset_alt.toFixed(1)+'\u00b0 / '+d.offset_az.toFixed(1)+'\u00b0';});}
function clearOffset(){fetch('/offset/clear').then(r=>r.json()).then(d=>{document.getElementById('offset_alt').value='0';document.getElementById('offset_az').value='0';document.getElementById('current_offset').textContent='0.0\u00b0 / 0.0\u00b0';});}
let isSlewing=false;let refreshInterval=null;
function setFaultLocked(locked){faultLocked=locked;document.querySelectorAll('#tab-control .fault-dependent').forEach(el=>{el.disabled=locked;el.classList.toggle('locked',locked);});}
function scheduleRefresh(){if(refreshInterval)clearInterval(refreshInterval);refreshInterval=setInterval(updateStatus,isSlewing?500:1000);}
// The motor-current bars: |I| as a fraction of the Due's default 5 A overcurrent
// limit (DEFAULT_CURRENT_LIMIT), coloured as it nears it.
const CURRENT_FULL_SCALE_A=5.0;
function setCurrentBar(id,amps){const el=document.getElementById(id);if(!el)return;const a=Math.abs(Number(amps)||0);el.style.width=Math.min(100,100*a/CURRENT_FULL_SCALE_A).toFixed(1)+'%';el.classList.toggle('warm',a>=3&&a<4);el.classList.toggle('hot',a>=4);}
function updateStatus(){fetch('/status').then(r=>r.json()).then(d=>{document.getElementById('alt').textContent=(d.true_alt!==undefined?d.true_alt:d.alt).toFixed(2)+'\u00b0';document.getElementById('az').textContent=(d.true_az!==undefined?d.true_az:d.az).toFixed(2)+'\u00b0';document.getElementById('drive_pos').textContent=d.alt.toFixed(2)+'\u00b0 / '+d.az.toFixed(2)+'\u00b0'+(d.pointing_loaded?'':' (no model)');if(d.ra!==undefined){document.getElementById('cur_ra').textContent=formatRA(d.ra);document.getElementById('cur_dec').textContent=formatDec(d.dec);document.getElementById('cur_gl').textContent=d.gal_l.toFixed(2)+'\u00b0';document.getElementById('cur_gb').textContent=d.gal_b.toFixed(2)+'\u00b0';}document.getElementById('alt_a').textContent=d.alt_current_a.toFixed(2)+' A';document.getElementById('az_a').textContent=d.az_current_a.toFixed(2)+' A';setCurrentBar('alt_a_bar',d.alt_current_a);setCurrentBar('az_a_bar',d.az_current_a);document.getElementById('status').textContent=d.status;document.getElementById('status').className='value '+(d.is_slewing?'tracking':'idle');document.getElementById('error_status').textContent=d.fault_active?(d.fault||'FAULT'):'Clear';document.getElementById('error_status').className='value '+(d.fault_active?'disconnected':'connected');const resetBtn=document.getElementById('reset_btn');resetBtn.disabled=!d.fault_active;resetBtn.className=d.fault_active?'stop':'secondary';setFaultLocked(!!d.fault_active);if(d.is_slewing!==isSlewing){isSlewing=d.is_slewing;scheduleRefresh();}});fetch('/tracking').then(r=>r.json()).then(d=>{updateAxisMode(d);updateTargetButtons(d.enabled?d.target_name:'');if(d.enabled){let info=d.target_name||'RA/Dec';info+=': '+formatRA(d.ra)+' '+formatDec(d.dec);if(d.az_only){info+=' [Az only, Alt '+d.az_only_alt.toFixed(1)+'\u00b0]';}if(d.alt_only){info+=' [Alt only, Az '+d.alt_only_az.toFixed(1)+'\u00b0]';}if(d.waiting_for_rise){info+=' [Below horizon]';document.getElementById('tracking_target').className='value disconnected';}else if(d.waiting_for_wrap){info+=' [Az limits]';document.getElementById('tracking_target').className='value disconnected';}else{document.getElementById('tracking_target').className='value tracking';}if(d.offset_alt!==0||d.offset_az!==0){info+=' [+'+d.offset_alt.toFixed(1)+'/'+d.offset_az.toFixed(1)+']';}document.getElementById('tracking_target').textContent=info;}else{document.getElementById('tracking_target').textContent='Off';document.getElementById('tracking_target').className='value idle';}document.getElementById('current_offset').textContent=d.offset_alt.toFixed(1)+'\u00b0 / '+d.offset_az.toFixed(1)+'\u00b0';});const t0=Date.now();fetch('/time/status').then(r=>r.json()).then(d=>{noteControllerClock(d,t0,Date.now());let ts=d.utc+' UTC';let cls='value connected';if(d.sync_state==='ok'){ts+=' (NTP '+formatAge(d.last_sync_age_s)+' ago)';}else if(d.sync_state==='stale'){ts+=' (STALE - no NTP for '+formatAge(d.last_sync_age_s)+')';cls='value disconnected';}else if(d.sync_state==='unverified'){ts+=' (browser set, NOT NTP verified)';cls='value disconnected';}else{ts='NOT SYNCED';cls='value disconnected';}document.getElementById('time_status').className=cls;clockSuffix=ts.slice(d.utc.length+4);clockSynced=(d.sync_state!==undefined&&ts!=='NOT SYNCED');if(ctrlOffsetMs===null||!clockSynced)document.getElementById('time_status').textContent=ts;});}
// The Time row runs its own clock, ticking on each of the controller's
// seconds (2026-10-02). The controller reports its time to the millisecond
// (unix_ms); each poll measures the offset from this browser's clock, taking
// the middle of the request as the moment it was read, so the display is good
// to the round trip's asymmetry, a few tens of ms, instead of whole seconds a
// poll late.
let ctrlOffsetMs=null,clockSuffix='',clockSynced=false,clockTimer=null;
function noteControllerClock(d,t0,t1){if(d.unix_ms===undefined)return;ctrlOffsetMs=d.unix_ms-(t0+t1)/2;if(!clockTimer)tickControllerClock();}
function tickControllerClock(){const ms=Date.now()+ctrlOffsetMs;const el=document.getElementById('time_status');
 if(el&&clockSynced){const t=new Date(Math.floor(ms/1000)*1000).toISOString();el.textContent=t.slice(0,10)+' '+t.slice(11,19)+' UTC'+clockSuffix;}
 clockTimer=setTimeout(tickControllerClock,1000-(ms%1000)+5);}
function formatAge(s){if(s===undefined||s<0)return'never';if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m';return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m';}
function loadSettings(){fetch('/settings').then(r=>r.json()).then(d=>{document.getElementById('set_lat').value=d.observer_lat;document.getElementById('set_lon').value=d.observer_lon;document.getElementById('set_az_min').value=d.mount_az_min;document.getElementById('set_az_max').value=d.mount_az_max;document.getElementById('set_alt_min').value=d.mount_alt_min;document.getElementById('set_alt_max').value=d.mount_alt_max;document.getElementById('set_horizon_alt').value=d.horizon_alt;document.getElementById('set_galactic_min_alt').value=d.galactic_min_alt;document.getElementById('set_stow_alt').value=d.stow_alt;document.getElementById('set_stow_az').value=d.stow_az;document.getElementById('set_deadband').value=d.position_deadband;document.getElementById('set_ap_ssid').value=d.ap_ssid;document.getElementById('set_ap_pass').value=d.ap_password;document.getElementById('set_page_name').value=d.page_name;const cb=document.getElementById('set_hover_help');if(cb)cb.checked=hoverHelpEnabled;document.getElementById('page-title').textContent=d.page_name;document.title=d.page_name;document.getElementById('settings-status').textContent='';});}
function saveSettings(){const params=new URLSearchParams();params.append('observer_lat',document.getElementById('set_lat').value);params.append('observer_lon',document.getElementById('set_lon').value);params.append('mount_az_min',document.getElementById('set_az_min').value);params.append('mount_az_max',document.getElementById('set_az_max').value);params.append('mount_alt_min',document.getElementById('set_alt_min').value);params.append('mount_alt_max',document.getElementById('set_alt_max').value);params.append('horizon_alt',document.getElementById('set_horizon_alt').value);params.append('galactic_min_alt',document.getElementById('set_galactic_min_alt').value);params.append('stow_alt',document.getElementById('set_stow_alt').value);params.append('stow_az',document.getElementById('set_stow_az').value);params.append('position_deadband',document.getElementById('set_deadband').value);params.append('ap_ssid',document.getElementById('set_ap_ssid').value);const appw=document.getElementById('set_ap_pass').value;if(appw)params.append('ap_password',appw);params.append('page_name',document.getElementById('set_page_name').value);fetch('/settings/save?'+params.toString()).then(r=>r.json()).then(d=>{document.getElementById('settings-status').textContent=d.ok?'Settings saved!':('Save failed'+(d.error?(': '+d.error):''));document.getElementById('settings-status').style.color=d.ok?'#00ff00':'#ff4444';if(d.ok){document.getElementById('page-title').textContent=document.getElementById('set_page_name').value;document.title=document.getElementById('set_page_name').value;document.getElementById('set_ap_pass').value='';}});}
function resetSettings(){if(confirm('Reset all settings to defaults?')){fetch('/settings/reset').then(r=>r.json()).then(d=>{if(d.ok){loadSettings();document.getElementById('settings-status').textContent='Reset to defaults';document.getElementById('settings-status').style.color='#ffaa00';}});}}
function loadPageName(){fetch('/settings').then(r=>r.json()).then(d=>{document.getElementById('page-title').textContent=d.page_name;document.title=d.page_name;});}
let serialExpanded=true;
function toggleSerialPanel(){const log=document.getElementById('serial-log');const tog=document.getElementById('serial-toggle');serialExpanded=!serialExpanded;log.classList.toggle('collapsed',!serialExpanded);tog.textContent=serialExpanded?'\u25BC':'\u25B2';}
function updateSerialLog(){fetch('/serial/log').then(r=>r.json()).then(entries=>{const log=document.getElementById('serial-log');const wasAtBottom=log.scrollHeight-log.scrollTop<=log.clientHeight+5;log.textContent='';entries.forEach(e=>{const div=document.createElement('div');div.className='log-line log-'+e.dir.toLowerCase();const t=document.createElement('span');t.className='log-time';t.textContent=e.time;const d=document.createElement('span');d.className='log-dir';d.textContent='['+e.dir+']';div.appendChild(t);div.appendChild(d);div.appendChild(document.createTextNode(' '+e.msg));log.appendChild(div);});if(wasAtBottom)log.scrollTop=log.scrollHeight;});}
targetKindChanged();fetch('/diag').then(r=>r.json()).then(d=>{if(d.build)document.getElementById('build_info').textContent='firmware built '+d.build+(d.source_hash?', source '+d.source_hash:'');}).catch(()=>{});setInterval(updateEphemeris,10000);setInterval(updateSerialLog,1000);initHoverHelp();scheduleRefresh();updateStatus();updateEphemeris();checkAndSyncTime();loadPageName();updateSerialLog();
</script>
</body>
</html>)rawliteral";

#endif
