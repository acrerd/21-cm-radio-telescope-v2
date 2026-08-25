// The Observe tab: hand-off from the simulator, Start Now, and the plot.
//
// Part of the operator page, split out of one 2144-line file on 2026-08-25.
// Loaded as a classic script: everything here shares one global scope,
// exactly as it did before the split.

        function onObserveModeChange() {
            const mode = document.getElementById('obvMode').value;
            const drift = mode === 'drift';
            const solar = mode === 'solar';
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
                    if (force) setObserveStatus('Nothing handed over yet - press Realise in the Simulator tab.', '#ffa502');
                    return;
                }
                const p = d.params;
                if (!force && p.source_utc === obvAppliedStamp) return;
                obvAppliedStamp = p.source_utc;
                document.getElementById('obvMode').value = p.mode;
                document.getElementById('obvL').value = p.l;
                document.getElementById('obvB').value = p.b;
                document.getElementById('obvCenterFreq').value = p.center_freq_mhz;
                document.getElementById('obvBandwidth').value = p.bandwidth_mhz;
                document.getElementById('obvChannels').value = p.channels;
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
                    when.toLocaleTimeString() + '</strong> &mdash; ' +
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
            const drift = mode === 'drift';
            const solar = mode === 'solar';
            const num = (id, dflt) => {
                const v = parseFloat(document.getElementById(id).value);
                return Number.isFinite(v) ? v : dflt;
            };
            const l = num('obvL', 0), b = num('obvB', 0);
            const obs = Object.assign({}, DEFAULTS, {
                name: document.getElementById('obvName').value.trim() || 'Simulator target',
                coord_system: solar ? 'object' : (drift ? 'drift' : 'galactic'),
                object_name: solar ? 'sun' : '',
                drift_frame: 'galactic',
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
                center_freq_mhz: num('obvCenterFreq', 1420.405752),
                bandwidth_mhz: num('obvBandwidth', 2.4),
                gain_db: num('obvGain', 40),
                channels: Math.round(num('obvChannels', 4096)),
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
                } else {
                    setObserveStatus('Failed: ' + (d.error || 'unknown') + ' - see the Log tab.', '#ff4757');
                }
            }).catch(e => setObserveStatus('Failed: ' + e, '#ff4757'))
              .finally(() => { btn.disabled = false; });
        }

        function loadObserveLast() {
            fetch('/api/observe/last').then(r => r.json()).then(d => {
                const el = document.getElementById('obvLastInfo');
                if (!d.available) {
                    el.textContent = 'Nothing has finished yet this session.';
                    return;
                }
                const kind = d.mode === 'drift' ? 'Drift scan' : 'Spectrum';
                const size = d.size_bytes ? ' &mdash; ' + (d.size_bytes / 1e6).toFixed(1) + ' MB' : '';
                el.innerHTML = kind + ' &ldquo;' + d.name + '&rdquo;, ended ' +
                    new Date(d.ended_at).toLocaleTimeString() +
                    ' &mdash; <code>' + d.filename + '</code>' + size +
                    (d.exists ? '' : ' <span style="color:#ff4757;">(file missing)</span>');
            }).catch(() => {});
        }

        function showObservePlot() {
            const host = document.getElementById('obvPlot');
            host.innerHTML = '<span style="color:#888; font-size:12px;">Drawing&hellip;</span>';
            // Fetched rather than dropped straight into an img src: a refusal
            // comes back as JSON with a reason - still recording, no spectra -
            // and a broken image icon would throw that away.
            fetch('/api/observe/plot?' + Date.now()).then(r => {
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

        function observeToScheduler() {
            // Reuse the Add Observation form rather than duplicating date, time
            // and clash handling here: it is the one place that knows how a
            // drift slot is laid out around its transit.
            document.getElementById('modalTitle').textContent = 'Add Observation';
            document.getElementById('obsIndex').value = -1;
            fillForm(observeToObs());
            document.getElementById('obsModal').classList.add('active');
        }

        function refreshObserveTuning() {
            const params = new URLSearchParams({
                center_freq_mhz: document.getElementById('obvCenterFreq').value || 1420.405752,
                bandwidth_mhz: document.getElementById('obvBandwidth').value || 2.4,
                channels: document.getElementById('obvChannels').value || 4096,
            });
            fetch('/api/tuning?' + params).then(r => r.json()).then(p => {
                const box = document.getElementById('obvTuning');
                if (!p.success) { box.textContent = p.error || 'Tuning unavailable'; return; }
                const mhz = v => (v / 1e6).toFixed(6);
                let html = '<div style="color:#00d4ff; margin-bottom:4px;">The receiver will be tuned to '
                         + mhz(p.tuned_center_freq_hz) + ' MHz</div>';
                html += '<div>' + (p.lo_offset_hz / 1e6).toFixed(2) + ' MHz above '
                     + mhz(p.sky_center_freq_hz) + ' MHz, so the DC artefact lands clear of the line '
                     + 'instead of on top of it.</div>';
                if (p.sample_rate_raised) {
                    html += '<div style="color:#ffa502; margin-top:4px;">Bandwidth raised '
                         + (p.requested_sample_rate_hz / 1e6).toFixed(2) + ' &rarr; '
                         + (p.sample_rate_hz / 1e6).toFixed(2) + ' MHz to keep the line in the flat '
                         + 'part of the band';
                    if (p.channels !== p.requested_channels) {
                        html += ', and channels ' + p.requested_channels + ' &rarr; ' + p.channels
                             + ' to hold ' + (p.channel_width_hz / 1e3).toFixed(2) + ' kHz resolution';
                    }
                    html += '.</div>';
                }
                box.innerHTML = html;
            }).catch(() => {});
        }

        function scheduleObserveTuning() {
            if (obvTuningTimer) clearTimeout(obvTuningTimer);
            obvTuningTimer = setTimeout(refreshObserveTuning, 250);
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
            // A finished run has nothing more to add, so stop asking. The plot
            // stays on screen; opening the tab again polls once and redraws it.
            if (d && d.finished) { obvLiveTimer = null; return; }
            obvLiveTimer = setTimeout(obvLivePoll, obvLiveInterval(d));
        }

        function obvLivePoll() {
            const box = document.getElementById('obvLiveBox');
            fetch('/api/observe/live?limit=2000').then(r => r.json()).then(d => {
                obvLiveSchedule(d);
                if (!d.success || !d.is_solar) { box.style.display = 'none'; return; }
                box.style.display = '';
                if (!d.points || !d.points.length) {
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
            ctx.clearRect(0, 0, W, H);

            const cal = d.calibrated;
            const ys = d.points.map(p => cal ? p.sfu : p.counts);
            // Absolute UTC seconds. Elapsed minutes were easier to draw but
            // meant nothing once the plot was one of several: solar activity is
            // reported against the clock, and a feature here has to be matched
            // to a flare time, to the published index, or to another
            // instrument's record.
            const xs = d.points.map(p => p.t);
            let ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys);
            if (!(ymax > ymin)) { ymax = ymin + 1; }
            const pad = 0.08 * (ymax - ymin);
            ymin -= pad; ymax += pad;
            const tStart = xs[0];
            const tEnd = Math.max(xs[xs.length - 1], tStart + 1);
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
                ctx.fillText(v.toFixed(cal ? 1 : 5), L - 8, y);
            }
            ctx.textAlign = 'center'; ctx.textBaseline = 'top';
            const fine = span < 1200;            // under 20 minutes, show seconds
            for (let i = 0; i <= 5; i++) {
                const v = tStart + span * i / 5;
                ctx.fillText(utc(v, fine), X(v), H - B + 10);
            }
            ctx.font = '22px sans-serif';
            ctx.fillText('UTC on ' + new Date(tStart * 1000).toISOString().slice(0, 10)
                         + (d.finished ? '   (finished \u2014 kept until the next run)' : ''),
                         (L + W - R) / 2, H - 34);
            ctx.font = '20px sans-serif';
            ctx.save();
            ctx.font = '22px sans-serif';
            ctx.translate(34, (T + H - B) / 2); ctx.rotate(-Math.PI / 2);
            ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
            ctx.fillText(cal ? 'solar flux (SFU, T_sys subtracted)'
                             : 'band power (counts, uncalibrated)', 0, 0);
            ctx.restore();

            // A finished run is drawn in a cooler colour than a live one, so
            // it is obvious at a glance that nothing is arriving any more.
            ctx.strokeStyle = d.finished ? '#00d4ff' : '#ffa502';
            ctx.lineWidth = 2.5;
            ctx.beginPath();
            xs.forEach((x, i) => { i ? ctx.lineTo(X(x), Y(ys[i])) : ctx.moveTo(X(x), Y(ys[i])); });
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
                text = 'finished' + (d.ended_at ? ' ' + d.ended_at.replace('T', ' ') : '')
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
            if (cal) {
                text += ' · latest ' + last.toFixed(1) + ' SFU · mean '
                      + mean.toFixed(1) + ' SFU';
                if (d.t_sys_k) text += ' · T_sys ' + d.t_sys_k.toFixed(0) + ' K subtracted';
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
