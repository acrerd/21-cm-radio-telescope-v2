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
            // A solar track has no l and b to type: the target is the Sun, and
            // the controller follows it.
            ['obvL', 'obvB'].forEach(function (id) {
                const el = document.getElementById(id);
                if (el) el.disabled = solar;
            });
            const src = document.getElementById('obvSource');
            if (solar && src) {
                src.innerHTML = 'Tracking the <strong>Sun</strong>. The dish '
                    + 'follows it for the whole run; flux is plotted live below '
                    + 'and the spectra are recorded as usual.';
            }
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
                coord1_deg: l, coord1_min: 0, coord1_sec: 0,
                coord2_deg: b, coord2_min: 0, coord2_sec: 0,
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

        function obvLivePoll() {
            const box = document.getElementById('obvLiveBox');
            fetch('/api/observe/live?limit=2000').then(r => r.json()).then(d => {
                if (!d.success || !d.is_solar) { box.style.display = 'none'; return; }
                box.style.display = '';
                if (!d.points || !d.points.length) {
                    document.getElementById('obvLiveInfo').textContent =
                        d.note || 'waiting for the first record';
                    return;
                }
                obvLiveDraw(d);
            }).catch(() => {});
        }

        function obvLiveDraw(d) {
            const c = document.getElementById('obvLiveCanvas');
            const ctx = c.getContext('2d');
            const W = c.width, H = c.height;
            const L = 90, R = 20, T = 20, B = 44;
            ctx.clearRect(0, 0, W, H);

            const cal = d.calibrated;
            const ys = d.points.map(p => cal ? p.sfu : p.counts);
            const t0 = d.points[0].t;
            const xs = d.points.map(p => (p.t - t0) / 60.0);      // minutes
            let ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys);
            if (!(ymax > ymin)) { ymax = ymin + 1; }
            const pad = 0.08 * (ymax - ymin);
            ymin -= pad; ymax += pad;
            const xmax = Math.max(1e-6, xs[xs.length - 1]);
            const X = v => L + (W - L - R) * (v / xmax);
            const Y = v => T + (H - T - B) * (1 - (v - ymin) / (ymax - ymin));

            ctx.strokeStyle = '#333355'; ctx.fillStyle = '#c8c8d8';
            ctx.font = '16px sans-serif'; ctx.lineWidth = 1;
            for (let i = 0; i <= 4; i++) {
                const v = ymin + (ymax - ymin) * i / 4;
                const y = Y(v);
                ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(W - R, y); ctx.stroke();
                ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
                ctx.fillText(v.toFixed(cal ? 1 : 5), L - 8, y);
            }
            ctx.textAlign = 'center'; ctx.textBaseline = 'top';
            for (let i = 0; i <= 4; i++) {
                const v = xmax * i / 4;
                ctx.fillText(v.toFixed(1), X(v), H - B + 8);
            }
            ctx.fillText('minutes since the run started', (L + W - R) / 2, H - 20);
            ctx.save();
            ctx.translate(22, (T + H - B) / 2); ctx.rotate(-Math.PI / 2);
            ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
            ctx.fillText(cal ? 'solar flux (SFU, T_sys subtracted)'
                             : 'band power (counts, uncalibrated)', 0, 0);
            ctx.restore();

            ctx.strokeStyle = '#ffa502'; ctx.lineWidth = 2;
            ctx.beginPath();
            xs.forEach((x, i) => { i ? ctx.lineTo(X(x), Y(ys[i])) : ctx.moveTo(X(x), Y(ys[i])); });
            ctx.stroke();

            const last = ys[ys.length - 1];
            const mean = ys.reduce((a, b) => a + b, 0) / ys.length;
            let text = d.points.length + ' records';
            if (cal) {
                text += ' · latest ' + last.toFixed(1) + ' SFU · mean '
                      + mean.toFixed(1) + ' SFU';
                if (d.t_sys_k) text += ' · T_sys ' + d.t_sys_k.toFixed(0) + ' K subtracted';
            } else {
                text += ' · no gain calibration for this tuning, so counts: ' + (d.why || '');
            }
            document.getElementById('obvLiveInfo').textContent = text;
        }

        function obvLiveStart() {
            if (obvLiveTimer) clearInterval(obvLiveTimer);
            obvLivePoll();
            obvLiveTimer = setInterval(obvLivePoll, 10000);
        }

        function obvLiveStop() {
            if (obvLiveTimer) { clearInterval(obvLiveTimer); obvLiveTimer = null; }
        }
