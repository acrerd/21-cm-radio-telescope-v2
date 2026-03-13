# web_server.py - Web interface for SRT control

import socket
import json

from config import WEB_PORT, ETH_ENABLED
from wifi_manager import wifi
from coordinates import get_sun_position, get_moon_position, galactic_to_equatorial
import main as app  # Access global state

# Import ethernet if enabled
if ETH_ENABLED:
    try:
        from ethernet import ethernet
    except ImportError:
        ethernet = None
else:
    ethernet = None


HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
    <title>SRT Controller</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #1a1a2e; color: #eee; }
        .container { max-width: 600px; margin: 0 auto; }
        h1 { color: #00d9ff; }
        h3 { margin-top: 0; color: #aaa; }
        .box { background: #16213e; padding: 15px; border-radius: 8px; margin: 10px 0; }
        .status-row { display: flex; justify-content: space-between; margin: 5px 0; flex-wrap: wrap; }
        .label { color: #888; }
        .value { font-family: monospace; font-size: 1.2em; }
        input[type="number"], input[type="text"], input[type="password"], select {
            padding: 8px; background: #0f0f23; color: #fff; border: 1px solid #444;
            border-radius: 4px; margin: 2px;
        }
        input[type="number"] { width: 80px; }
        input[type="text"], input[type="password"], select { width: 200px; }
        button {
            background: #00d9ff; color: #000; border: none;
            padding: 10px 20px; cursor: pointer; margin: 5px; border-radius: 4px;
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
        .tracking { color: #00ff00; }
        .idle { color: #888; }
        .connected { color: #00ff00; }
        .disconnected { color: #ff8800; }
        .wifi-network { padding: 8px; margin: 4px 0; background: #0f0f23; border-radius: 4px; cursor: pointer; }
        .wifi-network:hover { background: #1a1a3e; }
        .wifi-signal { float: right; color: #888; }
        .hidden { display: none; }
        .tab-bar { display: flex; margin-bottom: 10px; flex-wrap: wrap; }
        .tab { padding: 10px 20px; cursor: pointer; background: #16213e; border-radius: 8px 8px 0 0; margin-right: 2px; }
        .tab.active { background: #00d9ff; color: #000; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .target-info { font-size: 0.9em; color: #888; margin-top: 5px; }
        .coord-section { margin: 10px 0; padding: 10px 0; border-top: 1px solid #333; }
        .coord-section:first-child { border-top: none; padding-top: 0; }
    </style>
</head>
<body>
    <div class="container">
        <h1>SRT Controller</h1>

        <div class="tab-bar">
            <div class="tab active" onclick="showTab('control')">Control</div>
            <div class="tab" onclick="showTab('network')">Network</div>
        </div>

        <div id="tab-control" class="tab-content active">
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
                <button class="stop" onclick="stopTracking()">Stop Tracking</button>
                <div class="target-info">
                    Sun: <span id="sun_pos">--</span> | Moon: <span id="moon_pos">--</span>
                </div>
            </div>

            <div class="box">
                <h3>Target Coordinates</h3>

                <div class="coord-section">
                    <strong>Equatorial (RA/Dec)</strong>
                    <div style="margin-top: 8px;">
                        <label>RA (hours): <input type="number" id="ra" step="0.001" min="0" max="24" value="0"></label>
                        <label>Dec (deg): <input type="number" id="dec" step="0.1" min="-90" max="90" value="0"></label>
                    </div>
                    <div style="margin-top: 10px;">
                        <button onclick="goToRaDec()">Go To</button>
                        <button onclick="trackRaDec()">Track</button>
                    </div>
                </div>

                <div class="coord-section">
                    <strong>Galactic (l/b)</strong>
                    <div style="margin-top: 8px;">
                        <label>l (deg): <input type="number" id="gal_l" step="0.1" min="0" max="360" value="0"></label>
                        <label>b (deg): <input type="number" id="gal_b" step="0.1" min="-90" max="90" value="0"></label>
                    </div>
                    <div style="margin-top: 10px;">
                        <button onclick="goToGalactic()">Go To</button>
                        <button onclick="trackGalactic()">Track</button>
                    </div>
                    <div class="target-info" id="galactic_radec"></div>
                </div>
            </div>

            <div class="box">
                <h3>Direct Control (Alt/Az)</h3>
                <div>
                    <label>Alt (deg): <input type="number" id="direct_alt" step="0.5" min="0" max="90" value="45"></label>
                    <label>Az (deg): <input type="number" id="direct_az" step="0.5" min="0" max="355" value="180"></label>
                </div>
                <div style="margin-top: 10px;">
                    <button onclick="goDirect()">Go Direct</button>
                    <button onclick="goHome()">Home (0,0)</button>
                </div>
            </div>
        </div>

        <div id="tab-network" class="tab-content">
            <div class="box">
                <h3>Ethernet</h3>
                <div class="status-row">
                    <span class="label">Status:</span>
                    <span class="value" id="eth_status">--</span>
                </div>
                <div class="status-row">
                    <span class="label">IP Address:</span>
                    <span class="value" id="eth_ip">--</span>
                </div>
                <div class="status-row">
                    <span class="label">MAC:</span>
                    <span class="value" id="eth_mac">--</span>
                </div>
            </div>

            <div class="box">
                <h3>WiFi</h3>
                <div class="status-row">
                    <span class="label">Access Point:</span>
                    <span class="value" id="ap_status">--</span>
                </div>
                <div class="status-row">
                    <span class="label">AP IP:</span>
                    <span class="value" id="ap_ip">--</span>
                </div>
                <div class="status-row">
                    <span class="label">Station:</span>
                    <span class="value" id="sta_status">--</span>
                </div>
                <div class="status-row">
                    <span class="label">Station IP:</span>
                    <span class="value" id="sta_ip">--</span>
                </div>
            </div>

            <div class="box">
                <h3>Connect to Network</h3>
                <div id="wifi-networks">
                    <p style="color: #888;">Click Scan to find networks...</p>
                </div>
                <div style="margin-top: 10px;">
                    <button onclick="scanWifi()">Scan</button>
                </div>

                <div id="wifi-connect-form" class="hidden" style="margin-top: 15px; padding-top: 15px; border-top: 1px solid #444;">
                    <div style="margin-bottom: 10px;">
                        <label>Network: <strong id="selected-ssid"></strong></label>
                    </div>
                    <div style="margin-bottom: 10px;">
                        <label>Password: <input type="password" id="wifi-password"></label>
                    </div>
                    <button onclick="connectWifi()">Connect</button>
                    <button class="secondary" onclick="hideConnectForm()">Cancel</button>
                </div>
            </div>

            <div class="box">
                <h3>Saved Network</h3>
                <p id="saved-network" style="color: #888;">None</p>
                <button class="secondary" onclick="forgetWifi()">Forget Saved Network</button>
            </div>
        </div>
    </div>

    <script>
        let selectedSsid = '';

        function showTab(name) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            event.target.classList.add('active');
            document.getElementById('tab-' + name).classList.add('active');
            if (name === 'network') updateNetworkStatus();
        }

        function formatRA(hours) {
            const h = Math.floor(hours);
            const m = Math.floor((hours - h) * 60);
            const s = Math.floor(((hours - h) * 60 - m) * 60);
            return h + 'h' + m.toString().padStart(2,'0') + 'm' + s.toString().padStart(2,'0') + 's';
        }

        function formatDec(deg) {
            const sign = deg >= 0 ? '+' : '-';
            deg = Math.abs(deg);
            const d = Math.floor(deg);
            const m = Math.floor((deg - d) * 60);
            return sign + d + '°' + m.toString().padStart(2,'0') + "'";
        }

        function updateStatus() {
            fetch('/status')
                .then(r => r.json())
                .then(d => {
                    document.getElementById('alt').textContent = d.alt.toFixed(2) + '°';
                    document.getElementById('az').textContent = d.az.toFixed(2) + '°';
                    document.getElementById('alt_a').textContent = d.alt_current_a.toFixed(2) + ' A';
                    document.getElementById('az_a').textContent = d.az_current_a.toFixed(2) + ' A';
                    document.getElementById('status').textContent = d.status + (d.fault ? ' [' + d.fault + ']' : '');
                    document.getElementById('status').className = 'value ' + (d.is_slewing ? 'tracking' : 'idle');
                });

            // Get tracking info
            fetch('/tracking')
                .then(r => r.json())
                .then(d => {
                    if (d.enabled) {
                        let info = d.target_name || 'RA/Dec';
                        info += ': ' + formatRA(d.ra) + ' ' + formatDec(d.dec);
                        document.getElementById('tracking_target').textContent = info;
                        document.getElementById('tracking_target').className = 'value tracking';
                    } else {
                        document.getElementById('tracking_target').textContent = 'Off';
                        document.getElementById('tracking_target').className = 'value idle';
                    }
                });

            // Get time status
            fetch('/time/status')
                .then(r => r.json())
                .then(d => {
                    let timeStr = d.utc + ' UTC';
                    if (d.synced) {
                        timeStr += ' (' + d.source + ')';
                        document.getElementById('time_status').className = 'value connected';
                    } else {
                        timeStr = 'NOT SYNCED';
                        document.getElementById('time_status').className = 'value disconnected';
                    }
                    document.getElementById('time_status').textContent = timeStr;
                });
        }

        function syncBrowserTime() {
            // Send browser's current time to ESP32 as Unix timestamp
            const timestamp = Math.floor(Date.now() / 1000);
            fetch('/time/set?timestamp=' + timestamp)
                .then(r => r.json())
                .then(d => {
                    if (d.ok) {
                        console.log('Browser time synced to ESP32');
                    }
                });
        }

        function checkAndSyncTime() {
            // Check if time needs syncing, if so send browser time
            fetch('/time/status')
                .then(r => r.json())
                .then(d => {
                    if (!d.synced) {
                        console.log('ESP32 time not synced, sending browser time...');
                        syncBrowserTime();
                    }
                });
        }

        function updateEphemeris() {
            fetch('/ephemeris')
                .then(r => r.json())
                .then(d => {
                    document.getElementById('sun_pos').textContent =
                        formatRA(d.sun.ra) + ' ' + formatDec(d.sun.dec);
                    document.getElementById('moon_pos').textContent =
                        formatRA(d.moon.ra) + ' ' + formatDec(d.moon.dec);
                });
        }

        function updateNetworkStatus() {
            // Update Ethernet status
            fetch('/eth/status')
                .then(r => r.json())
                .then(d => {
                    if (d.connected) {
                        document.getElementById('eth_status').textContent = 'Connected';
                        document.getElementById('eth_status').className = 'value connected';
                        document.getElementById('eth_ip').textContent = d.ip || '--';
                    } else {
                        document.getElementById('eth_status').textContent = d.enabled ? 'Disconnected' : 'Disabled';
                        document.getElementById('eth_status').className = 'value disconnected';
                        document.getElementById('eth_ip').textContent = '--';
                    }
                    document.getElementById('eth_mac').textContent = d.mac || '--';
                });

            // Update WiFi status
            fetch('/wifi/status')
                .then(r => r.json())
                .then(d => {
                    document.getElementById('ap_status').textContent = d.ap_ssid || '--';
                    document.getElementById('ap_ip').textContent = d.ap_ip || '--';
                    if (d.sta_connected) {
                        document.getElementById('sta_status').textContent = d.sta_ssid;
                        document.getElementById('sta_status').className = 'value connected';
                        document.getElementById('sta_ip').textContent = d.sta_ip;
                    } else {
                        document.getElementById('sta_status').textContent = 'Not connected';
                        document.getElementById('sta_status').className = 'value disconnected';
                        document.getElementById('sta_ip').textContent = '--';
                    }
                    document.getElementById('saved-network').textContent = d.saved_ssid || 'None';
                });
        }

        function scanWifi() {
            document.getElementById('wifi-networks').innerHTML = '<p style="color: #888;">Scanning...</p>';
            fetch('/wifi/scan')
                .then(r => r.json())
                .then(networks => {
                    if (networks.length === 0) {
                        document.getElementById('wifi-networks').innerHTML = '<p style="color: #888;">No networks found</p>';
                        return;
                    }
                    let html = '';
                    networks.forEach(n => {
                        const signal = n.rssi > -50 ? '####' : n.rssi > -60 ? '###-' : n.rssi > -70 ? '##--' : '#---';
                        const secure = n.secure ? '[+]' : '';
                        html += '<div class="wifi-network" onclick="selectNetwork(\\''+n.ssid+'\\')">'+
                                secure + ' ' + n.ssid + '<span class="wifi-signal">' + signal + '</span></div>';
                    });
                    document.getElementById('wifi-networks').innerHTML = html;
                });
        }

        function selectNetwork(ssid) {
            selectedSsid = ssid;
            document.getElementById('selected-ssid').textContent = ssid;
            document.getElementById('wifi-password').value = '';
            document.getElementById('wifi-connect-form').classList.remove('hidden');
        }

        function hideConnectForm() {
            document.getElementById('wifi-connect-form').classList.add('hidden');
        }

        function connectWifi() {
            const password = document.getElementById('wifi-password').value;
            fetch('/wifi/connect?ssid=' + encodeURIComponent(selectedSsid) + '&password=' + encodeURIComponent(password))
                .then(r => r.json())
                .then(d => {
                    if (d.ok) {
                        alert('Connected! New IP: ' + d.ip);
                        hideConnectForm();
                        updateNetworkStatus();
                    } else {
                        alert('Connection failed: ' + (d.error || 'Unknown error'));
                    }
                });
        }

        function forgetWifi() {
            if (confirm('Forget saved WiFi network?')) {
                fetch('/wifi/forget')
                    .then(() => updateNetworkStatus());
            }
        }

        // Tracking functions
        function trackSun() {
            fetch('/track/sun').then(() => updateStatus());
        }

        function trackMoon() {
            fetch('/track/moon').then(() => updateStatus());
        }

        function goToRaDec() {
            const ra = document.getElementById('ra').value;
            const dec = document.getElementById('dec').value;
            fetch('/goto?ra=' + ra + '&dec=' + dec);
        }

        function trackRaDec() {
            const ra = document.getElementById('ra').value;
            const dec = document.getElementById('dec').value;
            fetch('/track/radec?ra=' + ra + '&dec=' + dec).then(() => updateStatus());
        }

        function goToGalactic() {
            const l = document.getElementById('gal_l').value;
            const b = document.getElementById('gal_b').value;
            fetch('/goto/galactic?l=' + l + '&b=' + b)
                .then(r => r.json())
                .then(d => {
                    document.getElementById('galactic_radec').textContent =
                        'RA: ' + formatRA(d.ra) + ' Dec: ' + formatDec(d.dec);
                });
        }

        function trackGalactic() {
            const l = document.getElementById('gal_l').value;
            const b = document.getElementById('gal_b').value;
            fetch('/track/galactic?l=' + l + '&b=' + b)
                .then(r => r.json())
                .then(d => {
                    document.getElementById('galactic_radec').textContent =
                        'RA: ' + formatRA(d.ra) + ' Dec: ' + formatDec(d.dec);
                    updateStatus();
                });
        }

        function stopTracking() {
            fetch('/track?enable=0').then(() => updateStatus());
        }

        function goDirect() {
            const alt = document.getElementById('direct_alt').value;
            const az = document.getElementById('direct_az').value;
            fetch('/direct?alt=' + alt + '&az=' + az);
        }

        function goHome() { fetch('/direct?alt=0&az=0'); }

        // Update periodically
        setInterval(updateStatus, 1000);
        setInterval(updateEphemeris, 10000);
        updateStatus();
        updateEphemeris();

        // Check and sync time on page load
        checkAndSyncTime();
    </script>
</body>
</html>
"""


def url_decode(s):
    """Decode URL-encoded string"""
    result = s.replace('+', ' ')
    i = 0
    decoded = ''
    while i < len(result):
        if result[i] == '%' and i + 2 < len(result):
            try:
                decoded += chr(int(result[i+1:i+3], 16))
                i += 3
                continue
            except ValueError:
                pass
        decoded += result[i]
        i += 1
    return decoded


def parse_query_string(qs):
    """Parse a query string into a dict"""
    params = {}
    if qs:
        for pair in qs.split('&'):
            if '=' in pair:
                k, v = pair.split('=', 1)
                params[k] = url_decode(v)
    return params


def start_web_server(srt):
    """Start the HTTP web server"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('0.0.0.0', WEB_PORT))
    server.listen(5)

    print(f"Web server listening on port {WEB_PORT}")

    while True:
        try:
            client, addr = server.accept()
            handle_http_request(client, srt)
        except Exception as e:
            print(f"Web server error: {e}")


def handle_http_request(client, srt):
    """Handle a single HTTP request"""
    try:
        request = client.recv(1024).decode()
        if not request:
            client.close()
            return

        # Parse request line
        lines = request.split('\r\n')
        if not lines:
            client.close()
            return

        parts = lines[0].split(' ')
        if len(parts) < 2:
            client.close()
            return

        path = parts[1]

        # Split path and query string
        if '?' in path:
            path, qs = path.split('?', 1)
        else:
            qs = ''
        params = parse_query_string(qs)

        # Route requests
        if path in ('/', '/index.html'):
            send_response(client, HTML_PAGE, 'text/html')

        elif path == '/status':
            srt.read_status()
            status = srt.get_status_dict()
            send_response(client, json.dumps(status), 'application/json')

        elif path == '/tracking':
            # Return current tracking state
            result = {
                "enabled": app.tracking_enabled,
                "ra": app.current_ra,
                "dec": app.current_dec,
                "target_name": getattr(app, 'target_name', None)
            }
            send_response(client, json.dumps(result), 'application/json')

        elif path == '/ephemeris':
            # Return Sun and Moon positions
            sun_ra, sun_dec = get_sun_position()
            moon_ra, moon_dec = get_moon_position()
            result = {
                "sun": {"ra": sun_ra, "dec": sun_dec},
                "moon": {"ra": moon_ra, "dec": moon_dec}
            }
            send_response(client, json.dumps(result), 'application/json')

        elif path == '/goto':
            ra = float(params.get('ra', 0))
            dec = float(params.get('dec', 0))
            app.current_ra = ra
            app.current_dec = dec
            app.target_name = None
            app.tracking_enabled = True
            send_response(client, '{"ok":true}', 'application/json')

        elif path == '/goto/galactic':
            l = float(params.get('l', 0))
            b = float(params.get('b', 0))
            ra, dec = galactic_to_equatorial(l, b)
            app.current_ra = ra
            app.current_dec = dec
            app.target_name = f"Gal l={l:.1f} b={b:.1f}"
            app.tracking_enabled = True
            result = {"ok": True, "ra": ra, "dec": dec}
            send_response(client, json.dumps(result), 'application/json')

        elif path == '/track':
            enable = params.get('enable', '0') == '1'
            app.tracking_enabled = enable
            if not enable:
                app.target_name = None
            send_response(client, '{"ok":true}', 'application/json')

        elif path == '/track/sun':
            ra, dec = get_sun_position()
            app.current_ra = ra
            app.current_dec = dec
            app.target_name = "Sun"
            app.tracking_enabled = True
            send_response(client, '{"ok":true}', 'application/json')

        elif path == '/track/moon':
            ra, dec = get_moon_position()
            app.current_ra = ra
            app.current_dec = dec
            app.target_name = "Moon"
            app.tracking_enabled = True
            send_response(client, '{"ok":true}', 'application/json')

        elif path == '/track/radec':
            ra = float(params.get('ra', 0))
            dec = float(params.get('dec', 0))
            app.current_ra = ra
            app.current_dec = dec
            app.target_name = None
            app.tracking_enabled = True
            send_response(client, '{"ok":true}', 'application/json')

        elif path == '/track/galactic':
            l = float(params.get('l', 0))
            b = float(params.get('b', 0))
            ra, dec = galactic_to_equatorial(l, b)
            app.current_ra = ra
            app.current_dec = dec
            app.target_name = f"Gal l={l:.1f} b={b:.1f}"
            app.tracking_enabled = True
            result = {"ok": True, "ra": ra, "dec": dec}
            send_response(client, json.dumps(result), 'application/json')

        # Time endpoints
        elif path == '/time/status':
            status = app.get_time_status()
            send_response(client, json.dumps(status), 'application/json')

        elif path == '/time/set':
            timestamp = int(params.get('timestamp', 0))
            if timestamp > 0:
                ok = app.set_time_from_timestamp(timestamp)
                result = {"ok": ok}
            else:
                result = {"ok": False, "error": "Invalid timestamp"}
            send_response(client, json.dumps(result), 'application/json')

        elif path == '/direct':
            alt = float(params.get('alt', 0))
            az = float(params.get('az', 0))
            app.target_alt = alt
            app.target_az = az
            app.tracking_enabled = False
            app.target_name = None
            srt.send_target(alt, az)
            send_response(client, '{"ok":true}', 'application/json')

        # Ethernet endpoint
        elif path == '/eth/status':
            if ethernet:
                status = ethernet.get_status()
                status['enabled'] = True
            else:
                status = {"enabled": False, "connected": False, "ip": None, "mac": None}
            send_response(client, json.dumps(status), 'application/json')

        # WiFi endpoints
        elif path == '/wifi/status':
            status = wifi.get_status()
            creds = wifi.load_credentials()
            status['saved_ssid'] = creds.get('ssid') if creds else None
            send_response(client, json.dumps(status), 'application/json')

        elif path == '/wifi/scan':
            networks = wifi.scan_networks()
            send_response(client, json.dumps(networks), 'application/json')

        elif path == '/wifi/connect':
            ssid = params.get('ssid', '')
            password = params.get('password', '')
            if wifi.connect_sta(ssid, password):
                wifi.save_credentials(ssid, password)
                result = {"ok": True, "ip": wifi.sta.ifconfig()[0]}
            else:
                result = {"ok": False, "error": "Connection failed"}
            send_response(client, json.dumps(result), 'application/json')

        elif path == '/wifi/forget':
            wifi.clear_credentials()
            send_response(client, '{"ok":true}', 'application/json')

        else:
            send_response(client, 'Not Found', 'text/plain', 404)

    except Exception as e:
        print(f"HTTP error: {e}")
    finally:
        client.close()


def send_response(client, body, content_type, status=200):
    """Send an HTTP response"""
    status_text = 'OK' if status == 200 else 'Not Found'
    response = f"HTTP/1.1 {status} {status_text}\r\n"
    response += f"Content-Type: {content_type}\r\n"
    response += f"Content-Length: {len(body)}\r\n"
    response += "Connection: close\r\n"
    response += "\r\n"
    response += body
    client.send(response.encode())
