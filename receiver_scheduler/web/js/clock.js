// The clocks: the Thunderbolt (10 MHz and PPS), this computer's NTP and the
// controller's NTP, from /api/clock. A line under the banner's times shows
// the worst of them on every tab; the Clocks card on the RF calibration tab
// shows all three, and the Thunderbolt's last six hours.

        const CLOCK_COLOURS = {ok: '#4caf50', warn: '#ff9500', stale: '#ff4444', bad: '#ff4444', absent: '#666'};
        const CLOCK_NAMES = {thunderbolt: 'Reference (Thunderbolt)', host: 'This computer', controller: 'Controller'};
        // What the Thunderbolt's numbers compare (hover text; no quotes or
        // angle brackets, since it goes into an attribute as it stands).
        const CLOCK_TB_HELP =
            '10 MHz: the frequency error of the Thunderbolt\u2019s own oscillator - the 10 MHz the B200 runs from - ' +
            'against GPS time as its GPS receiver sees it, in parts per billion (1 ppb = 0.01 Hz at 10 MHz). ' +
            'PPS: the time of its pulse-per-second output - the PPS that times the B200 - against the GPS second, ' +
            'in nanoseconds. Both are the unit\u2019s own estimates of how closely it follows GPS, not an ' +
            'independent check against UTC; on short timescales their scatter is GPS measurement noise, ' +
            'not the oscillator, which is far steadier over seconds. ' +
            'Antenna-cable and receiver delays are not in them. ' +
            'The PPS output is counted down from the same disciplined oscillator as the 10 MHz, so the two are one clock. ' +
            'Loop: the time constant over which GPS steers the oscillator; below it the output is the free oscillator, above it GPS. ' +
            'Steering: the control voltage the unit applies to the oscillator to hold it on GPS; ' +
            'a voltage drifting to either end of its range is an ageing oscillator running out of adjustment. ' +
            'Temperature: inside the unit.';
        const CLOCK_TRACE_HELP = {
            osc_ppb: 'Frequency error of the 10 MHz oscillator against GPS, as the unit estimates it each second (ppb). The short-term scatter is GPS noise, not the oscillator.',
            pps_ns: 'Time of the PPS output against the GPS second, as the unit estimates it (ns). The short-term scatter is GPS noise, not the oscillator.',
            dac_v: 'Control voltage steering the oscillator onto GPS (V). Flat and mid-range is healthy.',
            temp_c: 'Temperature inside the unit (\u00b0C). The oscillator has its own oven; this is the air around it.'};

        function refreshClocks() {
            const panelOpen = document.getElementById('tab-rf').classList.contains('active');
            fetch('/api/clock' + (panelOpen ? '?history=1' : '')).then(r => r.json()).then(d => {
                drawClockChip(d);
                if (panelOpen) drawClockPanel(d);
            }).catch(() => {
                const chip = document.getElementById('clockChip');
                chip.textContent = 'Clocks: scheduler not answering';
                chip.style.color = CLOCK_COLOURS.absent;
            });
        }

        function drawClockChip(d) {
            const chip = document.getElementById('clockChip');
            const names = ['thunderbolt', 'host', 'controller'];
            // The line names the clock that is worst, or says all is well -
            // with the reference's own words when it is connected.
            const order = ['bad', 'stale', 'warn'];
            let text = 'Clocks OK';
            for (const lv of order) {
                const k = names.find(n => d[n] && d[n].level === lv);
                if (k) { text = CLOCK_NAMES[k] + ': ' + d[k].text; break; }
            }
            if (text === 'Clocks OK' && d.thunderbolt && d.thunderbolt.level === 'ok') {
                const sats = d.thunderbolt.satellites ? ', ' + d.thunderbolt.satellites.n_sats + ' sats' : '';
                text = 'Clocks OK · GPS locked' + sats;
            }
            chip.textContent = '● ' + text;
            chip.style.color = CLOCK_COLOURS[d.overall] || CLOCK_COLOURS.absent;
            chip.title = names.map(n => CLOCK_NAMES[n] + ': ' + (d[n] ? d[n].text : '?')).join('\n') +
                         '\n\nClick for the clock panel (RF calibration tab).';
        }

        function showClockPanel() {
            switchTab('rf');
            const el = document.getElementById('clockPanel');
            if (el) el.scrollIntoView({behavior: 'smooth', block: 'start'});
        }

        function clockRow(name, c, detail) {
            const colour = CLOCK_COLOURS[c.level] || CLOCK_COLOURS.absent;
            return '<tr><td style="padding:4px 12px 4px 0; color:#ccc;">' + name + '</td>' +
                   '<td style="padding:4px 12px 4px 0; color:' + colour + ';">● ' + (c.level || '?') + '</td>' +
                   '<td style="padding:4px 12px 4px 0;">' + escapeClock(c.text || '') + '</td>' +
                   '<td style="padding:4px 0; color:#888;">' + detail + '</td></tr>';
        }

        // Text only ever lands between tags here, never in an attribute, so
        // &, < and > are all that need escaping.
        function escapeClock(s) {
            return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        }

        function drawClockPanel(d) {
            const tb = d.thunderbolt || {}, host = d.host || {}, ctrl = d.controller || {};
            const s = tb.supplemental;
            let tbDetail = escapeClock(tb.device || '');
            if (s) {
                // Say what each number compares: all four are the unit's own
                // reports, the first two its GPS receiver's estimates.
                tbDetail = '<span title="' + CLOCK_TB_HELP + '">' +
                           '10 MHz ' + s.osc_offset_ppb.toFixed(3) + ' ppb from GPS · PPS ' +
                           s.pps_offset_ns.toFixed(1) + ' ns from GPS time · steering ' + s.dac_v.toFixed(4) +
                           ' V · ' + s.temperature_c.toFixed(1) + ' °C inside' +
                           (tb.satellites ? ' · ' + tb.satellites.n_sats + ' sats' : '') +
                           ' · ' + escapeClock(s.disciplining_activity_text) +
                           (tb.loop ? ' · loop ' + Math.round(tb.loop.time_constant_s) + ' s' : '') +
                           (tb.oscillator && tb.oscillator.dac_max_v > tb.oscillator.dac_min_v
                               ? ' · DAC at ' + Math.round(100 * (s.dac_v - tb.oscillator.dac_min_v) /
                                     (tb.oscillator.dac_max_v - tb.oscillator.dac_min_v)) + '% of range' : '') +
                           '</span>';
            }
            const hostDetail = [host.server, host.poll ? 'poll ' + host.poll : '',
                                host.jitter_ms != null ? 'jitter ' + host.jitter_ms.toFixed(2) + ' ms' : '']
                               .filter(Boolean).map(escapeClock).join(' · ');
            const ctrlDetail = ctrl.sync_count != null
                ? 'syncs ' + ctrl.sync_count + ' · last correction ' + ctrl.last_offset_ms + ' ms' +
                  (ctrl.offset_s != null ? ' · ' + (ctrl.offset_s >= 0 ? '+' : '') + ctrl.offset_s + ' s from this computer' : '')
                : '';
            document.getElementById('clockTable').innerHTML =
                clockRow(CLOCK_NAMES.thunderbolt, tb, tbDetail) +
                clockRow(CLOCK_NAMES.host, host, hostDetail) +
                clockRow(CLOCK_NAMES.controller, ctrl, ctrlDetail);
            let alarms = '';
            if (s && (s.critical_alarms || s.minor_alarms)) {
                alarms = 'Alarms: ' + s.critical_alarms_text.concat(s.minor_alarms_text).map(escapeClock).join(', ');
            }
            document.getElementById('clockAlarms').textContent = alarms;
            drawClockTraces(d.history || []);
        }

        // Four small traces of the Thunderbolt's last six hours: its own
        // estimates of the 10 MHz and PPS errors, the steering voltage (a
        // voltage walking to a rail is the ageing oscillator running out of
        // range) and the temperature. Plain SVG; nothing to load.
        function drawClockTraces(h) {
            const box = document.getElementById('clockTraces');
            if (!h.length) {
                box.innerHTML = '<div style="color:#666; font-size:12px;">No Thunderbolt history yet.</div>';
                return;
            }
            const CLOCK_MIN_SPAN = {osc_ppb: 0.2, pps_ns: 2.0, dac_v: 0.005, temp_c: 0.5};
            const series = [['osc_ppb', '10 MHz error vs GPS', 'ppb'], ['pps_ns', 'PPS vs GPS second', 'ns'],
                            ['dac_v', 'Oscillator steering', 'V'], ['temp_c', 'Temperature inside', '°C']];
            const t0 = h[0].t, t1 = h[h.length - 1].t || t0 + 1;
            const W = 260, H = 60;
            box.innerHTML = series.map(([key, label, unit]) => {
                const v = h.map(p => p[key]);
                const lo = Math.min(...v), hi = Math.max(...v);
                // At least CLOCK_MIN_SPAN tall, centred on the data: autoscaled
                // to its own range, 3.5 mK of settling filled the box as a
                // steep rise, and a constant value drew along the bottom
                // border and vanished (2026-09-30).
                const span = Math.max(hi - lo, CLOCK_MIN_SPAN[key]);
                const base = (lo + hi) / 2 - span / 2;
                const pts = h.map((p, i) => ((p.t - t0) / ((t1 - t0) || 1) * W).toFixed(1) + ',' +
                                            (H - 2 - (v[i] - base) / span * (H - 4)).toFixed(1)).join(' ');
                const last = v[v.length - 1];
                return '<div style="display:inline-block; margin:0 18px 10px 0;" title="' + CLOCK_TRACE_HELP[key] + '">' +
                       '<div style="color:#888; font-size:11px;">' + label + ': ' + last.toPrecision(4) + ' ' + unit +
                       ' <span style="color:#555;">(' + lo.toPrecision(3) + '…' + hi.toPrecision(3) + ')</span></div>' +
                       '<svg width="' + W + '" height="' + H + '" style="background:#0f0f23; border:1px solid #333;">' +
                       '<polyline fill="none" stroke="#00d4ff" stroke-width="1" points="' + pts + '"/></svg></div>';
            }).join('') +
            '<div style="color:#555; font-size:11px;">' + ((t1 - t0) / 3600).toFixed(1) + ' h to now · ' +
            'all four are the Thunderbolt\u2019s own reports: its estimates of how far its 10 MHz and PPS are from ' +
            'GPS, the voltage it steers its oscillator with, and its temperature (hover for more)</div>';
        }

        // Modified Allan deviation of the Thunderbolt's PPS-offset record, on
        // demand (/api/clock/stability). The record is the unit's PPS output
        // against its own GPS solution, so on short timescales it is GPS
        // measurement noise; the dashed lines separate that from the output
        // (oscillator and steering) by the slopes of the fitted noise terms.
        function drawClockStability() {
            const box = document.getElementById('clockStability');
            const note = document.getElementById('clockStabilityNote');
            note.textContent = 'Computing…';
            fetch('/api/clock/stability').then(r => r.json()).then(d => {
                if (!d.ok) { note.textContent = d.error || 'not available'; box.innerHTML = ''; return; }
                note.textContent = (d.n_s / 3600).toFixed(2) + ' h of one-second data' +
                    (d.crossover_s ? ' · GPS noise and output noise cross at ~' + Math.round(d.crossover_s) + ' s' : '');
                box.innerHTML = stabilitySvg(d);
            }).catch(e => { note.textContent = 'Failed: ' + e; });
        }

        function stabilitySvg(d) {
            const W = 620, H = 330, L = 72, R = 170, T = 14, B = 44;
            const pw = W - L - R, ph = H - T - B;
            const lx = v => Math.log10(v);
            const x0 = 0, x1 = Math.ceil(lx(d.tau[d.tau.length - 1]));
            const vals = d.mdev.concat(d.fit_total).filter(v => v > 0);
            const ylo = Math.floor(lx(Math.min(...vals)) - 0.2), yhi = Math.ceil(lx(Math.max(...vals)) + 0.1);
            const X = t => L + (lx(t) - x0) / ((x1 - x0) || 1) * pw;
            const Y = v => T + (yhi - lx(Math.max(v, Math.pow(10, ylo)))) / (yhi - ylo) * ph;
            let g = '';
            for (let k = x0; k <= x1; k++) {
                g += '<line x1="' + X(Math.pow(10, k)) + '" x2="' + X(Math.pow(10, k)) + '" y1="' + T + '" y2="' + (T + ph) +
                     '" stroke="#223" /><text x="' + X(Math.pow(10, k)) + '" y="' + (T + ph + 16) +
                     '" fill="#888" font-size="11" text-anchor="middle">10^' + k + '</text>';
            }
            for (let k = ylo; k <= yhi; k++) {
                g += '<line x1="' + L + '" x2="' + (L + pw) + '" y1="' + Y(Math.pow(10, k)) + '" y2="' + Y(Math.pow(10, k)) +
                     '" stroke="#223" /><text x="' + (L - 6) + '" y="' + (Y(Math.pow(10, k)) + 4) +
                     '" fill="#888" font-size="11" text-anchor="end">1e' + k + '</text>';
            }
            const line = (ys, colour, dash) => {
                const pts = d.fit_tau.map((t, i) => ys[i] > 0 ? X(t).toFixed(1) + ',' + Y(ys[i]).toFixed(1) : null)
                                     .filter(Boolean).join(' ');
                return '<polyline fill="none" stroke="' + colour + '" stroke-width="1.5"' +
                       (dash ? ' stroke-dasharray="6,4"' : '') + ' points="' + pts + '"/>';
            };
            g += line(d.fit_total, '#777', false) + line(d.fit_gps, '#ff9500', true) + line(d.fit_output, '#4caf50', true);
            d.tau.forEach((t, i) => {
                const v = d.mdev[i], e = d.err[i];
                g += '<line x1="' + X(t) + '" x2="' + X(t) + '" y1="' + Y(v + e) + '" y2="' + Y(Math.max(v - e, v * 0.1)) +
                     '" stroke="#00d4ff" stroke-width="1"/><circle cx="' + X(t) + '" cy="' + Y(v) + '" r="2.6" fill="#00d4ff"/>';
            });
            if (d.crossover_s) {
                g += '<line x1="' + X(d.crossover_s) + '" x2="' + X(d.crossover_s) + '" y1="' + T + '" y2="' + (T + ph) +
                     '" stroke="#aaa" stroke-dasharray="2,3"/>';
            }
            const key = [['#00d4ff', 'measured (PPS vs GPS)', false], ['#ff9500', 'GPS measurement noise', true],
                         ['#4caf50', 'output: oscillator + steering', true], ['#777', 'sum of the fit', false]];
            key.forEach(([c, label, dash], i) => {
                const y = T + 14 + i * 18, x = L + pw + 14;
                g += '<line x1="' + x + '" x2="' + (x + 22) + '" y1="' + y + '" y2="' + y + '" stroke="' + c +
                     '" stroke-width="2"' + (dash ? ' stroke-dasharray="6,4"' : '') + '/><text x="' + (x + 28) + '" y="' +
                     (y + 4) + '" fill="#ccc" font-size="11">' + label + '</text>';
            });
            g += '<text x="' + (L + pw / 2) + '" y="' + (H - 6) + '" fill="#aaa" font-size="12" text-anchor="middle">averaging time τ (s)</text>' +
                 '<text x="14" y="' + (T + ph / 2) + '" fill="#aaa" font-size="12" text-anchor="middle" transform="rotate(-90 14 ' +
                 (T + ph / 2) + ')">modified Allan deviation</text>';
            return '<svg width="' + W + '" height="' + H + '" style="background:#0f0f23; border:1px solid #333;">' + g + '</svg>' +
                   '<div style="color:#666; font-size:11px; max-width:620px; margin-top:4px;">The unit’s own report of its ' +
                   'oscillator-derived PPS against its GPS solution — how closely it tracks GPS, not the stability of the ' +
                   '10 MHz itself, which needs an independent reference (against a hydrogen maser a Thunderbolt reads about ' +
                   '1e-12 at 1 s, a hump to 1e-11 near its loop time constant, mid-1e-14 at a day: Van Baak, leapsecond.com). ' +
                   'The reported offset is filtered inside the unit, so the shortest taus show that filter too. ' +
                   'Dashed: fitted by slope — GPS measurement noise as white and flicker phase noise ' +
                   '(τ^−3/2, τ^−1), the output as white, flicker and random-walk frequency noise ' +
                   '(τ^−1/2, flat, τ^+1/2). The record restarts with the scheduler.</div>';
        }
