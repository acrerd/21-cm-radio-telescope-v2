// The RF calibration tab: bandpass, gain, target suggestions and the stage
// countdown.
//
// Part of the operator page, split out of one 2144-line file on 2026-08-25.
// Loaded as a classic script: everything here shares one global scope,
// exactly as it did before the split.

        function rfStartTicking() {
            if (!rfTickTimer) rfTickTimer = setInterval(rfTick, 250);
            rfTick();
        }

        function rfStopTicking() {
            if (rfTickTimer) { clearInterval(rfTickTimer); rfTickTimer = null; }
            rfEndsAt = null; rfTotalS = null;
        }

        function rfTick() {
            const box = document.getElementById('rfCountdown');
            if (!box) return;
            if (!rfEndsAt) {
                // A slew has no knowable duration; say so rather than invent one.
                box.innerHTML = '<span style="color:#888; font-size:12px;">'
                              + 'no fixed duration for this step</span>';
                return;
            }
            const left = Math.max(0, (rfEndsAt - Date.now()) / 1000);
            const total = rfTotalS || 1;
            const done = Math.min(100, Math.max(0, 100 * (1 - left / total)));
            const mm = Math.floor(left / 60), ss = Math.floor(left % 60);
            box.innerHTML =
                '<div style="font-size:26px; font-variant-numeric:tabular-nums; color:#00d4ff;">'
                + mm + ':' + (ss < 10 ? '0' : '') + ss + '</div>'
                + '<div style="color:#888; font-size:12px; margin-bottom:6px;">'
                + Math.round(left) + ' s of ' + Math.round(total) + ' s remaining</div>'
                + '<div style="height:8px; background:#0a0a1a; border:1px solid #333; '
                + 'border-radius:4px; overflow:hidden;">'
                + '<div style="height:100%; width:' + done.toFixed(1) + '%; '
                + 'background:#00d4ff; transition:width .25s linear;"></div></div>';
        }

        function rfAge(iso) {
            if (!iso) return '';
            const mins = (Date.now() - Date.parse(iso)) / 60000;
            if (!isFinite(mins)) return '';
            if (mins < 90) return Math.round(mins) + ' min ago';
            if (mins < 60 * 48) return (mins / 60).toFixed(1) + ' hours ago';
            return (mins / 1440).toFixed(1) + ' days ago';
        }

        function rfRefresh() {
            fetch('/api/rf/status').then(r => r.json()).then(d => {
                if (!d.success) return;
                const bp = document.getElementById('rfBandpassStatus');
                if (!d.bandpass) {
                    bp.innerHTML = '<span style="color:#ffa502;">No template stored &mdash; '
                                 + 'spectra are not being corrected.</span>';
                } else {
                    bp.innerHTML =
                        '<div style="color:#00d4ff;">Order ' + d.bandpass.degree
                        + ' over &plusmn;' + d.bandpass.band_mhz.toFixed(3) + ' MHz, residual '
                        + d.bandpass.residual_pct.toFixed(3) + '%</div>'
                        + '<div>measured ' + rfAge(d.bandpass.created_utc)
                        + (d.bandpass.source_name ? ' at ' + d.bandpass.source_name : '')
                        + ', at LO ' + d.bandpass.lo_mhz.toFixed(6) + ' MHz, '
                        + d.bandpass.sample_rate_mhz.toFixed(3) + ' Msps</div>'
                        + '<div style="color:#666;">Only applies to observations at that '
                        + 'exact tuning; anything else is left uncorrected and says so.</div>';
                }

                const g = document.getElementById('rfGainStatus');
                if (!d.gain) {
                    g.innerHTML = '<span style="color:#ffa502;">Not calibrated &mdash; '
                                + 'spectra are in counts, not kelvin.</span>';
                } else {
                    const warn = d.gain.t_sys_bound_active
                        ? '<div style="color:#ff4757;">T_sys hit its 50 K floor &mdash; the fit '
                        + 'is against the bound, not a measurement. Check the bandpass template '
                        + 'and that the slew arrived.</div>' : '';
                    // The floor at 50 K only catches errors of one sign. A run
                    // that recorded while the mount was still slewing fitted
                    // 467 K and said nothing about it. Worth flagging, but this
                    // telescope runs at 340-372 K and works, so the flag says
                    // how high it is rather than that it cannot be real.
                    const level = d.gain.t_sys_level;
                    const hot = level
                        ? '<div style="color:' + (level === 'very high' ? '#ff4757' : '#ffa502')
                        + ';">T_sys is ' + level + '. Loss ahead of the LNA raises it '
                        + 'genuinely; a sudden change is more likely a recording that began '
                        + 'before the mount arrived, a stale bandpass template, or ground '
                        + 'in the beam.</div>' : '';
                    const weak = (d.gain.correlation < 0.8)
                        ? '<div style="color:#ffa502;">Weak correlation: little lever arm in '
                        + 'this pointing, so T_sys is poorly determined.</div>' : '';
                    g.innerHTML =
                        '<div style="color:#00d4ff;">T_sys ' + d.gain.t_sys_k.toFixed(1)
                        + ' K &nbsp; gain ' + d.gain.gain_counts_per_k.toExponential(3)
                        + ' counts/K</div>'
                        + '<div>from l=' + Math.round(d.gain.glon) + ' b=' + Math.round(d.gain.glat)
                        + ', ' + rfAge(d.gain.observed_utc)
                        + ' &nbsp; r=' + d.gain.correlation.toFixed(3)
                        + ' &nbsp; residual ' + d.gain.residual_rms_k.toFixed(2) + ' K</div>'
                        + (d.gain.implied_ppm
                           ? '<div style="color:#888;">receiver clock '
                             + d.gain.implied_ppm.toFixed(2) + ' ppm ('
                             + d.gain.velocity_shift_km_s.toFixed(2)
                             + ' km/s), fitted with the gain</div>'
                           : '')
                        + (d.gain.implied_loss_db
                           ? '<div style="color:#888;">equivalent to '
                             + d.gain.implied_loss_db.toFixed(2)
                             + ' dB of loss ahead of the LNA, if that is what it is</div>'
                           : '')
                        + warn + hot + weak;
                }

                const st = d.state || {};
                const prog = document.getElementById('rfProgress');
                if (st.running) {
                    let t = st.target ? (' &mdash; l=' + Math.round(st.target.glon)
                          + ' b=' + Math.round(st.target.glat)
                          + (st.target.alt_deg ? ' at alt ' + st.target.alt_deg.toFixed(0) : '')) : '';
                    rfEndsAt = st.stage_ends_utc ? Date.parse(st.stage_ends_utc) : null;
                    rfTotalS = st.stage_total_s || null;
                    prog.innerHTML = '<span style="color:#00d4ff;">' + st.job + ': '
                                   + (st.stage || 'working') + '</span>' + t
                                   + '<div id="rfCountdown" style="margin-top:10px;"></div>';
                    rfStartTicking();
                } else if (st.error) {
                    prog.innerHTML = '<span style="color:#ff4757;">' + st.job + ' failed: '
                                   + st.error + '</span>';
                } else if (st.result) {
                    prog.innerHTML = '<span style="color:#2ed573;">' + st.job
                                   + ' finished.</span>';
                } else {
                    prog.textContent = 'Idle.';
                }
                // Shown whatever the stage: a run that pointed into the trees is
                // worth seeing while it happens and afterwards, because it
                // explains a T_sys that comes out high.
                if (st.horizon_warning) {
                    prog.innerHTML += '<div style="margin-top:10px; color:#ffa502;">'
                                    + '&#9888; ' + st.horizon_warning + '</div>';
                }

                if (st.running && !rfPollTimer) rfPollTimer = setInterval(rfRefresh, 2000);
                if (!st.running && rfPollTimer) { clearInterval(rfPollTimer); rfPollTimer = null; }
                if (!st.running) rfStopTicking();
                // A finished bandpass job means the stored template changed, so
                // the plot on screen is of the previous one.
                if (!st.running && st.result && !rfPlotIsCurrent) {
                    rfPlotIsCurrent = true;
                    if (st.job === 'bandpass') rfLoadBandpassPlot();
                    if (st.job === 'gain') rfLoadGainPlot();
                }
                if (st.running) rfPlotIsCurrent = false;
            }).catch(() => {});
        }

        function rfRefreshTarget() {
            fetch('/api/rf/target').then(r => r.json()).then(d => {
                const box = document.getElementById('rfTargets');
                if (!d.success) { box.textContent = d.error || 'unavailable'; return; }
                const list = d.targets || [];
                if (!list.length) {
                    box.innerHTML = '<span style="color:#ff4757;">Nothing is high enough '
                                  + 'right now, in any direction.</span>';
                    return;
                }
                let h = '<table style="width:100%; border-collapse:collapse; font-size:12px;">'
                      + '<tr style="color:#666; text-align:left;">'
                      + '<th style="padding:4px 8px;">l, b</th>'
                      + '<th style="padding:4px 8px;">alt</th>'
                      + '<th style="padding:4px 8px;">az</th>'
                      + '<th style="padding:4px 8px;">looking</th>'
                      + '<th style="padding:4px 8px;">expected peak</th>'
                      + '<th style="padding:4px 8px;">clears</th>'
                      + '<th></th></tr>';
                list.forEach(function (t) {
                    const b = (t.glat >= 0 ? '+' : '') + Math.round(t.glat);
                    h += '<tr style="border-top:1px solid #262640;">'
                       + '<td class="mono" style="padding:5px 8px; color:#00d4ff;">l='
                       + Math.round(t.glon) + ' b=' + b + '</td>'
                       + '<td class="mono" style="padding:5px 8px;">' + t.alt_deg.toFixed(1) + '&deg;</td>'
                       + '<td class="mono" style="padding:5px 8px;">' + t.az_deg.toFixed(1) + '&deg;</td>'
                       + '<td style="padding:5px 8px; color:#ffa502;">' + t.compass + '</td>'
                       + '<td class="mono" style="padding:5px 8px;">' + t.expected_peak_k.toFixed(0) + ' K</td>'
                       + '<td class="mono" style="padding:5px 8px;">' + rfClears(t.horizon) + '</td>'
                       + '<td style="padding:3px 8px;"><button class="btn" style="padding:3px 10px; font-size:11px;"'
                       + ' onclick="rfUse(' + t.glon + ',' + t.glat + ')">Use</button></td>'
                       + '</tr>';
                });
                // Say what the predicted peaks assume. The Simulator tab
                // defaults to a main-beam efficiency of 0.7 and this to 1.0, so
                // the same direction reads 9.5 K there and 13.5 K here; without
                // the assumptions on screen that difference is a mystery.
                box.innerHTML = h + '</table>'
                    + '<div style="color:#666; font-size:11px; margin-top:6px;">'
                    + 'peaks predicted for a ' + (d.beam_fwhm_deg || 0).toFixed(2)
                    + '&deg; beam at main-beam efficiency '
                    + (d.main_beam_efficiency || 0).toFixed(2)
                    + ' &mdash; the Simulator tab defaults to 0.70, which reads '
                    + (0.70 / (d.main_beam_efficiency || 1)).toFixed(2)
                    + '&times; these values</div>';
            }).catch(() => {});
        }

        function rfLoadBandpassPlot() {
            const host = document.getElementById('rfBandpassPlot');
            host.textContent = 'Drawing\u2026';
            fetch('/api/rf/bandpass/plot?' + Date.now()).then(r => {
                if (!r.ok) return r.json().then(d => { throw new Error(d.error || ('HTTP ' + r.status)); });
                return r.blob();
            }).then(b => {
                const url = URL.createObjectURL(b);
                host.innerHTML = '<img src="' + url + '" style="width:100%; height:auto; '
                               + 'border-radius:8px; border:1px solid #333;">';
            }).catch(e => {
                host.innerHTML = '<span style="color:#ffa502;">' + e.message + '</span>';
            });
        }

        function rfLoadGainPlot() {
            const host = document.getElementById('rfGainPlot');
            host.textContent = 'Drawing\u2026';
            fetch('/api/rf/gain/plot?' + Date.now()).then(r => {
                if (!r.ok) return r.json().then(d => { throw new Error(d.error || ('HTTP ' + r.status)); });
                return r.blob();
            }).then(b => {
                const url = URL.createObjectURL(b);
                host.innerHTML = '<img src="' + url + '" style="width:100%; height:auto; '
                               + 'border-radius:8px; border:1px solid #333;">';
            }).catch(e => {
                host.innerHTML = '<span style="color:#888;">' + e.message + '</span>';
            });
        }

        // How far a candidate sits above the measured floor, once a full
        // beamwidth is allowed for. Amber under 3 deg: that is inside the
        // sampling of the horizon scan itself (5 deg strips), so the true edge
        // could be anywhere in the step below the floor it reported.
        function rfClears(h) {
            if (!h || !h.known) return '<span style="color:#666;">not measured</span>';
            const by = h.alt_deg - h.required_deg;
            if (by < 0) {
                return '<span style="color:#ff4757;">behind by ' + (-by).toFixed(1) + '&deg;</span>';
            }
            const colour = by < 3 ? '#ffa502' : '#2ed573';
            return '<span style="color:' + colour + ';">+' + by.toFixed(1) + '&deg;</span>';
        }

        function rfUse(l, b) {
            document.getElementById('rfGlon').value = l;
            document.getElementById('rfGlat').value = b;
            rfShowChosen();
        }

        function rfShowChosen() {
            const l = document.getElementById('rfGlon').value;
            const b = document.getElementById('rfGlat').value;
            const box = document.getElementById('rfChosen');
            if (l === '' || b === '') {
                box.innerHTML = 'Nothing chosen &mdash; a direction will be picked '
                              + 'automatically from the screened list.';
            } else {
                box.innerHTML = 'Will calibrate on <span style="color:#00d4ff;">l=' + l
                              + ' b=' + b + '</span>. Checking the horizon&hellip;';
                // Ask the server where that lands and whether it is behind the
                // measured horizon. A typed direction used to be taken wholly
                // on trust, on the grounds that only the operator could see the
                // skyline; it is measured now, so it can be said here.
                fetch('/api/rf/target?glon=' + encodeURIComponent(l)
                      + '&glat=' + encodeURIComponent(b))
                    .then(r => r.json()).then(d => {
                        const c = d.chosen;
                        if (!c) return;
                        let txt = 'Will calibrate on <span style="color:#00d4ff;">l='
                                + l + ' b=' + b + '</span> &mdash; alt '
                                + c.alt_deg.toFixed(1) + '&deg; az ' + c.az_deg.toFixed(1)
                                + '&deg;, clears ' + rfClears(c.horizon) + '.';
                        if (c.warning) {
                            txt += '<div style="margin-top:6px; color:#ffa502;">&#9888; '
                                 + c.warning + '</div>';
                        }
                        box.innerHTML = txt;
                    }).catch(() => {});
            }
        }

        function rfRun(job) {
            const secs = job === 'gain' ? 180 : 120;
            const body = {job: job, duration_s: secs,
                          respect_local_horizon:
                              document.getElementById('rfRespectHorizon').checked};
            if (job === 'gain') {
                const l = document.getElementById('rfGlon').value;
                const b = document.getElementById('rfGlat').value;
                if (l !== '' && b !== '') { body.glon = parseFloat(l); body.glat = parseFloat(b); }
            }
            fetch('/api/rf/run', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)
            }).then(r => r.json()).then(d => {
                if (!d.success) { alert(d.error || 'could not start'); return; }
                rfRefresh();
            }).catch(e => alert(e));
        }

        function rfCancel() {
            fetch('/api/rf/cancel', {method: 'POST'}).then(() => rfRefresh());
        }
