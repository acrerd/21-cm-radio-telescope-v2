// The Observe tab: hand-off from the simulator, Start Now, and the plot.
//
// Part of the operator page, split out of one 2144-line file on 2026-08-25.
// Loaded as a classic script: everything here shares one global scope,
// exactly as it did before the split.

        function onObserveModeChange() {
            const mode = document.getElementById('obvMode').value;
            const drift = mode === 'drift' || mode === 'solardrift';
            const solar = mode === 'solar' || mode === 'solardrift';
            // In drift mode the duration IS the scan length, and the source
            // transits at its mid-point; saying so beats a tooltip.
            document.getElementById('obvDurationLabel').innerHTML =
                drift ? 'Scan length <span class="unit">(min)</span> &mdash; transit at mid-point'
                      : 'Total integration time <span class="unit">(min)</span>';
            // A solar track has no coordinates to type - the controller
            // follows the Sun from its own ephemeris - so the l/b boxes are
            // replaced by where the Sun actually is. Leaving them on screen
            // greyed out only invites the question of what to put in them.
            ['obvLGroup', 'obvBGroup'].forEach(function (id) {
                const el = document.getElementById(id);
                if (el) el.style.display = solar ? 'none' : '';
            });
            const sun = document.getElementById('obvSunGroup');
            if (sun) sun.style.display = solar ? '' : 'none';
            if (solar) obvRefreshSun();
        }

        // The Sun moves, and a form that says where it was ten minutes ago is
        // worse than one that says nothing. Refreshed when the mode is chosen
        // and every minute the tab stays open on it.
        let obvSunTimer = null;

        function obvRefreshSun() {
            const box = document.getElementById('obvSunWhere');
            if (!box) return;
            fetch('/api/sun/position').then(r => r.json()).then(d => {
                if (!d.success) { box.textContent = d.error || 'unavailable'; return; }
                let text = 'the Sun \u2014 now at altitude ' + d.alt_deg.toFixed(1)
                         + '\u00b0, azimuth ' + d.az_deg.toFixed(1) + '\u00b0';
                if (!d.up) {
                    text += '  \u2014 below the horizon, so there is nothing to track';
                    box.style.color = '#ff4757';
                } else if (d.horizon_warning) {
                    text += '  \u2014 ' + d.horizon_warning;
                    box.style.color = '#ffa502';
                } else {
                    text += '  \u2014 clear of the measured horizon';
                    box.style.color = '#2ed573';
                }
                box.textContent = text;
            }).catch(() => {});
            if (obvSunTimer) clearInterval(obvSunTimer);
            obvSunTimer = setInterval(function () {
                if (document.getElementById('obvMode').value === 'solar') {
                    obvRefreshSun();
                } else {
                    clearInterval(obvSunTimer); obvSunTimer = null;
                }
            }, 60000);
        }

        function loadObserveParams(force) {
            fetch('/api/observe/params').then(r => r.json()).then(d => {
                const info = document.getElementById('obvSource');
                if (!d.available) {
                    if (force) setObserveStatus('Nothing handed over from the Simulator tab.', '#ffa502');
                    return;
                }
                const p = d.params;
                if (!force && p.source_utc === obvAppliedStamp) return;
                obvAppliedStamp = p.source_utc;
                document.getElementById('obvMode').value = p.mode;
                document.getElementById('obvL').value = p.l;
                document.getElementById('obvB').value = p.b;
                document.getElementById('obvDuration').value = p.duration_minutes;
                // Null for a tracked spectrum: there tau is the length of the
                // observation, which has gone into the duration, and how finely
                // the run is chopped into saved spectra is ours to choose. In
                // drift mode tau is the time per sample and does belong here.
                if (p.integration_time_s) {
                    document.getElementById('obvIntegration').value = p.integration_time_s;
                }
                onObserveModeChange();
                const when = new Date(p.source_utc);
                info.innerHTML = 'Copied from the simulator at <strong>' +
                    when.toLocaleTimeString() + ' local</strong> &mdash; ' +
                    (p.mode === 'drift' ? 'drift scan' : 'tracked spectrum') +
                    ' of l=' + p.l.toFixed(2) + '&deg;, b=' + p.b.toFixed(2) + '&deg;. ' +
                    'Edit anything below before starting.';
            }).catch(e => setObserveStatus('Could not read the simulator hand-off: ' + e, '#ff4757'));
        }

        function setObserveStatus(text, colour) {
            const el = document.getElementById('obvStatus');
            el.textContent = text;
            el.style.color = colour || '#888';
        }

        // Build the schedule-entry shape the rest of the app already speaks, so
        // Start Now and Send to Scheduler hand over exactly the same document.
        function observeToObs() {
            const mode = document.getElementById('obvMode').value;
            const solarDrift = mode === 'solardrift';
            const drift = mode === 'drift' || solarDrift;
            const solar = mode === 'solar' || solarDrift;
            const num = (id, dflt) => {
                const v = parseFloat(document.getElementById(id).value);
                return Number.isFinite(v) ? v : dflt;
            };
            const l = num('obvL', 0), b = num('obvB', 0);
            const obs = Object.assign({}, DEFAULTS, {
                name: document.getElementById('obvName').value.trim() || 'Simulator target',
                // A solar drift is a drift entry in the "object" frame: the
                // scheduler parks for where the Sun will be at the mid-point.
                coord_system: solarDrift ? 'drift' : solar ? 'object' : (drift ? 'drift' : 'galactic'),
                object_name: solar ? 'sun' : '',
                drift_frame: solarDrift ? 'object' : 'galactic',
                // Decimal degrees in the degrees field, which dms_to_decimal
                // sums as given; the minutes and seconds boxes are for the
                // schedule form's benefit, not this one's.
                // Zeroed for a solar track. The boxes still hold whatever
                // was last typed, and recording those would put a galactic
                // direction in the file that the dish was never pointed at -
                // which anything reading it later would believe.
                coord1_deg: solar ? 0 : l, coord1_min: 0, coord1_sec: 0,
                coord2_deg: solar ? 0 : b, coord2_min: 0, coord2_sec: 0,
                duration_minutes: Math.round(num('obvDuration', 30)),
                integration_time_s: num('obvIntegration', 3.0),
                sdr_type: document.getElementById('obvSdr').value,
                filename: document.getElementById('obvFilename').value.trim(),
                end_action: document.getElementById('obvEndAction').value,
                respect_local_horizon:
                    document.getElementById('obvRespectHorizon').checked,
                calibrator: false,
                enabled: true,
                // No date or time: for a Run Now start the scheduler reads the
                // drift beam-crossing time as now + half the duration, which is
                // what a drift scan started this moment means.
                start_date: '', start_time: '',
            });
            return obs;
        }

        function observeStartNow() {
            const obs = observeToObs();
            const btn = document.getElementById('obvStartBtn');
            btn.disabled = true;
            setObserveStatus('Pointing and starting the receiver...', '#00d4ff');
            fetch('/api/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(obs)
            }).then(r => r.json()).then(d => {
                if (d.success) {
                    setObserveStatus('Observation started.', '#2ed573');
                    updateStatus();
                    // Restart the live poll rather than waiting for the next
                    // tick. The chain stops itself when a run finishes, so
                    // without this the plot of the *previous* run stayed on
                    // screen until the tab was switched away and back.
                    obvLiveStart();
                } else {
                    setObserveStatus('Failed: ' + (d.error || 'unknown'), '#ff4757');
                }
            }).catch(e => setObserveStatus('Failed: ' + e, '#ff4757'))
              .finally(() => { btn.disabled = false; });
        }

        // The recordings list: every file in data/observations, newest
        // first, described from its own attributes. Kept under the old name
        // because shared.js calls it on every visit to the tab; the session's
        // last-finished run is selected by default, which is what "last
        // observation" used to mean, but any recording can be chosen.
        let obvObservations = [];

        function loadObserveLast() {
            fetch('/api/observations').then(r => r.json()).then(d => {
                const sel = document.getElementById('obvFileSelect');
                const current = sel.value;
                obvObservations = d.observations || [];
                sel.innerHTML = '';
                if (!obvObservations.length) {
                    const o = document.createElement('option');
                    o.value = ''; o.textContent = 'no recordings yet';
                    sel.appendChild(o);
                    onObserveFileChange();
                    return;
                }
                obvObservations.forEach(r => {
                    const o = document.createElement('option');
                    o.value = r.filename;
                    // Date from the file's own creation stamp (UTC) where it has
                    // one, else the file's modification time (local, and says so).
                    const when = r.created ? r.created.slice(0, 16).replace('T', ' ') + ' UTC'
                                           : r.mtime.slice(0, 16).replace('T', ' ') + ' local';
                    let text = when + '  ' + r.filename;
                    if (r.name) text += '  \u2014 ' + r.name;
                    if (r.recording) text += r.locked ? '  (recording, not readable yet)' : '  (recording \u2014 live)';
                    if (r.comment) text += '  \u2014 ' + r.comment;
                    o.textContent = text;
                    // Readable while recording (SWMR) unless written by a
                    // receiver from before that was possible.
                    o.disabled = !!r.locked;
                    sel.appendChild(o);
                });
                // Keep the operator's choice across refreshes; otherwise the
                // run that most recently finished, else the newest file.
                const want = obvObservations.some(r => r.filename === current) ? current
                           : (d.last && obvObservations.some(r => r.filename === d.last)) ? d.last
                           : obvObservations.find(r => !r.locked)?.filename || '';
                sel.value = want;
                const liveRow = obvObservations.find(r => r.recording && !r.locked);
                document.getElementById('obvLiveViewBtn').style.display = liveRow ? '' : 'none';
                onObserveFileChange();
            }).catch(() => {});
        }

        function downloadObserveFile() {
            const file = obvSelectedFile();
            if (!file) return;
            // A navigation, not a fetch: the browser saves the attachment.
            window.location.href = '/api/observe/download?file=' + encodeURIComponent(file);
        }

        // View live recording: select the file the receiver is writing and
        // plot it as it stands, then keep re-plotting while it records. The
        // chain stops when the selection changes or the run finishes.
        let obvLiveViewTimer = null;

        function viewLiveRecording() {
            const liveRow = obvObservations.find(r => r.recording && !r.locked);
            if (!liveRow) return;
            document.getElementById('obvFileSelect').value = liveRow.filename;
            onObserveFileChange();
            showObservePlot();
            if (obvLiveViewTimer) clearTimeout(obvLiveViewTimer);
            const tick = () => {
                obvLiveViewTimer = null;
                fetch('/api/observations').then(r => r.json()).then(d => {
                    const still = (d.observations || []).find(r => r.filename === liveRow.filename);
                    if (!still || !still.recording || obvSelectedFile() !== liveRow.filename) {
                        loadObserveLast();          // the run ended: relist, keep the selection
                        return;
                    }
                    showObservePlot();
                    obvLiveViewTimer = setTimeout(tick, 30000);
                }).catch(() => {});
            };
            obvLiveViewTimer = setTimeout(tick, 30000);
        }

        function obvSelectedFile() {
            return document.getElementById('obvFileSelect').value || '';
        }

        function onObserveFileChange() {
            const el = document.getElementById('obvLastInfo');
            const r = obvObservations.find(x => x.filename === obvSelectedFile());
            if (!r) { el.textContent = 'Nothing recorded yet.'; return; }
            const kind = r.mode === 'drift' ? 'Drift scan' : r.mode === 'manual' ? 'Console recording' : 'Spectrum';
            const size = r.size_bytes ? (r.size_bytes / 1e6).toFixed(1) + ' MB' : '';
            const units = r.units === 'K' ? 'calibrated (kelvin)' : r.units === 'counts' ? 'uncalibrated (counts)' : '';
            el.textContent = [kind + (r.coord_system ? ' \u00b7 ' + r.coord_system : ''),
                              size, units,
                              r.comment ? '\u201c' + r.comment + '\u201d' : ''].filter(Boolean).join(' \u00b7 ');
            // A fit belongs to one file; changing the file retires its proposal.
            document.getElementById('obvFitApplyBtn').style.display = 'none';
            document.getElementById('obvFitInfo').textContent = '';
            loadObserveDetails();
        }

        // The recording's facts, beside the plot.
        function loadObserveDetails() {
            const t = document.getElementById('obvDetails');
            const file = obvSelectedFile();
            if (!file) { t.innerHTML = ''; return; }
            fetch('/api/observe/info?file=' + encodeURIComponent(file)).then(r => r.json()).then(d => {
                if (!d.success) { t.innerHTML = '<tr><td style="color:#ffa502;">' + escapeHtml(d.error || '') + '</td></tr>'; return; }
                const x = d.details;
                const f = (v, n) => v == null ? '\u2014' : Number(v).toFixed(n);
                const rows = [];
                const row = (k, v) => rows.push('<tr><td style="color:#778; padding:2px 8px 2px 0; white-space:nowrap; vertical-align:top;">'
                                              + k + '</td><td style="padding:2px 0;">' + v + '</td></tr>');
                row('file', escapeHtml(x.filename));
                if (x.name) row('target', escapeHtml(x.name));
                row('mode', escapeHtml(x.mode) + (x.coord_system ? ' \u00b7 ' + escapeHtml(x.coord_system) : ''));
                if (x.mode === 'drift' && x.drift_alt != null) {
                    row('parked at', 'alt ' + f(x.drift_alt, 2) + '\u00b0, az ' + f(x.drift_az, 2) + '\u00b0'
                        + (x.drift_drive_alt != null ? ' (drive ' + f(x.drift_drive_alt, 1) + ' / ' + f(x.drift_drive_az, 1) + ')' : ''));
                }
                if (x.mode === 'drift' && x.drift_crossing_time) {
                    // The moment the source crosses the parked beam, computed at
                    // the start from where the mount actually parked - not the
                    // slot's mid-point - and how far it misses beam centre.
                    row('beam crossing', escapeHtml(x.drift_crossing_time) + ' local'
                        + (x.drift_crossing_offset_deg != null ? ', ' + f(x.drift_crossing_offset_deg, 3) + '\u00b0 off centre' : ''));
                }
                if (x.coord1_deg != null && x.coord_system !== 'object') {
                    row('coords', f(x.coord1_deg, 3) + ', ' + f(x.coord2_deg, 3)
                        + (x.drift_frame ? ' (' + escapeHtml(x.drift_frame) + ')' : ''));
                }
                if (x.created) row('started', escapeHtml(x.created.slice(0, 19).replace('T', ' ')) + ' UTC');
                row('records', x.records + ' \u00d7 ' + f(x.integration_s, 0) + ' s, ' + x.channels + ' ch'
                    + (x.wide_channels ? ' H I + ' + x.wide_channels + ' ch continuum' : ''));
                if (x.h1_band_mhz) {
                    // A fixed-instrument recording: both products, their bands.
                    row('H I band', f(x.h1_band_mhz[0], 3) + ' \u2013 ' + f(x.h1_band_mhz[1], 3) + ' MHz');
                    row('continuum', f(x.continuum_band_mhz[0], 3) + ' \u2013 ' + f(x.continuum_band_mhz[1], 3) + ' MHz'
                        + (x.wide_units === 'K' ? ', kelvin' : ', counts'));
                }
                if (x.overflows_total != null) {
                    row('samples lost', x.overflows_total
                        ? '<span style="color:#ff4757;">' + x.overflows_total + ' overflows \u2014 records affected are averaged over fewer samples</span>'
                        : '<span style="color:#2ed573;">none (no overflows)</span>');
                }
                row('sky centre', f(x.sky_center_mhz, 3) + ' MHz');
                row('LO', f(x.lo_mhz, 3) + ' MHz');
                row('sample rate', f(x.sample_rate_mhz, 2) + ' Msps'
                    + (x.channel_khz != null ? ' (' + f(x.channel_khz, 2) + ' kHz/ch)' : ''));
                if (x.band_mhz) row('band', f(x.band_mhz[0], 2) + ' \u2013 ' + f(x.band_mhz[1], 2) + ' MHz');
                if (x.fit_window_mhz) row('continuum window', f(x.fit_window_mhz[0], 2) + ' \u2013 ' + f(x.fit_window_mhz[1], 2) + ' MHz');
                if (x.velocity_frame) row('velocity frame', escapeHtml(x.velocity_frame.replace(/^velocity axis: /, '')));
                // For a continuum recording (one with a continuum window) the H I
                // band is cut from both the plot and the fit; say so plainly
                // rather than "outside fit window", and flag the contaminating
                // case where the line falls inside the window. A recording with
                // no continuum window gets no such clause.
                row('H I line', f(x.h1_line_mhz, 3) + ' MHz: '
                    + (x.h1_in_band ? '<span style="color:#2ed573;">in band</span>' : '<span style="color:#ff4757;">not in band</span>')
                    + (x.fit_window_mhz
                        ? (x.h1_in_fit_window
                            ? '<span style="color:#ff4757;">, inside the continuum window \u2014 contaminates the continuum plot and fit</span>'
                            : ', excluded from the continuum plot and fit')
                        : '')
                    + (x.h1_offset_from_lo_mhz != null ? ', ' + (x.h1_offset_from_lo_mhz >= 0 ? '+' : '') + f(x.h1_offset_from_lo_mhz, 2) + ' MHz from LO' : ''));
                row('gain', f(x.gain_db, 0) + ' dB' + (x.sdr_type ? ' \u00b7 ' + escapeHtml(x.sdr_type) : ''));
                row('units', x.units === 'K'
                    ? 'kelvin (gain ' + Number(x.applied_gain_counts_per_k).toExponential(3) + ' counts/K, T_sys ' + f(x.applied_t_sys_k, 1) + ' K applied)'
                    : 'counts (uncalibrated)');
                if (x.comment) row('comment', escapeHtml(x.comment));
                t.innerHTML = rows.join('');
            }).catch(() => { t.innerHTML = ''; });
        }

        function showObservePlot() {
            const host = document.getElementById('obvPlot');
            host.innerHTML = '<span style="color:#888; font-size:12px;">Drawing&hellip;</span>';
            // Fetched rather than dropped straight into an img src: a refusal
            // comes back as JSON with a reason - still recording, no spectra -
            // and a broken image icon would throw that away.
            fetch('/api/observe/plot?file=' + encodeURIComponent(obvSelectedFile()) + '&' + Date.now()).then(r => {
                if (!r.ok) return r.json().then(d => { throw new Error(d.error || ('HTTP ' + r.status)); });
                return r.blob();
            }).then(b => {
                const url = URL.createObjectURL(b);
                host.innerHTML = '<img src="' + url + '" style="width:100%; height:auto; '
                               + 'border-radius:8px; border:1px solid #333;">';
            }).catch(e => {
                host.innerHTML = '<span style="color:#ffa502; font-size:12px;">' + e.message + '</span>';
            });
        }

        // ---- Fit model: the simulator's sky against the last recording ----
        // The RF tab's gain fit, applied to whatever was just observed, so any
        // tracked spectrum of the plane checks the calibration in force. The
        // result is a proposal until "Apply as calibration" is pressed.
        function fitObserveModel() {
            const info = document.getElementById('obvFitInfo');
            const host = document.getElementById('obvPlot');
            const apply = document.getElementById('obvFitApplyBtn');
            apply.style.display = 'none';
            info.textContent = 'Fitting the simulator to the recording\u2026';
            fetch('/api/observe/fit', {method: 'POST', headers: {'Content-Type': 'application/json'},
                   body: JSON.stringify({file: obvSelectedFile()})}).then(r => r.json()).then(d => {
                if (!d.success) { info.textContent = 'Fit: ' + (d.error || 'failed'); return; }
                const f = d.fit;
                obvFitApproximate = f.approximate || '';
                let text = (f.approximate ? 'APPROXIMATE \u2014 ' + f.approximate + '\n' : '')
                         + 'fit of ' + f.source_file
                         + (f.records_used ? ' (' + f.records_used + ' records)' : '') + ' at l=' + f.glon.toFixed(2)
                         + ' b=' + f.glat.toFixed(2) + ': gain ' + f.gain_counts_per_k.toExponential(3)
                         + ' counts/K \u00b7 T_sys ' + f.t_sys_k.toFixed(1) + ' K \u00b7 correlation '
                         + (f.correlation != null ? f.correlation.toFixed(3) : '?')
                         + ' \u00b7 residual ' + (f.residual_rms_k != null ? f.residual_rms_k.toFixed(2) + ' K' : '?');
                if (f.velocity_shift_km_s != null) {
                    text += ' \u00b7 clock shift ' + f.velocity_shift_km_s.toFixed(2) + ' km/s'
                          + (d.trustworthy_shift ? '' : ' (line too weak to trust)');
                }
                if (d.compare) {
                    const pct = 100 * (d.compare.gain_ratio - 1);
                    text += '\nagainst the calibration in force (' + (d.compare.in_force_observed_utc || '?')
                          + '): gain ' + (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%, T_sys '
                          + (d.compare.t_sys_delta_k >= 0 ? '+' : '') + d.compare.t_sys_delta_k.toFixed(1) + ' K';
                }
                if (f.applicable === false) {
                    text += '\n(reported and drawn only: a total-power gain is not the '
                          + 'per-channel calibration, so there is nothing to apply)';
                }
                info.style.whiteSpace = 'pre-wrap';
                info.textContent = text;
                apply.style.display = f.applicable === false ? 'none' : '';
                host.innerHTML = '<span style="color:#888; font-size:12px;">Drawing&hellip;</span>';
                fetch('/api/observe/fit/plot?' + Date.now()).then(r => {
                    if (!r.ok) return r.json().then(x => { throw new Error(x.error || ('HTTP ' + r.status)); });
                    return r.blob();
                }).then(b => {
                    host.innerHTML = '<img src="' + URL.createObjectURL(b) + '" style="width:100%; height:auto; '
                                   + 'border-radius:8px; border:1px solid #333;">';
                }).catch(e => { host.innerHTML = '<span style="color:#ffa502; font-size:12px;">' + e.message + '</span>'; });
            }).catch(e => { info.textContent = 'Fit failed: ' + e; });
        }

        let obvFitApproximate = '';

        function applyObserveFit() {
            if (!confirm((obvFitApproximate ? 'This fit is APPROXIMATE (' + obvFitApproximate + ').\n\n' : '')
                         + 'Replace the gain calibration in force with this fit?')) return;
            fetch('/api/observe/fit/apply', {method: 'POST'}).then(r => r.json()).then(d => {
                const info = document.getElementById('obvFitInfo');
                info.textContent = d.success ? 'Applied: this fit is now the calibration in force.'
                                             : 'Apply failed: ' + (d.error || '');
                document.getElementById('obvFitApplyBtn').style.display = 'none';
            });
        }

        function observeToScheduler() {
            // Reuse the Add Observation form rather than duplicating date, time
            // and clash handling here: it is the one place that knows how a
            // drift slot is laid out around its transit.
            document.getElementById('modalTitle').textContent = 'Add Observation';
            document.getElementById('obsIndex').value = -1;
            fillForm(observeToObs());
            document.getElementById('obsModal').classList.add('active');
        }

        // The tuning is the fixed instrument's (issue #27); the tab shows it
        // and offers nothing to change. The old per-observation tuning
        // preview (/api/tuning) went with the boxes it described.
        function refreshObserveTuning() {
            showInstrument('obvTuning');
        }

        // ---- Solar flux, live -------------------------------------------
        //
        // Polled from /api/observe/live, which reads the one-line-per-record
        // summary the receiver writes beside its HDF5. The recording itself
        // cannot be opened while it is being written, and a live *spectrum*
        // display is a bigger problem than this one (issue #15) - a flux
        // monitor needs a single number per record, not the spectrum.
        //
        // Drawn on a canvas rather than fetched as a PNG: the point of a live
        // plot is that it updates, and re-rendering matplotlib every ten
        // seconds for a line graph would put the work on the observatory
        // machine while it is recording.
        // Matches the canvas background in app.css. Both are needed: the CSS
        // one covers the element before the first draw, this one is in the
        // pixels so a copy of the image looks like the image.
        const PLOT_BG = '#16162e';

        let obvLiveTimer = null;

        // How long to wait before asking again. Follows the record cadence
        // rather than being fixed: a tenth-second integration is chosen to
        // catch bursts, and a ten-second refresh would show them ten seconds
        // late, which defeats the reason for choosing it. At the other end a
        // minute-long integration made five polls in six find nothing at all.
        //
        // Clamped either way. Two seconds is as fast as a line on a screen is
        // worth redrawing, and thirty keeps a slow run feeling alive.
        function obvLiveInterval(d) {
            const last = d && d.points && d.points.length
                       ? d.points[d.points.length - 1] : null;
            if (!last || !last.tau) return 10000;
            const tau = last.n ? last.tau / last.n : last.tau;   // per record
            return Math.max(2000, Math.min(30000, tau * 1000));
        }

        function obvLiveSchedule(d) {
            if (obvLiveTimer) clearTimeout(obvLiveTimer);
            // A finished run has nothing more to add, so stop asking.
            if (d && d.finished) { obvLiveTimer = null; return; }
            obvLiveTimer = setTimeout(obvLivePoll, obvLiveInterval(d));
        }

        // Whether the last poll saw a run in progress, so the moment it ends
        // can be told from a tab opened after the fact.
        let obvLiveWasLive = false;

        function obvLivePoll() {
            const box = document.getElementById('obvLiveBox');
            fetch('/api/observe/live?limit=2000').then(r => r.json()).then(d => {
                obvLiveSchedule(d);
                // kind is 'solar', 'drift' or null. A tracked spectrum gets no
                // live plot: its band power is meant to be flat, so the trace
                // would be an autoscaled picture of the noise.
                if (!d.success || !d.kind) { box.style.display = 'none'; obvLiveWasLive = false; return; }
                // A finished run comes down: the recording itself can be
                // plotted now (SWMR made that possible), and it shows the same
                // data properly - the file's own axes, the recorded crossing,
                // the fit. If this poll is the one that saw it end, switch the
                // viewer to that recording and draw it, so the trace is
                // replaced rather than removed.
                if (d.finished) {
                    box.style.display = 'none';
                    if (obvLiveWasLive) {
                        obvLiveWasLive = false;
                        fetch('/api/observations').then(r => r.json()).then(o => {
                            const sel = document.getElementById('obvFileSelect');
                            if (o.last && o.observations && o.observations.some(x => x.filename === o.last)) {
                                // Select first: loadObserveLast keeps whatever
                                // is selected across its relist.
                                sel.value = o.last;
                                loadObserveLast();
                                showObservePlot();
                            }
                        }).catch(() => {});
                    }
                    return;
                }
                obvLiveWasLive = true;
                box.style.display = '';
                const drift = d.kind === 'drift';
                document.getElementById('obvLiveTitle').textContent =
                    drift ? 'Drift scan, live' : 'Solar flux, live';
                document.getElementById('obvLiveBlurbSolar').style.display =
                    drift ? 'none' : '';
                document.getElementById('obvLiveBlurbDrift').style.display =
                    drift ? '' : 'none';
                if (!d.points || !d.points.length) {
                    // A drift scan still gets its axes: the window is known
                    // from the start, and an empty box with the crossing time
                    // marked says "waiting" better than a line of text does.
                    if (drift && d.t_start && d.t_end) obvLiveDraw(d);
                    document.getElementById('obvLiveInfo').textContent =
                        d.note || 'waiting for the first record';
                    return;
                }
                obvLiveDraw(d);
            }).catch(() => { obvLiveSchedule(null); });
        }

        function obvLiveDraw(d) {
            const c = document.getElementById('obvLiveCanvas');
            const ctx = c.getContext('2d');
            // Drawn in the canvas's own pixel space (1920 x 520) and scaled
            // to the window by CSS, so these are chosen for the plot's
            // proportions rather than for any particular screen.
            const W = c.width, H = c.height;
            const L = 130, R = 30, T = 30, B = 78;
            // Painted into the canvas, not left to CSS. The stylesheet's
            // background colours the element on the page but never reaches the
            // bitmap, so a copied or saved image carried an alpha channel and
            // whatever pasted it supplied white - dark text and a dark trace on
            // white, which is illegible and looks like a broken export rather
            // than a transparency. What is copied now matches what is shown.
            ctx.fillStyle = PLOT_BG;
            ctx.fillRect(0, 0, W, H);

            const cal = d.calibrated;
            const drift = d.kind === 'drift';
            const pts = d.points || [];
            // A drift scan is plotted in antenna temperature, not flux: SFU is
            // a solar convention, and the sources a drift scan crosses here are
            // quoted as brightness temperature.
            const ys = pts.map(p => cal ? (drift ? p.t_a_k : p.sfu) : p.counts);
            // Absolute UTC seconds. Elapsed minutes were easier to draw but
            // meant nothing once the plot was one of several: solar activity is
            // reported against the clock, and a feature here has to be matched
            // to a flare time, to the published index, or to another
            // instrument's record.
            const xs = pts.map(p => p.t);
            let ymin = ys.length ? Math.min.apply(null, ys) : 0;
            let ymax = ys.length ? Math.max.apply(null, ys) : 1;
            if (!(ymax > ymin)) { ymax = ymin + 1; }
            const pad = 0.08 * (ymax - ymin);
            ymin -= pad; ymax += pad;
            // The time axis. A drift scan gets the observation's own window,
            // fixed from the moment it starts, because the whole point is to
            // see where in the scan the source turns up and whether it lands on
            // the crossing time the pointing was laid out for. Scaling to the
            // data instead would rescale under the reader every few seconds and
            // could never show how far through the scan is.
            let tStart, tEnd;
            if (drift && d.t_start && d.t_end && d.t_end > d.t_start) {
                tStart = d.t_start; tEnd = d.t_end;
            } else {
                tStart = xs.length ? xs[0] : (d.t_start || 0);
                tEnd = Math.max(xs.length ? xs[xs.length - 1] : tStart + 1, tStart + 1);
            }
            const X = v => L + (W - L - R) * ((v - tStart) / (tEnd - tStart));
            // toISOString is UTC whatever the browser's timezone, which is the
            // point: the observatory is worked from more than one place and a
            // local clock would label the same run differently.
            const span = tEnd - tStart;
            const utc = (t, withSeconds) => new Date(t * 1000).toISOString()
                            .slice(11, withSeconds ? 19 : 16);
            const Y = v => T + (H - T - B) * (1 - (v - ymin) / (ymax - ymin));

            ctx.strokeStyle = '#333355'; ctx.fillStyle = '#c8c8d8';
            ctx.font = '20px sans-serif'; ctx.lineWidth = 1;
            for (let i = 0; i <= 4; i++) {
                const v = ymin + (ymax - ymin) * i / 4;
                const y = Y(v);
                ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(W - R, y); ctx.stroke();
                ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
                ctx.fillText(v.toFixed(cal ? (drift ? 2 : 1) : 5), L - 8, y);
            }
            ctx.textAlign = 'center'; ctx.textBaseline = 'top';
            // Ticks on round clock times - whole minutes, five minutes, the
            // hour - at multiples of a step chosen to give five to eight of
            // them, rather than at fifths of a window that starts at 14:00:09.
            // Epoch seconds are UTC-aligned, so a multiple of the step is a
            // round UTC time.
            const STEPS = [10, 15, 30, 60, 120, 300, 600, 900, 1200, 1800, 3600, 7200, 10800, 21600];
            const step = STEPS.find(s => span / s <= 8) || STEPS[STEPS.length - 1];
            const fine = step < 60;              // show seconds only when the step needs them
            for (let v = Math.ceil(tStart / step) * step; v <= tEnd; v += step) {
                const x = X(v);
                ctx.beginPath(); ctx.moveTo(x, H - B); ctx.lineTo(x, H - B + 6); ctx.stroke();
                ctx.fillText(utc(v, fine), x, H - B + 10);
            }
            ctx.font = '22px sans-serif';
            ctx.fillText('UTC on ' + new Date(tStart * 1000).toISOString().slice(0, 10),
                         (L + W - R) / 2, H - 34);
            ctx.font = '20px sans-serif';
            ctx.save();
            ctx.font = '22px sans-serif';
            ctx.translate(34, (T + H - B) / 2); ctx.rotate(-Math.PI / 2);
            ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
            ctx.fillText(!cal ? 'band power (counts, uncalibrated)'
                         : drift ? 'antenna temperature (K, T_sys subtracted)'
                         : (d.opacity_applied
                            ? 'solar flux (SFU, above the atmosphere)'
                            : 'solar flux (SFU, T_sys subtracted)'), 0, 0);
            ctx.restore();

            // The crossing time the pointing was laid out for. Drawn before the
            // trace so the data sits on top of it, and dashed so it cannot be
            // mistaken for a measurement.
            if (drift && d.t_transit && d.t_transit >= tStart && d.t_transit <= tEnd) {
                const xt = X(d.t_transit);
                ctx.save();
                ctx.setLineDash([10, 8]);
                ctx.strokeStyle = '#8888aa'; ctx.lineWidth = 2;
                ctx.beginPath(); ctx.moveTo(xt, T); ctx.lineTo(xt, H - B); ctx.stroke();
                ctx.setLineDash([]);
                ctx.fillStyle = '#8888aa'; ctx.font = '18px sans-serif';
                ctx.textAlign = 'center'; ctx.textBaseline = 'top';
                ctx.fillText('beam crossing', xt, T + 4);
                ctx.restore();
            }

            if (!xs.length) {
                document.getElementById('obvLiveInfo').textContent =
                    d.note || 'waiting for the first record';
                return;
            }

            // A finished run is drawn in a cooler colour than a live one, so
            // it is obvious at a glance that nothing is arriving any more.
            ctx.strokeStyle = d.finished ? '#00d4ff' : '#ffa502';
            ctx.lineWidth = 2.5;
            // Drawn as a staircase, matching the spectrum and calibration
            // plots. Each point is an average over a finite time - a record,
            // or several binned together - so it is a level held across an
            // interval and not a vertex on a curve. Joining the centres draws
            // slopes between samples that were never measured, and on a flux
            // monitor that is exactly the kind of structure someone would
            // otherwise read as real.
            ctx.beginPath();
            for (let i = 0; i < xs.length; i++) {
                const half = i ? (xs[i] - xs[i - 1]) / 2
                               : (xs.length > 1 ? (xs[1] - xs[0]) / 2 : 0.5);
                const halfR = (i < xs.length - 1) ? (xs[i + 1] - xs[i]) / 2 : half;
                const x0 = X(xs[i] - half), x1 = X(xs[i] + halfR), y = Y(ys[i]);
                if (i === 0) ctx.moveTo(x0, y); else ctx.lineTo(x0, y);
                ctx.lineTo(x1, y);
            }
            ctx.stroke();

            const last = ys[ys.length - 1];
            const mean = ys.reduce((a, b) => a + b, 0) / ys.length;
            // Say when the plot is showing averages rather than records. The
            // whole run is always drawn - it is binned down, never truncated -
            // but a point that is twelve records deep is a different thing
            // from one that is a single measurement, and the noise on screen
            // is smaller for a reason the reader should know.
            let text = (d.records || d.points.length) + ' records';
            if (d.finished) {
                text = 'finished' + (d.ended_at ? ' ' + d.ended_at.replace('T', ' ') + ' local' : '')
                     + ' · ' + text;
            }
            // Say what was left off. The first record of a run comes in several
            // percent low while the flowgraph settles, and dropping it silently
            // would be a plot that quietly disagrees with the recording.
            if (d.warmup_dropped) {
                text += ' · first ' + d.warmup_s.toFixed(0) + ' s ('
                      + d.warmup_dropped + (d.warmup_dropped === 1 ? ' record' : ' records')
                      + ') left off as warm-up';
            }
            if (d.binned > 1) {
                text += ' · ' + d.points.length + ' points, '
                      + d.binned + ' records averaged into each';
            }
            if (cal && drift) {
                text += ' · latest ' + last.toFixed(2) + ' K · mean '
                      + mean.toFixed(2) + ' K · peak ' + Math.max.apply(null, ys).toFixed(2) + ' K';
                if (d.t_sys_k) text += ' · T_sys ' + d.t_sys_k.toFixed(0) + ' K subtracted';
                // How far through, against the window rather than against the
                // data - the two differ, and the window is the honest one.
                if (!d.finished && d.t_end > d.t_start) {
                    const frac = (xs[xs.length - 1] - tStart) / (tEnd - tStart);
                    text += ' · ' + Math.round(100 * Math.max(0, Math.min(1, frac)))
                          + '% through the scan';
                    // The cadence, so a long integration does not read as a
                    // stall: at 30 s per record the plot is *supposed* to sit
                    // still for 30 s at a time, and on a long window each new
                    // point is a sliver at the far left.
                    const lastP = d.points[d.points.length - 1];
                    if (lastP && lastP.tau && lastP.n) {
                        text += ' · one record every '
                              + Math.round(lastP.tau / lastP.n) + ' s';
                    }
                }
                text += ' · band median, so a narrow line moves it little';
            } else if (cal) {
                text += ' · latest ' + last.toFixed(1) + ' SFU · mean '
                      + mean.toFixed(1) + ' SFU';
                if (d.t_sys_k) text += ' · T_sys ' + d.t_sys_k.toFixed(0) + ' K subtracted';
                // Small, but it grows fast as the Sun sets, so the airmass it
                // was computed at belongs on screen beside the number.
                if (d.opacity_applied) {
                    const last = d.points[d.points.length - 1];
                    text += ' · above the atmosphere (zenith opacity '
                          + d.zenith_opacity.toFixed(3) + ' Np';
                    if (last && last.airmass) {
                        text += ', now airmass ' + last.airmass.toFixed(2)
                              + ' at alt ' + last.alt_deg.toFixed(1) + '\u00b0';
                    }
                    text += ')';
                }
            } else {
                text += ' · no gain calibration for this tuning, so counts: ' + (d.why || '');
            }
            document.getElementById('obvLiveInfo').textContent = text;
        }

        // setTimeout chained rather than setInterval, so the wait can change
        // between polls and two requests can never overlap on a slow reply.
        function obvLiveStart() {
            obvLiveStop();
            obvLivePoll();
        }

        function obvLiveStop() {
            if (obvLiveTimer) { clearTimeout(obvLiveTimer); obvLiveTimer = null; }
        }
