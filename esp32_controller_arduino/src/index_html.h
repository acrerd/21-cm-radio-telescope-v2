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
    </style>
</head>
<body>
    <div class="container">
        <h1>SRT Controller</h1>
        <div class="tab-bar">
            <div class="tab active" onclick="showTab('control')">Control</div>
            <div class="tab" onclick="showTab('network')">Network</div>
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
                </div>
            </div>
        </div>
        <div id="tab-network" class="tab-content">
            <div class="two-col">
                <div>
                    <div class="box">
                        <h3>WiFi</h3>
                        <div class="status-row"><span class="label">Access Point:</span><span class="value" id="ap_status">--</span></div>
                        <div class="status-row"><span class="label">AP IP:</span><span class="value" id="ap_ip">--</span></div>
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
                        <h3>Tracking Modes</h3>
                        <p><strong>Go To:</strong> Slew to position once</p>
                        <p><strong>Track:</strong> Continuously follow as Earth rotates</p>
                    </div>
                </div>
                <div>
                    <div class="box">
                        <h3>Stellarium Setup</h3>
                        <ol style="margin: 5px 0; padding-left: 20px; font-size: 0.9em;">
                            <li>Configuration > Plugins > Telescope Control</li>
                            <li>Enable and restart Stellarium</li>
                            <li>Add telescope: Type "External", Host IP, Port 10001</li>
                            <li>Connect, then Ctrl+1 to slew to selected object</li>
                        </ol>
                    </div>
                </div>
            </div>
        </div>
    </div>
