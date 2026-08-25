// The Sun Scan tab and the calibration day, including fitting and applying the
// pointing model.
//
// Part of the operator page, split out of one 2144-line file on 2026-08-25.
// Loaded as a classic script: everything here shares one global scope,
// exactly as it did before the split.

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
                respect_local_horizon:
                    document.getElementById('ssRespectHorizon').checked,
            };
            fetch('/api/sunscan/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(params)
            }).then(r => r.json()).then(data => {
                if (data.success) {
                    if (data.horizon_warning) {
                        alert('Local horizon: ' + data.horizon_warning +
                              ' The scan is starting anyway.');
                    }
                    document.getElementById('ssStartBtn').style.display = 'none';
                    document.getElementById('ssStopBtn').style.display = 'inline-block';
                    document.getElementById('ssProgress').style.display = 'block';
                    document.getElementById('ssStatus').innerHTML = '<span style="color:#00d4ff;">Starting sun scan...</span>';
                    document.getElementById('ssImageContainer').innerHTML = '';
                    if (!ssPollTimer) ssPollTimer = setInterval(pollSunScan, 2000);
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
                    // The scan may have been started by the schedule, by the
                    // calibration day, or before this page was loaded, so the
                    // poll starts its own timer rather than relying on the
                    // Start button having done it.
                    if (!ssPollTimer) ssPollTimer = setInterval(pollSunScan, 2000);
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
            }).catch(e => {
                document.getElementById('cdStatus').innerHTML = '<span style="color:#ff4757;">Could not start calibration: ' + e + '</span>';
            });
        }

        function stopCalDay() {
            fetch('/api/calday/stop', {method: 'POST'}).then(() => pollCalDay()).catch(e => {
                document.getElementById('cdStatus').innerHTML = '<span style="color:#ff4757;">Stop request failed: ' + e + '</span>';
            });
        }

        function pollCalDay() {
            fetch('/api/calday/status').then(r => r.json()).then(data => {
                if (data.running) {
                    // A calibration day outlives this page — it can be started
                    // by the schedule, and it runs for a whole day across any
                    // number of browser reloads. Start the timer from here so
                    // the display follows the run rather than the button press.
                    if (!cdPollTimer) cdPollTimer = setInterval(pollCalDay, 3000);
                    document.getElementById('cdStartBtn').style.display = 'none';
                    document.getElementById('cdStopBtn').style.display = 'inline-block';
                    let info = '<span style="color:#00d4ff;">Running</span> &mdash; ';
                    info += data.scans_completed + ' scans completed';
                    if (data.phase === 'waiting_for_sunrise') {
                        info += '<br><span style="color:#ffaa00;">Waiting for the Sun to reach 5&deg; altitude</span>';
                    } else if (data.phase === 'waiting_for_clear_horizon') {
                        info += '<br><span style="color:#ffaa00;">Sun is behind the obstructed horizon; waiting for it to clear</span>';
                    } else if (data.phase === 'homing') {
                        info += '<br><span style="color:#ffaa00;">Running physical homing sequence before scan</span>';
                    } else if (data.phase === 'retrying') {
                        info += '<br><span style="color:#ffaa00;">Re-homing and automatically retrying rejected scan</span>';
                    } else if (data.scan_running) {
                        info += '<br><span style="color:#ccc;">Scan in progress (' + data.scan_progress + '/' + data.scan_total + ' points)</span>';
                    } else if (data.next_scan_time) {
                        const next = new Date(data.next_scan_time).toLocaleTimeString();
                        info += '<br><span style="color:#888;">Next scan at ' + next + '</span>';
                    }
                    if (data.last_scan_error) {
                        if (data.consecutive_failures === 0 && (data.phase === 'homing' || data.phase === 'retrying')) {
                            info += '<br><span style="color:#ff9500;">Previous attempt rejected; automatic retry active: ' + data.last_scan_error + '</span>';
                        } else {
                            info += '<br><span style="color:#ff9500;">Last scan failed (' + data.consecutive_failures + '/3): ' + data.last_scan_error + '</span>';
                        }
                    }
                    document.getElementById('cdStatus').innerHTML = info;
                } else {
                    document.getElementById('cdStartBtn').style.display = 'inline-block';
                    document.getElementById('cdStopBtn').style.display = 'none';
                    if (cdPollTimer) { clearInterval(cdPollTimer); cdPollTimer = null; }
                    let info = '<span style="color:#888;">Idle</span>';
                    if (cdArchiveCount) {
                        info += ' &mdash; ' + cdArchiveCount + ' scans on file';
                    } else if (data.scans_completed > 0) {
                        info += ' &mdash; ' + data.scans_completed + ' scans collected';
                    }
                    if (data.error) {
                        info += '<br><span style="color:#ff4757;">' + data.error + '</span>';
                    } else if (data.phase === 'complete') {
                        info += '<br><span style="color:#00ff88;">Completed at sunset.</span>';
                    } else if (data.phase === 'stopped') {
                        info += '<br><span style="color:#ffaa00;">Stopped by user or schedule.</span>';
                    }
                    document.getElementById('cdStatus').innerHTML = info;
                }
            }).catch(e => {
                document.getElementById('cdStatus').innerHTML = '<span style="color:#ff4757;">Status request failed: ' + e + '</span>';
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
                    html += '<tr><td style="color:#888; padding:4px 8px;">Sun azimuth coverage</td><td>' + m.az_coverage_deg.toFixed(1) + '&deg;</td></tr>';
                    html += '<tr><td style="color:#888; padding:4px 8px;">Fit condition</td><td>' + m.condition_number.toFixed(1) + '</td></tr>';
                    // How well the model places the beam, not the uncertainty on
                    // any one term. Those are ~-0.9 correlated with each other,
                    // so each is individually loose while the combination is
                    // tight: showing sigma(IA) here read as +-0.50 deg for a
                    // model whose cross-validated pointing error was 0.021 deg.
                    // The per-term errors stay in the API for diagnostics.
                    if (m.pointing_sigma_alt_deg !== undefined && m.pointing_sigma_alt_deg !== null) {
                        html += '<tr><td style="color:#888; padding:4px 8px;">Pointing uncertainty</td><td>&plusmn;' + m.pointing_sigma_alt_deg.toFixed(3) + '&deg; alt, &plusmn;' + m.pointing_sigma_xel_deg.toFixed(3) + '&deg; cross-el</td></tr>';
                    }
                    if (m.n_outliers) {
                        html += '<tr><td style="color:#888; padding:4px 8px;">Outliers rejected</td><td style="color:#ffa502;">' + m.n_outliers + ' (more than ' + m.outlier_sigma.toFixed(0) + '&sigma; from the model)</td></tr>';
                    }
                    if (m.n_superseded) {
                        html += '<tr><td style="color:#888; padding:4px 8px;">Older scans skipped</td><td style="color:#ffa502;">' + m.n_superseded + ' (recorded before the resident pointing model)</td></tr>';
                    }
                    if (m.n_obstructed) {
                        html += '<tr><td style="color:#888; padding:4px 8px;">Behind the horizon</td><td style="color:#ffa502;">' + m.n_obstructed + ' (Sun inside an obstruction sector)</td></tr>';
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
            }).catch(e => {
                document.getElementById('cdModel').innerHTML = '<span style="color:#ff4757;">Model request failed: ' + e + '</span>';
            });
        }

        function clearCalData() {
            if (!confirm('Clear all accumulated pointing data, and erase the pointing model stored on the telescope controller?')) return;
            fetch('/api/calday/clear', {method: 'POST'}).then(r => r.json()).then(data => {
                if (!data.success) {
                    document.getElementById('cdStatus').innerHTML = '<span style="color:#ff4757;">Clear failed: ' + (data.error || 'Unknown error') + '</span>';
                    return;
                }
                document.getElementById('cdModel').innerHTML = '<span style="color:#888;">No model fitted yet.</span>';
                document.getElementById('cdApplyBtn').style.display = 'none';
                document.getElementById('cdPlotContainer').innerHTML = '';
                cdArchiveCount = 0;
                document.getElementById('cdStatus').innerHTML = '<span style="color:#888;">Data cleared.</span>';
            }).catch(e => {
                document.getElementById('cdStatus').innerHTML = '<span style="color:#ff4757;">Clear request failed: ' + e + '</span>';
            });
        }

        function applyModel() {
            if (!confirm('Store this pointing model on the telescope controller? It replaces the model currently in the controller flash and takes effect on the next slew.')) return;
            fetch('/api/calday/apply', {method: 'POST'}).then(r => r.json()).then(data => {
                if (data.success) {
                    var t = data.terms || {};
                    var lines = Object.keys(t).map(function (k) {
                        return k + ': ' + (t[k] >= 0 ? '+' : '') + t[k].toFixed(4) + '\u00b0';
                    });
                    alert('Stored on the controller.\\n\\n' + lines.join('\\n') +
                          '\\n\\nFitted from ' + data.n_scans + ' scans at ' + data.fitted_utc + '.');
                } else {
                    alert('Failed: ' + (data.error || 'Unknown error'));
                }
            }).catch(e => alert('Apply request failed: ' + e));
        }

        // Load existing model on tab open
        function loadCalModel() {
            fetch('/api/calday/model').then(r => r.json()).then(m => {
                if (m.success) {
                    fitModel();  // re-render
                }
            });
            fetch('/api/calday/data').then(r => r.json()).then(d => {
                if (d.data) {
                    cdArchiveCount = d.data.length;
                    // Re-render through pollCalDay, which knows whether a
                    // calibration day is running; writing "Idle" from here is
                    // what made a live run look stopped.
                    pollCalDay();
                }
            });
        }
