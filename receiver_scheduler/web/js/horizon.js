// The Horizon tab: running a scan, and choosing which measured profile is in
// force.
//
// Part of the operator page, split out of one 2144-line file on 2026-08-25.
// Loaded as a classic script: everything here shares one global scope,
// exactly as it did before the split.

        function startHorizon() {
            const params = {
                az_step: parseFloat(document.getElementById('hzAzStep').value) || 5,
                alt_step: parseFloat(document.getElementById('hzAltStep').value) || 5,
                alt_start: parseFloat(document.getElementById('hzAltStart').value) || 5,
                alt_max: parseFloat(document.getElementById('hzAltMax').value) || 60,
                settle_s: parseFloat(document.getElementById('hzSettle').value),
                integration_time_s: parseFloat(document.getElementById('hzIntegration').value) || 2,
                home_every_strips: parseInt(document.getElementById('hzHomeEvery').value, 10),
                sdr_type: document.getElementById('hzSdrType').value,
            };
            fetch('/api/horizon/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(params)
            }).then(r => r.json()).then(d => {
                if (!d.success) {
                    document.getElementById('hzStatus').innerHTML =
                        '<span style="color:#ff4757;">' + (d.error || 'Could not start') + '</span>';
                    return;
                }
                pollHorizon();
            }).catch(e => alert('Horizon scan request failed: ' + e));
        }

        function stopHorizon() {
            fetch('/api/horizon/stop', {method: 'POST'}).then(() => pollHorizon());
        }

        function pollHorizon() {
            fetch('/api/horizon/status').then(r => r.json()).then(d => {
                const status = document.getElementById('hzStatus');
                document.getElementById('hzStartBtn').style.display = d.running ? 'none' : 'inline-block';
                document.getElementById('hzStopBtn').style.display = d.running ? 'inline-block' : 'none';
                if (d.running) {
                    let info = '<span style="color:#00d4ff;">Scanning</span> &mdash; azimuth ' +
                               d.progress + ' of ' + d.total;
                    const p = d.point_info;
                    if (p) {
                        info += '<br><span style="color:#888;">az ' + p.az.toFixed(0) + '&deg;: ' +
                                (p.edge === null ? 'no edge found'
                                 : 'edge ' + p.edge.toFixed(1) + '&deg;, clear above ' +
                                   (p.clear === null ? '?' : p.clear.toFixed(1)) + '&deg;') +
                                ' (' + p.estimator + ')</span>';
                    }
                    status.innerHTML = info;
                } else if (d.error) {
                    status.innerHTML = '<span style="color:#ff4757;">' + d.error + '</span>';
                } else {
                    status.innerHTML = '<span style="color:#888;">Idle.</span>';
                }
                if (d.running) {
                    if (hzPollTimer) clearTimeout(hzPollTimer);
                    hzPollTimer = setTimeout(pollHorizon, 2000);
                } else {
                    if (hzPollTimer) { clearTimeout(hzPollTimer); hzPollTimer = null; }
                    loadHorizonProfile();
                    // A scan that has just finished is in the archive but is not
                    // yet in force, so the list has to refresh for it to be
                    // choosable.
                    loadHorizonProfiles();
                }
            }).catch(() => {});
        }

        // Which archived scans exist, and which one the system believes. Kept
        // separate from the profile display because choosing is a deliberate
        // act: a new scan appears here as soon as it finishes but changes
        // nothing until someone picks it.
        function loadHorizonProfiles() {
            fetch('/api/horizon/profiles').then(r => r.json()).then(d => {
                const sel = document.getElementById('hzProfileSelect');
                const note = document.getElementById('hzArchiveNote');
                if (!sel) return;
                const list = d.profiles || [];
                if (!list.length) {
                    sel.innerHTML = '<option value="">No scans archived yet</option>';
                    note.textContent = '';
                    return;
                }
                const keep = sel.value;
                sel.innerHTML = list.map(p => {
                    const f = p.floors || {};
                    const bits = [p.date];
                    if (p.is_demo) bits.push('SIMULATED');
                    bits.push(p.n_azimuths + ' az at ' + p.az_step_deg + ' deg');
                    // Visible sky rather than the median floor: solid angle is
                    // what an obstruction actually costs, and a tall one costs
                    // far more than its share of the azimuth count.
                    if (f.visible_sq_deg !== null && f.visible_sq_deg !== undefined) {
                        bits.push(f.visible_sq_deg.toLocaleString() + ' deg&sup2; visible' +
                                  ' (' + (100 * f.visible_fraction).toFixed(0) + '%)');
                    }
                    if (!p.complete) bits.push('PARTIAL');
                    if (p.active) bits.push('&larr; in force');
                    return '<option value="' + p.name + '">' + bits.join('  &middot;  ') +
                           '</option>';
                }).join('');
                const active = list.find(p => p.active);
                sel.value = keep || (active ? active.name : list[0].name);
                const chosen = d.chosen || {};
                note.innerHTML = d.active
                    ? ('In force: <span style="color:#2ed573;">' + d.active + '</span>' +
                       (chosen.note ? ' &mdash; ' + chosen.note : ''))
                    : ('<span style="color:#ffa502;">Nothing chosen &mdash; falling back ' +
                       'to the most recent complete scan.</span>');
            }).catch(() => {});
        }

        function useHorizonProfile() {
            const sel = document.getElementById('hzProfileSelect');
            if (!sel || !sel.value) return;
            fetch('/api/horizon/profiles/select', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: sel.value})
            }).then(r => r.json()).then(d => {
                if (!d.success) { alert(d.error || 'Could not select that profile'); return; }
                loadHorizonProfiles();
                loadHorizonProfile();
            });
        }

        function loadHorizonProfile(name) {
            const url = name ? ('/api/horizon/profile?name=' + encodeURIComponent(name))
                             : '/api/horizon/profile';
            fetch(url).then(r => r.json()).then(m => {
                const box = document.getElementById('hzProfile');
                const skyBox = document.getElementById('hzSkyPlotBox');
                if (!m.success) {
                    box.innerHTML = '<span style="color:#888;">' + (m.error || 'No profile') + '</span>';
                    if (skyBox) skyBox.style.display = 'none';
                    return;
                }
                // Always keyed by name: the same URL for two different profiles
                // would be served from the browser cache, and comparing two
                // scans would show the same picture twice.
                if (skyBox && m.name) {
                    document.getElementById('hzSkyPlot').src =
                        '/api/horizon/skyplot?name=' + encodeURIComponent(m.name);
                    skyBox.style.display = '';
                }
                const clears = m.azimuths.map(a => a.clear).filter(v => v !== null);
                const edges = m.azimuths.map(a => a.edge).filter(v => v !== null);
                const envelope = m.azimuths.filter(a => a.estimator === 'envelope').length;
                const highest = m.azimuths.reduce((b, a) =>
                    (a.edge !== null && (b === null || a.edge > b.edge)) ? a : b, null);
                let html = '<table style="width:100%; font-size:13px;">';
                const row = (k, v) => '<tr><td style="color:#888; padding:4px 8px;">' + k +
                                      '</td><td>' + v + '</td></tr>';
                if (m.sdr_type === 'demo') {
                    html += row('<span style="color:#ff4757;">Source</span>',
                        '<span style="color:#ff4757;">Simulated &mdash; this is not the ' +
                        'observatory horizon</span>');
                }
                html += row('Measured', new Date(m.measured_utc).toLocaleString());
                html += row('Azimuths', m.n_azimuths + ' at ' + m.az_step_deg + '&deg; spacing' +
                            (envelope ? ' <span style="color:#ffa502;">(' + envelope +
                             ' by envelope)</span>' : ''));
                html += row('Duration', (m.duration_s / 60).toFixed(0) + ' min');
                if (highest) {
                    html += row('Highest obstruction', highest.edge.toFixed(1) + '&deg; at az ' +
                                highest.az.toFixed(0) + '&deg;');
                }
                if (edges.length) {
                    const med = a => a.slice().sort((x, y) => x - y)[Math.floor(a.length / 2)];
                    html += row('Median edge', med(edges).toFixed(1) + '&deg;');
                    html += row('Median clearance', med(clears).toFixed(1) + '&deg;');
                }
                const refs = m.sky_references || [];
                if (refs.length > 1) {
                    // The sky reference is the run's own health check: it is the
                    // same position every time, so a drift in it is the
                    // instrument, not the sky. A collapse in it is what a
                    // mount that has lost its position looks like from here.
                    const levels = refs.map(r => r.level);
                    const drift = 100 * (Math.max(...levels) - Math.min(...levels))
                                  / Math.min(...levels);
                    html += row('Sky reference drift',
                        '<span style="color:' + (drift > 10 ? '#ff4757' : '#888') + ';">' +
                        drift.toFixed(1) + '% across ' + refs.length + ' checks</span>');
                }
                if (m.complete === false) {
                    html += row('<span style="color:#ffa502;">Incomplete</span>',
                        '<span style="color:#ffa502;">azimuths still blocked at the ceiling</span>');
                }
                const b = {};
                if (b.available) {
                    // Up-cuts against down-cuts: a real horizon has no reason to
                    // zigzag with the parity of the azimuth index, so anything
                    // significant here is backlash in the altitude axis.
                    const sig = b.significance;
                    html += row('Up minus down cuts',
                        '<span style="color:' + (sig > 3 ? '#ff4757' : '#888') + ';">' +
                        (b.up_minus_down_deg >= 0 ? '+' : '') + b.up_minus_down_deg.toFixed(3) +
                        ' &plusmn; ' + b.uncertainty_deg.toFixed(3) + '&deg; (' +
                        sig.toFixed(1) + ' sigma)</span>');
                }
                html += '</table>';
                box.innerHTML = html;
                document.getElementById('hzLandscape').style.display = '';
                document.getElementById('hzPlotContainer').innerHTML =
                    '<img src="/api/horizon/plot?' + Date.now() + '" style="max-width:100%; border-radius:8px; border:1px solid #333;">';
            }).catch(() => {});
        }