<script>
let selectedSsid='';
function showTab(name){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));event.target.classList.add('active');document.getElementById('tab-'+name).classList.add('active');if(name==='network')updateNetworkStatus();}
function formatRA(h){const hh=Math.floor(h);const m=Math.floor((h-hh)*60);const s=Math.floor(((h-hh)*60-m)*60);return hh+'h'+String(m).padStart(2,'0')+'m'+String(s).padStart(2,'0')+'s';}
function formatDec(d){const sign=d>=0?'+':'-';d=Math.abs(d);const dd=Math.floor(d);const m=Math.floor((d-dd)*60);return sign+dd+'\u00b0'+String(m).padStart(2,'0')+"'";}
function parseRA(s){s=s.trim().toLowerCase();let m=s.match(/^(\d+(?:\.\d+)?)\s*h\s*(?:(\d+(?:\.\d+)?)\s*m?\s*)?(?:(\d+(?:\.\d+)?)\s*s?\s*)?$/);if(m)return(parseFloat(m[1])||0)+(parseFloat(m[2])||0)/60+(parseFloat(m[3])||0)/3600;m=s.match(/^(\d+(?:\.\d+)?)[\s:]+(\d+(?:\.\d+)?)(?:[\s:]+(\d+(?:\.\d+)?))?$/);if(m)return(parseFloat(m[1])||0)+(parseFloat(m[2])||0)/60+(parseFloat(m[3])||0)/3600;return parseFloat(s)||0;}
function parseDec(s){s=s.trim().toLowerCase();let sign=1;if(s.startsWith('-')){sign=-1;s=s.substring(1);}else if(s.startsWith('+')){s=s.substring(1);}let m=s.match(/^(\d+(?:\.\d+)?)\s*[d\u00b0]\s*(?:(\d+(?:\.\d+)?)\s*[m'\u2032]?\s*)?(?:(\d+(?:\.\d+)?)\s*[s"\u2033]?\s*)?$/);if(m)return sign*((parseFloat(m[1])||0)+(parseFloat(m[2])||0)/60+(parseFloat(m[3])||0)/3600);m=s.match(/^(\d+(?:\.\d+)?)[\s:]+(\d+(?:\.\d+)?)(?:[\s:]+(\d+(?:\.\d+)?))?$/);if(m)return sign*((parseFloat(m[1])||0)+(parseFloat(m[2])||0)/60+(parseFloat(m[3])||0)/3600);return parseFloat(s)||0;}
function syncBrowserTime(){fetch('/time/set?timestamp='+Math.floor(Date.now()/1000)).then(r=>r.json()).then(d=>{if(d.ok)console.log('Time synced');});}
function checkAndSyncTime(){fetch('/time/status').then(r=>r.json()).then(d=>{if(!d.synced)syncBrowserTime();});}
function updateEphemeris(){fetch('/ephemeris').then(r=>r.json()).then(d=>{document.getElementById('sun_pos').textContent='Alt '+d.sun.alt.toFixed(1)+'\u00b0 Az '+d.sun.az.toFixed(1)+'\u00b0';document.getElementById('moon_pos').textContent='Alt '+d.moon.alt.toFixed(1)+'\u00b0 Az '+d.moon.az.toFixed(1)+'\u00b0';});}
function updateNetworkStatus(){fetch('/wifi/status').then(r=>r.json()).then(d=>{document.getElementById('ap_status').textContent=d.ap_ssid||'--';document.getElementById('ap_ip').textContent=d.ap_ip||'--';if(d.sta_connected){document.getElementById('sta_status').textContent=d.sta_ssid;document.getElementById('sta_status').className='value connected';document.getElementById('sta_ip').textContent=d.sta_ip;}else{document.getElementById('sta_status').textContent='Not connected';document.getElementById('sta_status').className='value disconnected';document.getElementById('sta_ip').textContent='--';}document.getElementById('saved-network').textContent=d.saved_ssid||'None';});}
function scanWifi(){document.getElementById('wifi-networks').innerHTML='<p style="color:#888;">Scanning...</p>';fetch('/wifi/scan').then(r=>r.json()).then(networks=>{if(networks.length===0){document.getElementById('wifi-networks').innerHTML='<p style="color:#888;">No networks found</p>';return;}let html='';networks.forEach(n=>{const sig=n.rssi>-50?'####':n.rssi>-60?'###-':n.rssi>-70?'##--':'#---';const sec=n.secure?'[+]':'';html+='<div class="wifi-network" onclick="selectNetwork(\''+n.ssid+'\')">'+sec+' '+n.ssid+'<span class="wifi-signal">'+sig+'</span></div>';});document.getElementById('wifi-networks').innerHTML=html;});}
function selectNetwork(ssid){selectedSsid=ssid;document.getElementById('selected-ssid').textContent=ssid;document.getElementById('wifi-password').value='';document.getElementById('wifi-connect-form').classList.remove('hidden');}
function hideConnectForm(){document.getElementById('wifi-connect-form').classList.add('hidden');}
function connectWifi(){const pw=document.getElementById('wifi-password').value;fetch('/wifi/connect?ssid='+encodeURIComponent(selectedSsid)+'&password='+encodeURIComponent(pw)).then(r=>r.json()).then(d=>{if(d.ok){alert('Connected! IP: '+d.ip);hideConnectForm();updateNetworkStatus();}else{alert('Failed: '+(d.error||'Unknown'));}});}
function forgetWifi(){if(confirm('Forget saved WiFi?')){fetch('/wifi/forget').then(()=>updateNetworkStatus());}}
function trackSun(){fetch('/track/sun').then(()=>updateStatus());}
function trackMoon(){fetch('/track/moon').then(()=>updateStatus());}
function goToRaDec(){const ra=parseRA(document.getElementById('ra').value);const dec=parseDec(document.getElementById('dec').value);fetch('/goto?ra='+ra+'&dec='+dec);}
function trackRaDec(){const ra=parseRA(document.getElementById('ra').value);const dec=parseDec(document.getElementById('dec').value);fetch('/track/radec?ra='+ra+'&dec='+dec).then(()=>updateStatus());}
function goToGalactic(){const l=document.getElementById('gal_l').value;const b=document.getElementById('gal_b').value;fetch('/goto/galactic?l='+l+'&b='+b).then(r=>r.json()).then(d=>{document.getElementById('galactic_radec').textContent='RA: '+formatRA(d.ra)+' Dec: '+formatDec(d.dec);});}
function trackGalactic(){const l=document.getElementById('gal_l').value;const b=document.getElementById('gal_b').value;fetch('/track/galactic?l='+l+'&b='+b).then(r=>r.json()).then(d=>{document.getElementById('galactic_radec').textContent='RA: '+formatRA(d.ra)+' Dec: '+formatDec(d.dec);updateStatus();});}
function stopTracking(){fetch('/tracking/enable?enable=0').then(()=>updateStatus());}
function goDirect(){const alt=document.getElementById('direct_alt').value;const az=document.getElementById('direct_az').value;fetch('/direct?alt='+alt+'&az='+az);}
function goHome(){fetch('/direct?alt=0&az=0');}
let isSlewing=false;let refreshInterval=null;
function scheduleRefresh(){if(refreshInterval)clearInterval(refreshInterval);refreshInterval=setInterval(updateStatus,isSlewing?500:1000);}
function updateStatus(){fetch('/status').then(r=>r.json()).then(d=>{document.getElementById('alt').textContent=d.alt.toFixed(2)+'\u00b0';document.getElementById('az').textContent=d.az.toFixed(2)+'\u00b0';document.getElementById('alt_a').textContent=d.alt_current_a.toFixed(2)+' A';document.getElementById('az_a').textContent=d.az_current_a.toFixed(2)+' A';document.getElementById('status').textContent=d.status+(d.fault?' ['+d.fault+']':'');document.getElementById('status').className='value '+(d.is_slewing?'tracking':'idle');if(d.is_slewing!==isSlewing){isSlewing=d.is_slewing;scheduleRefresh();}});fetch('/tracking').then(r=>r.json()).then(d=>{if(d.enabled){let info=d.target_name||'RA/Dec';info+=': '+formatRA(d.ra)+' '+formatDec(d.dec);if(d.waiting_for_rise){info+=' [Below horizon]';document.getElementById('tracking_target').className='value disconnected';}else if(d.waiting_for_wrap){info+=' [Az limits]';document.getElementById('tracking_target').className='value disconnected';}else{document.getElementById('tracking_target').className='value tracking';}document.getElementById('tracking_target').textContent=info;}else{document.getElementById('tracking_target').textContent='Off';document.getElementById('tracking_target').className='value idle';}});fetch('/time/status').then(r=>r.json()).then(d=>{let ts=d.utc+' UTC';if(d.synced){ts+=' ('+d.source+')';document.getElementById('time_status').className='value connected';}else{ts='NOT SYNCED';document.getElementById('time_status').className='value disconnected';}document.getElementById('time_status').textContent=ts;});}
setInterval(updateEphemeris,10000);scheduleRefresh();updateStatus();updateEphemeris();checkAndSyncTime();
</script>
</body>
</html>)rawliteral";

#endif
