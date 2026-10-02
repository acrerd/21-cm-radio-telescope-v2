// The interference survey on the Horizon tab (interference_scan.py): start
// and stop one, and plot any saved one as azimuth against frequency.
//
// A classic script sharing the page's one global scope, like the others.

        let ifPollTimer = null;
        let ifWasRunning = false;

        function startInterference() {
            const num = id => parseFloat(document.getElementById(id).value);
            const params = {
                alt: num('ifAlt'),
                az_start: num('ifAzStart'),
                az_end: num('ifAzEnd'),
                az_step: num('ifAzStep'),
                dwell_s: num('ifDwell'),
                gain_db: num('ifGain'),
                home_first: document.getElementById('ifHomeFirst').checked,
                stow_after: document.getElementById('ifStowAfter').checked,
                sdr_type: document.getElementById('ifSdrType').value,
            };
            fetch('/api/interference/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(params)
            }).then(r => r.json()).then(d => {
                if (!d.success) {
                    document.getElementById('ifStatus').innerHTML =
                        '<span style="color:#ff4757;">' + escapeClock(d.error || 'Could not start') + '</span>';
                    return;
                }
                pollInterference();
            }).catch(e => alert('Interference survey request failed: ' + e));
        }

        function stopInterference() {
            fetch('/api/interference/stop', {method: 'POST'}).then(() => pollInterference());
        }

        function pollInterference() {
            fetch('/api/interference/status').then(r => r.json()).then(d => {
                const status = document.getElementById('ifStatus');
                document.getElementById('ifStartBtn').style.display = d.running ? 'none' : 'inline-block';
                document.getElementById('ifStopBtn').style.display = d.running ? 'inline-block' : 'none';
                if (d.running) {
                    const p = d.point_info || {};
                    let info = '<span style="color:#00d4ff;">Surveying</span> &mdash; ' +
                               (d.total ? d.progress + ' of ' + d.total + ' azimuths done' : 'starting');
                    if (p.az != null) {
                        info += '<br><span style="color:#888;">az ' + p.az.toFixed(0) + '&deg;, alt ' +
                                p.alt.toFixed(0) + '&deg; &mdash; ' + (p.stage === 'measuring'
                                    ? 'tuning ' + p.tuning + ' of ' + p.of : p.stage) +
                                (p.clipped ? ' &mdash; <span style="color:#ff4757;">ADC clipped</span>' : '') +
                                '</span>';
                    }
                    status.innerHTML = info;
                } else if (d.error) {
                    status.innerHTML = '<span style="color:#ff4757;">' + escapeClock(d.error) + '</span>';
                } else {
                    status.innerHTML = '<span style="color:#888;">Idle.</span>';
                }
                if (ifPollTimer) { clearTimeout(ifPollTimer); ifPollTimer = null; }
                if (d.running) ifPollTimer = setTimeout(pollInterference, 3000);
                // A survey that has just ended is a new entry in the list.
                if (ifWasRunning && !d.running) loadInterferenceScans(d.last_name);
                ifWasRunning = !!d.running;
            }).catch(() => {});
        }

        function loadInterferenceScans(select) {
            fetch('/api/interference/scans').then(r => r.json()).then(d => {
                const sel = document.getElementById('ifScanSelect');
                const keep = select || sel.value;
                const scans = (d && d.scans) || [];
                sel.innerHTML = scans.map(s => {
                    const when = (s.started_utc || '').slice(0, 16).replace('T', ' ');
                    const label = when + ' UTC — alt ' + s.alt_deg + '°, ' + s.n_azimuths +
                                  (s.complete ? '' : ' of ' + s.n_planned) + ' azimuths' +
                                  (s.complete ? '' : ' (incomplete)') + (s.sdr_type === 'demo' ? ' — DEMO' : '');
                    return '<option value="' + s.name + '">' + escapeClock(label) + '</option>';
                }).join('');
                if (keep && scans.some(s => s.name === keep)) sel.value = keep;
                showInterferencePlot();
            }).catch(() => {});
        }

        function showInterferencePlot() {
            const box = document.getElementById('ifPlotBox');
            const name = document.getElementById('ifScanSelect').value;
            if (!name) {
                box.innerHTML = '<span style="color:#888;">No interference survey yet.</span>';
                return;
            }
            const url = '/api/interference/plot?name=' + encodeURIComponent(name) +
                        '&mode=' + document.getElementById('ifMode').value +
                        '&stat=' + document.getElementById('ifStat').value;
            box.innerHTML = '<img alt="Interference survey" style="max-width:100%; border-radius:8px;" src="' + url + '">';
        }
