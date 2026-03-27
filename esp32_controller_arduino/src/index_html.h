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
        .box { background: #16213e; padding: 12px; border-radius: 8px; margin-bottom: 10px; }
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
        button.secondary { background: #555; color: #fff; }
        button.secondary:hover { background: #666; }
        button.solar { background: #ffaa00; color: #000; }
        button.solar:hover { background: #cc8800; }
        button.lunar { background: #aaaacc; color: #000; }
        button.lunar:hover { background: #8888aa; }
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
        @media (max-width: 800px) { .two-col { grid-template-columns: 1fr; } }
        .serial-panel { position: fixed; bottom: 0; left: 0; right: 0; background: #0f0f23; border-top: 2px solid #00d9ff; transition: height 0.3s; }
        .serial-header { display: flex; justify-content: space-between; padding: 6px 15px; background: #16213e; cursor: pointer; }
        .serial-header:hover { background: #1a2a4e; }
        .serial-log { height: 150px; overflow-y: auto; font-family: monospace; font-size: 0.85em; padding: 5px 10px; }
        .serial-log.collapsed { height: 0; padding: 0; }
        .log-line { margin: 1px 0; white-space: nowrap; }
        .log-tx { color: #00d9ff; }
        .log-rx { color: #88ff88; }
        .log-time { color: #666; margin-right: 8px; }
        body { padding-bottom: 180px; }
    </style>
</head>
<body>
    <div class="container">
        <h1 id="page-title">SRT Controller</h1>
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
                        <h3>Current Position</h3>
                        <div class="status-row"><span class="label">Altitude:</span><span class="value" id="alt">--</span></div>
                        <div class="status-row"><span class="label">Azimuth:</span><span class="value" id="az">--</span></div>
                        <div class="status-row"><span class="label">Alt Motor:</span><span class="value" id="alt_a">-- A</span></div>
                        <div class="status-row"><span class="label">Az Motor:</span><span class="value" id="az_a">-- A</span></div>
                        <div class="status-row"><span class="label">Status:</span><span class="value" id="status">--</span></div>
                        <div class="status-row"><span class="label">Tracking:</span><span class="value" id="tracking_target">--</span></div>
                        <div class="status-row"><span class="label">Time:</span><span class="value" id="time_status">--</span></div>
                    </div>
                    <div class="box">
                        <h3>Quick Targets</h3>
                        <button class="solar" onclick="trackSun()">Track Sun</button>
                        <button class="lunar" onclick="trackMoon()">Track Moon</button>
                        <button class="stop" onclick="stopTracking()">Stop</button>
                        <div class="target-info">Sun: <span id="sun_pos">--</span><br>Moon: <span id="moon_pos">--</span></div>
                    </div>
                    <div class="box">
                        <h3>Calibrator</h3>
                        <button id="cal_btn" onclick="toggleCalibrator()">CAL OFF</button>
                        <span class="target-info" style="margin-left: 10px;">Noise source for system calibration</span>
                    </div>
                    <div class="box">
                        <h3>Direct Control (Alt/Az)</h3>
                        <div class="coord-row">
                            <label>Alt: <input type="number" id="direct_alt" step="0.5" min="0" max="90" value="45"></label>
                            <label>Az: <input type="number" id="direct_az" step="0.5" min="0" max="355" value="180"></label>
                        </div>
                        <div class="btn-row">
                            <button onclick="goDirect()">Go Direct</button>
                            <button onclick="goHome()">Home</button>
                        </div>
                    </div>
                </div>
                <div class="right-col">
                    <div class="box">
                        <h3>Equatorial (RA/Dec) - J2000</h3>
                        <div class="coord-row">
                            <label>RA: <input type="text" id="ra" value="0" placeholder="12h30m or 12.5"></label>
                            <label>Dec: <input type="text" id="dec" value="0" placeholder="+45d30m or 45.5"></label>
                        </div>
                        <div class="btn-row">
                            <button onclick="goToRaDec()">Go To</button>
                            <button onclick="trackRaDec()">Track</button>
                        </div>
                    </div>
                    <div class="box">
                        <h3>Galactic (l/b) - J2000</h3>
                        <div class="coord-row">
                            <label>l: <input type="number" id="gal_l" step="0.1" min="0" max="360" value="0"></label>
                            <label>b: <input type="number" id="gal_b" step="0.1" min="-90" max="90" value="0"></label>
                        </div>
                        <div class="btn-row">
                            <button onclick="goToGalactic()">Go To</button>
                            <button onclick="trackGalactic()">Track</button>
                        </div>
                        <div class="target-info" id="galactic_radec"></div>
                    </div>
                    <div class="box">
                        <h3>Pointing Offset (Scanning)</h3>
                        <div class="coord-row">
                            <label>dAlt: <input type="number" id="offset_alt" step="0.1" min="-45" max="45" value="0"></label>
                            <label>dAz: <input type="number" id="offset_az" step="0.1" min="-45" max="45" value="0"></label>
                        </div>
                        <div class="btn-row">
                            <button onclick="setOffset()">Apply Offset</button>
                            <button class="secondary" onclick="clearOffset()">Clear</button>
                        </div>
                        <div class="target-info">Current offset: <span id="current_offset">0.0 / 0.0</span></div>
                    </div>
                </div>
            </div>
        </div>
        <div id="tab-network" class="tab-content">
            <div class="two-col">
                <div>
                    <div class="box" id="eth-section">
                        <h3>Ethernet</h3>
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
                                <button onclick="saveEthSettings()">Save Ethernet</button>
                            </div>
                            <p id="eth-save-status" class="target-info"></p>
                        </div>
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
                        <button class="secondary" onclick="forgetWifi()">Forget</button>
                    </div>
                </div>
                <div>
                    <div class="box">
                        <h3>Connect to Network</h3>
                        <div id="wifi-networks" style="max-height: 200px; overflow-y: auto;">
                            <p style="color: #888;">Click Scan to find networks...</p>
                        </div>
                        <div style="margin-top: 8px;"><button onclick="scanWifi()">Scan</button></div>
                        <div id="wifi-connect-form" class="hidden" style="margin-top: 10px; padding-top: 10px; border-top: 1px solid #444;">
                            <div style="margin-bottom: 8px;"><label>Network: <strong id="selected-ssid"></strong></label></div>
                            <div style="margin-bottom: 8px;"><label>Password: <input type="password" id="wifi-password"></label></div>
                            <button onclick="connectWifi()">Connect</button>
                            <button class="secondary" onclick="hideConnectForm()">Cancel</button>
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
                            <button onclick="saveSettings()">Save Settings</button>
                            <button class="secondary" onclick="loadSettings()">Reload</button>
                            <button class="stop" onclick="resetSettings()">Reset to Defaults</button>
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
<script>
let selectedSsid='';
function showTab(name){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));event.target.classList.add('active');document.getElementById('tab-'+name).classList.add('active');if(name==='network')updateNetworkStatus();if(name==='settings')loadSettings();}
function formatRA(h){const hh=Math.floor(h);const m=Math.floor((h-hh)*60);const s=Math.floor(((h-hh)*60-m)*60);return hh+'h'+String(m).padStart(2,'0')+'m'+String(s).padStart(2,'0')+'s';}
function formatDec(d){const sign=d>=0?'+':'-';d=Math.abs(d);const dd=Math.floor(d);const m=Math.floor((d-dd)*60);return sign+dd+'\u00b0'+String(m).padStart(2,'0')+"'";}
function parseRA(s){s=s.trim().toLowerCase();let m=s.match(/^(\d+(?:\.\d+)?)\s*h\s*(?:(\d+(?:\.\d+)?)\s*m?\s*)?(?:(\d+(?:\.\d+)?)\s*s?\s*)?$/);if(m)return(parseFloat(m[1])||0)+(parseFloat(m[2])||0)/60+(parseFloat(m[3])||0)/3600;m=s.match(/^(\d+(?:\.\d+)?)[\s:]+(\d+(?:\.\d+)?)(?:[\s:]+(\d+(?:\.\d+)?))?$/);if(m)return(parseFloat(m[1])||0)+(parseFloat(m[2])||0)/60+(parseFloat(m[3])||0)/3600;return parseFloat(s)||0;}
function parseDec(s){s=s.trim().toLowerCase();let sign=1;if(s.startsWith('-')){sign=-1;s=s.substring(1);}else if(s.startsWith('+')){s=s.substring(1);}let m=s.match(/^(\d+(?:\.\d+)?)\s*[d\u00b0]\s*(?:(\d+(?:\.\d+)?)\s*[m'\u2032]?\s*)?(?:(\d+(?:\.\d+)?)\s*[s"\u2033]?\s*)?$/);if(m)return sign*((parseFloat(m[1])||0)+(parseFloat(m[2])||0)/60+(parseFloat(m[3])||0)/3600);m=s.match(/^(\d+(?:\.\d+)?)[\s:]+(\d+(?:\.\d+)?)(?:[\s:]+(\d+(?:\.\d+)?))?$/);if(m)return sign*((parseFloat(m[1])||0)+(parseFloat(m[2])||0)/60+(parseFloat(m[3])||0)/3600);return parseFloat(s)||0;}
function syncBrowserTime(){fetch('/time/set?timestamp='+Math.floor(Date.now()/1000)).then(r=>r.json()).then(d=>{if(d.ok)console.log('Time synced');});}
function checkAndSyncTime(){fetch('/time/status').then(r=>r.json()).then(d=>{if(!d.synced)syncBrowserTime();});}
function updateEphemeris(){fetch('/ephemeris').then(r=>r.json()).then(d=>{document.getElementById('sun_pos').textContent='Alt '+d.sun.alt.toFixed(1)+'\u00b0 Az '+d.sun.az.toFixed(1)+'\u00b0';document.getElementById('moon_pos').textContent='Alt '+d.moon.alt.toFixed(1)+'\u00b0 Az '+d.moon.az.toFixed(1)+'\u00b0';});}
function toggleEthMode(){const isDhcp=document.getElementById('eth_dhcp').checked;const fields=document.getElementById('eth-static-fields');fields.style.opacity=isDhcp?'0.5':'1';const inputs=fields.querySelectorAll('input');inputs.forEach(i=>i.disabled=isDhcp);}
function saveEthSettings(){const dhcp=document.getElementById('eth_dhcp').checked?'1':'0';const params=new URLSearchParams();params.append('dhcp',dhcp);params.append('ip',document.getElementById('eth_static_ip').value);params.append('gateway',document.getElementById('eth_gateway').value);params.append('subnet',document.getElementById('eth_subnet').value);params.append('dns',document.getElementById('eth_dns').value);fetch('/eth/save?'+params.toString()).then(r=>r.json()).then(d=>{const st=document.getElementById('eth-save-status');if(d.ok){st.textContent='Saved! Reboot to apply.';st.style.color='#ffaa00';}else{st.textContent='Error: '+(d.error||'Unknown');st.style.color='#ff4444';}});}
function updateNetworkStatus(){fetch('/wifi/status').then(r=>r.json()).then(d=>{const ethSec=document.getElementById('eth-section');if(d.eth_available){ethSec.style.display='block';if(d.eth_connected){document.getElementById('eth_status').textContent='Connected';document.getElementById('eth_status').className='value connected';document.getElementById('eth_ip').textContent=d.eth_ip;}else{document.getElementById('eth_status').textContent='Disconnected';document.getElementById('eth_status').className='value disconnected';document.getElementById('eth_ip').textContent='--';}document.getElementById('eth_mac').textContent=d.eth_mac||'--';document.getElementById('eth_dhcp').checked=d.eth_dhcp;document.getElementById('eth_static').checked=!d.eth_dhcp;document.getElementById('eth_static_ip').value=d.eth_static_ip||'';document.getElementById('eth_gateway').value=d.eth_gateway||'';document.getElementById('eth_subnet').value=d.eth_subnet||'';document.getElementById('eth_dns').value=d.eth_dns||'';toggleEthMode();}else{ethSec.style.display='none';}document.getElementById('ap_status').textContent=d.ap_ssid||'--';document.getElementById('ap_ip').textContent=d.ap_ip||'--';document.getElementById('wifi_mac').textContent=d.wifi_mac||'--';if(d.sta_connected){document.getElementById('sta_status').textContent=d.sta_ssid;document.getElementById('sta_status').className='value connected';document.getElementById('sta_ip').textContent=d.sta_ip;}else{document.getElementById('sta_status').textContent='Not connected';document.getElementById('sta_status').className='value disconnected';document.getElementById('sta_ip').textContent='--';}document.getElementById('saved-network').textContent=d.saved_ssid||'None';});}
function scanWifi(){document.getElementById('wifi-networks').innerHTML='<p style="color:#888;">Scanning...</p>';fetch('/wifi/scan').then(r=>r.json()).then(networks=>{if(networks.length===0){document.getElementById('wifi-networks').innerHTML='<p style="color:#888;">No networks found</p>';return;}let html='';networks.forEach(n=>{const sig=n.rssi>-50?'####':n.rssi>-60?'###-':n.rssi>-70?'##--':'#---';const sec=n.secure?'[+]':'';html+='<div class="wifi-network" onclick="selectNetwork(\''+n.ssid+'\')">'+sec+' '+n.ssid+'<span class="wifi-signal">'+sig+'</span></div>';});document.getElementById('wifi-networks').innerHTML=html;});}
function selectNetwork(ssid){selectedSsid=ssid;document.getElementById('selected-ssid').textContent=ssid;document.getElementById('wifi-password').value='';document.getElementById('wifi-connect-form').classList.remove('hidden');}
function hideConnectForm(){document.getElementById('wifi-connect-form').classList.add('hidden');}
function connectWifi(){const pw=document.getElementById('wifi-password').value;fetch('/wifi/connect?ssid='+encodeURIComponent(selectedSsid)+'&password='+encodeURIComponent(pw)).then(r=>r.json()).then(d=>{if(d.ok){alert('Connected! IP: '+d.ip);hideConnectForm();updateNetworkStatus();}else{alert('Failed: '+(d.error||'Unknown'));}});}
function forgetWifi(){if(confirm('Forget saved WiFi?')){fetch('/wifi/forget').then(()=>updateNetworkStatus());}}
function trackSun(){fetch('/track/sun').then(()=>updateStatus());}
function trackMoon(){fetch('/track/moon').then(()=>updateStatus());}
function toggleCalibrator(){const btn=document.getElementById('cal_btn');const isOn=btn.classList.contains('cal-on');fetch('/calibrator?on='+(isOn?'0':'1')).then(r=>r.json()).then(d=>{updateCalButton(d.calibrator);});}
function updateCalButton(on){const btn=document.getElementById('cal_btn');if(on){btn.textContent='CAL ON';btn.classList.add('cal-on');}else{btn.textContent='CAL OFF';btn.classList.remove('cal-on');}}
function goToRaDec(){const ra=parseRA(document.getElementById('ra').value);const dec=parseDec(document.getElementById('dec').value);fetch('/goto?ra='+ra+'&dec='+dec);}
function trackRaDec(){const ra=parseRA(document.getElementById('ra').value);const dec=parseDec(document.getElementById('dec').value);fetch('/track/radec?ra='+ra+'&dec='+dec).then(()=>updateStatus());}
function goToGalactic(){const l=document.getElementById('gal_l').value;const b=document.getElementById('gal_b').value;fetch('/goto/galactic?l='+l+'&b='+b).then(r=>r.json()).then(d=>{document.getElementById('galactic_radec').textContent='RA: '+formatRA(d.ra)+' Dec: '+formatDec(d.dec);});}
function trackGalactic(){const l=document.getElementById('gal_l').value;const b=document.getElementById('gal_b').value;fetch('/track/galactic?l='+l+'&b='+b).then(r=>r.json()).then(d=>{document.getElementById('galactic_radec').textContent='RA: '+formatRA(d.ra)+' Dec: '+formatDec(d.dec);updateStatus();});}
function stopTracking(){fetch('/tracking/enable?enable=0').then(()=>updateStatus());}
function goDirect(){const alt=document.getElementById('direct_alt').value;const az=document.getElementById('direct_az').value;fetch('/direct?alt='+alt+'&az='+az);}
function goHome(){fetch('/direct?alt=0&az=0');}
function setOffset(){const alt=document.getElementById('offset_alt').value;const az=document.getElementById('offset_az').value;fetch('/offset?alt='+alt+'&az='+az).then(r=>r.json()).then(d=>{document.getElementById('current_offset').textContent=d.offset_alt.toFixed(1)+'\u00b0 / '+d.offset_az.toFixed(1)+'\u00b0';});}
function clearOffset(){fetch('/offset/clear').then(r=>r.json()).then(d=>{document.getElementById('offset_alt').value='0';document.getElementById('offset_az').value='0';document.getElementById('current_offset').textContent='0.0\u00b0 / 0.0\u00b0';});}
let isSlewing=false;let refreshInterval=null;
function scheduleRefresh(){if(refreshInterval)clearInterval(refreshInterval);refreshInterval=setInterval(updateStatus,isSlewing?500:1000);}
function updateStatus(){fetch('/status').then(r=>r.json()).then(d=>{document.getElementById('alt').textContent=d.alt.toFixed(2)+'\u00b0';document.getElementById('az').textContent=d.az.toFixed(2)+'\u00b0';document.getElementById('alt_a').textContent=d.alt_current_a.toFixed(2)+' A';document.getElementById('az_a').textContent=d.az_current_a.toFixed(2)+' A';document.getElementById('status').textContent=d.status+(d.fault?' ['+d.fault+']':'');document.getElementById('status').className='value '+(d.is_slewing?'tracking':'idle');if(d.is_slewing!==isSlewing){isSlewing=d.is_slewing;scheduleRefresh();}updateCalButton(d.calibrator);});fetch('/tracking').then(r=>r.json()).then(d=>{if(d.enabled){let info=d.target_name||'RA/Dec';info+=': '+formatRA(d.ra)+' '+formatDec(d.dec);if(d.waiting_for_rise){info+=' [Below horizon]';document.getElementById('tracking_target').className='value disconnected';}else if(d.waiting_for_wrap){info+=' [Az limits]';document.getElementById('tracking_target').className='value disconnected';}else{document.getElementById('tracking_target').className='value tracking';}if(d.offset_alt!==0||d.offset_az!==0){info+=' [+'+d.offset_alt.toFixed(1)+'/'+d.offset_az.toFixed(1)+']';}document.getElementById('tracking_target').textContent=info;}else{document.getElementById('tracking_target').textContent='Off';document.getElementById('tracking_target').className='value idle';}document.getElementById('current_offset').textContent=d.offset_alt.toFixed(1)+'\u00b0 / '+d.offset_az.toFixed(1)+'\u00b0';});fetch('/time/status').then(r=>r.json()).then(d=>{let ts=d.utc+' UTC';if(d.synced){ts+=' ('+d.source+')';document.getElementById('time_status').className='value connected';}else{ts='NOT SYNCED';document.getElementById('time_status').className='value disconnected';}document.getElementById('time_status').textContent=ts;});}
function loadSettings(){fetch('/settings').then(r=>r.json()).then(d=>{document.getElementById('set_lat').value=d.observer_lat;document.getElementById('set_lon').value=d.observer_lon;document.getElementById('set_az_min').value=d.mount_az_min;document.getElementById('set_az_max').value=d.mount_az_max;document.getElementById('set_alt_min').value=d.mount_alt_min;document.getElementById('set_alt_max').value=d.mount_alt_max;document.getElementById('set_home_alt').value=d.home_alt;document.getElementById('set_home_az').value=d.home_az;document.getElementById('set_deadband').value=d.position_deadband;document.getElementById('set_ap_ssid').value=d.ap_ssid;document.getElementById('set_ap_pass').value=d.ap_password;document.getElementById('set_page_name').value=d.page_name;document.getElementById('page-title').textContent=d.page_name;document.title=d.page_name;document.getElementById('settings-status').textContent='';});}
function saveSettings(){const params=new URLSearchParams();params.append('observer_lat',document.getElementById('set_lat').value);params.append('observer_lon',document.getElementById('set_lon').value);params.append('mount_az_min',document.getElementById('set_az_min').value);params.append('mount_az_max',document.getElementById('set_az_max').value);params.append('mount_alt_min',document.getElementById('set_alt_min').value);params.append('mount_alt_max',document.getElementById('set_alt_max').value);params.append('home_alt',document.getElementById('set_home_alt').value);params.append('home_az',document.getElementById('set_home_az').value);params.append('position_deadband',document.getElementById('set_deadband').value);params.append('ap_ssid',document.getElementById('set_ap_ssid').value);params.append('ap_password',document.getElementById('set_ap_pass').value);params.append('page_name',document.getElementById('set_page_name').value);fetch('/settings/save?'+params.toString()).then(r=>r.json()).then(d=>{document.getElementById('settings-status').textContent=d.ok?'Settings saved!':'Save failed';document.getElementById('settings-status').style.color=d.ok?'#00ff00':'#ff4444';if(d.ok){document.getElementById('page-title').textContent=document.getElementById('set_page_name').value;document.title=document.getElementById('set_page_name').value;}});}
function resetSettings(){if(confirm('Reset all settings to defaults?')){fetch('/settings/reset').then(r=>r.json()).then(d=>{if(d.ok){loadSettings();document.getElementById('settings-status').textContent='Reset to defaults';document.getElementById('settings-status').style.color='#ffaa00';}});}}
function loadPageName(){fetch('/settings').then(r=>r.json()).then(d=>{document.getElementById('page-title').textContent=d.page_name;document.title=d.page_name;});}
let serialExpanded=true;
function toggleSerialPanel(){const log=document.getElementById('serial-log');const tog=document.getElementById('serial-toggle');serialExpanded=!serialExpanded;log.classList.toggle('collapsed',!serialExpanded);tog.textContent=serialExpanded?'\u25BC':'\u25B2';}
function updateSerialLog(){fetch('/serial/log').then(r=>r.json()).then(entries=>{const log=document.getElementById('serial-log');const wasAtBottom=log.scrollHeight-log.scrollTop<=log.clientHeight+5;let html='';entries.forEach(e=>{const secs=(e.t/1000).toFixed(1);html+='<div class="log-line log-'+e.dir.toLowerCase()+'"><span class="log-time">'+secs+'s</span><span class="log-dir">['+e.dir+']</span> '+e.msg+'</div>';});log.innerHTML=html;if(wasAtBottom)log.scrollTop=log.scrollHeight;});}
setInterval(updateEphemeris,10000);setInterval(updateSerialLog,1000);scheduleRefresh();updateStatus();updateEphemeris();checkAndSyncTime();loadPageName();updateSerialLog();
</script>
</body>
</html>)rawliteral";

#endif
