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
                if (panelOpen) { drawClockPanel(d); drawClockStability(); }
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
            // Only what is true now: nothing is shown when all is well.
            const warn = tb.warnings || [];
            document.getElementById('clockAlarms').textContent = warn.length ? '⚠ ' + warn.join(' · ') : '';
            drawClockSats(tb);
            drawClockTraces(d.history || [], tb);
        }

        // Signal level of every satellite the unit hears (0x47), the ones in its
        // timing solution (0x6D) bright, the rest dim: a failing antenna, cable
        // or preamp shows here as all the bars sinking together. A tile the
        // height of the traces, so it sits in their row (drawClockTraces).
        function clockSatsTile(tb) {
            const lv = (tb.levels && tb.levels.levels) || {};
            const used = new Set(((tb.satellites && tb.satellites.prns) || []).map(Number));
            const prns = Object.keys(lv).map(Number).sort((a, b) => a - b);
            if (!prns.length) return '';
            const W = 22, top = 11, H = 36, lo = 20, hi = 55;
            const bars = prns.map((p, i) => {
                const v = lv[p], h = Math.max(1, Math.min(1, (v - lo) / (hi - lo)) * H);
                const colour = used.has(p) ? '#00d4ff' : '#446';
                return '<rect x="' + (i * W + 2) + '" y="' + (top + H - h) + '" width="' + (W - 4) + '" height="' + h +
                       '" fill="' + colour + '"><title>PRN ' + p + ': ' + v.toFixed(1) +
                       (used.has(p) ? ' (in the solution)' : ' (tracked, not used)') + '</title></rect>' +
                       '<text x="' + (i * W + W / 2) + '" y="' + (top + H + 11) + '" fill="#888" font-size="9" text-anchor="middle">' + p + '</text>' +
                       '<text x="' + (i * W + W / 2) + '" y="' + (top + H - h - 2) + '" fill="#aaa" font-size="9" text-anchor="middle">' + Math.round(v) + '</text>';
            }).join('');
            return '<div style="flex:0 0 auto;" title="Signal level of each satellite the unit hears, scale ' + lo + '–' + hi +
                   '; bright bars are in the timing solution, dim ones tracked but not used. All sinking together means the antenna, its cable or preamp.">' +
                   '<div style="color:#888; font-size:11px; white-space:nowrap;">Satellites: ' + used.size + ' used, ' + prns.length + ' heard</div>' +
                   '<svg width="' + (prns.length * W + 4) + '" height="60" style="background:#0f0f23; border:1px solid #333;">' +
                   bars + '</svg></div>';
        }

        // Beneath the traces: where the unit thinks its antenna is, and the
        // cable delay it compensates.
        function drawClockSats(tb) {
            const box = document.getElementById('clockSats');
            const s = tb.supplemental, pc = tb.pps_config;
            box.innerHTML = s
                ? '<div style="color:#888; font-size:11px; margin-top:4px;" title="The position the unit surveyed and now holds fixed for timing; height is above the WGS-84 ellipsoid, not sea level. An error in it puts up to (error / c) into the PPS.">' +
                  'Antenna (as the unit holds it): ' + s.lat_deg.toFixed(7) + ', ' + s.lon_deg.toFixed(7) +
                  ', ' + s.alt_m.toFixed(1) + ' m above the ellipsoid' +
                  (pc ? ' · cable delay compensation ' + pc.cable_delay_ns.toFixed(1) + ' ns' : '') + '</div>'
                : '';
        }

        // Four small traces of the Thunderbolt's last six hours: its own
        // estimates of the 10 MHz and PPS errors, the steering voltage (a
        // voltage walking to a rail is the ageing oscillator running out of
        // range) and the temperature. Plain SVG; nothing to load.
        function drawClockTraces(h, tb) {
            const box = document.getElementById('clockTraces');
            const sats = clockSatsTile(tb || {});
            if (!h.length) {
                box.innerHTML = sats +
                                '<div style="color:#666; font-size:12px;">No Thunderbolt history yet.</div>';
                return;
            }
            // Floors only: they stop a flat trace vanishing into the border,
            // and are below what each trace really does (steering wanders ~1.5 mV).
            const CLOCK_MIN_SPAN = {osc_ppb: 0.02, pps_ns: 0.2, dac_v: 0.0005, temp_c: 0.05};
            const series = [['osc_ppb', '10 MHz error vs GPS', 'ppb'], ['pps_ns', 'PPS vs GPS second', 'ns'],
                            ['dac_v', 'Oscillator steering', 'V'], ['temp_c', 'Temperature inside', '°C']];
            const t0 = h[0].t, t1 = h[h.length - 1].t || t0 + 1;
            const W = 260, H = 44;
            box.innerHTML = '<div style="display:flex; flex-direction:column; gap:8px; margin-bottom:8px;">' +
                series.map(([key, label, unit]) => {
                const v = h.map(p => p[key]);
                const lo = Math.min(...v), hi = Math.max(...v);
                // At least CLOCK_MIN_SPAN tall, centred on the data: autoscaled
                // to its own range, 3.5 mK of settling filled the box as a
                // steep rise, and a constant value drew along the bottom
                // border and vanished (2026-09-30).
                const span = Math.max(hi - lo, CLOCK_MIN_SPAN[key]);
                // enough decimals to show the span: two significant figures of it
                const dp = Math.max(0, Math.min(6, 1 - Math.floor(Math.log10(span))));
                const base = (lo + hi) / 2 - span / 2;
                const pts = h.map((p, i) => ((p.t - t0) / ((t1 - t0) || 1) * W).toFixed(1) + ',' +
                                            (H - 2 - (v[i] - base) / span * (H - 4)).toFixed(1)).join(' ');
                const last = v[v.length - 1];
                // One above the other, to the right of the stability plot,
                // each as wide as the column.
                return '<div title="' + CLOCK_TRACE_HELP[key] + '">' +
                       '<div style="color:#888; font-size:11px;">' + label + ': ' + last.toFixed(dp) + ' ' + unit +
                       ' <span style="color:#555;">(' + lo.toFixed(dp) + '…' + hi.toFixed(dp) + ')</span></div>' +
                       '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" width="100%" height="' + H +
                       '" style="display:block; background:#0f0f23; border:1px solid #333; box-sizing:border-box;">' +
                       '<polyline fill="none" stroke="#00d4ff" stroke-width="1" vector-effect="non-scaling-stroke" points="' + pts + '"/></svg></div>';
            }).join('') + sats + '</div>' +
            '<div style="color:#555; font-size:11px;">' + ((t1 - t0) / 3600).toFixed(1) + ' h to now · ' +
            'all four are the Thunderbolt\u2019s own reports: its estimates of how far its 10 MHz and PPS are from ' +
            'GPS, the voltage it steers its oscillator with, and its temperature (hover for more)</div>';
        }

        // Modified Allan deviation of the Thunderbolt's PPS-offset record, on
        // demand (/api/clock/stability): the measured points only, and the
        // transition - where the curve stops falling and turns up - read off
        // them (clocks.transition). No noise model is drawn: the unit smooths
        // what it reports, so GPS and oscillator noise cannot be told apart by
        // slope, and the fit that tried said the opposite of the data
        // (2026-09-30).
        function clockTransitionText(d) {
            const tr = d.transition || {};
            const fmt = v => v >= 100 ? Math.round(v) + ' s' : v.toFixed(v >= 10 ? 0 : 1) + ' s';
            if (tr.found) {
                return 'transition at ~' + fmt(tr.tau_s) + ' (the data place it ' + fmt(tr.span_s[0]) + '–' +
                       fmt(tr.span_s[1]) + '; rise beyond it ' + tr.rise_slope.toFixed(2) + ' ± ' + tr.rise_sigma.toFixed(2) + ')';
            }
            if (tr.tau_s != null && tr.tau_s < tr.tau_max_s) {
                return 'lowest at ~' + fmt(tr.tau_s) + ', but the rise beyond it is not yet significant - more record needed';
            }
            return 'still falling at ' + fmt(tr.tau_max_s || 0) + ', the longest the record reaches: no transition yet';
        }

        function drawClockStability() {
            const box = document.getElementById('clockStability');
            const note = document.getElementById('clockStabilityNote');
            if (!box.innerHTML) note.textContent = 'Computing…';
            fetch('/api/clock/stability').then(r => r.json()).then(d => {
                if (!d.ok) { note.textContent = d.error || 'not available'; box.innerHTML = ''; return; }
                note.textContent = (d.n_s / 3600).toFixed(2) + ' h of one-second data · ' + clockTransitionText(d);
                box.innerHTML = stabilitySvg(d);
            }).catch(e => { note.textContent = 'Failed: ' + e; });
        }

        function stabilitySvg(d) {
            const W = 620, H = 330, L = 72, R = 20, T = 14, B = 44;
            const pw = W - L - R, ph = H - T - B;
            const lx = v => Math.log10(v);
            const x0 = 0, x1 = Math.max(1, Math.ceil(lx(d.tau[d.tau.length - 1])));
            const vals = d.mdev.map((v, i) => v + d.err[i]).concat(d.mdev.map((v, i) => Math.max(v - d.err[i], v * 0.1)))
                               .filter(v => v > 0);
            const ylo = Math.floor(lx(Math.min(...vals)) - 0.1), yhi = Math.ceil(lx(Math.max(...vals)) + 0.05);
            const X = t => L + (lx(t) - x0) / ((x1 - x0) || 1) * pw;
            const Y = v => T + (yhi - lx(Math.max(v, Math.pow(10, ylo)))) / (yhi - ylo) * ph;
            let g = '';
            const tr = d.transition || {};
            if (tr.found) {
                g += '<rect x="' + X(tr.span_s[0]) + '" y="' + T + '" width="' + Math.max(1, X(tr.span_s[1]) - X(tr.span_s[0])) +
                     '" height="' + ph + '" fill="#4caf50" fill-opacity="0.12"/>' +
                     '<line x1="' + X(tr.tau_s) + '" x2="' + X(tr.tau_s) + '" y1="' + T + '" y2="' + (T + ph) +
                     '" stroke="#4caf50" stroke-width="1.5"/>' +
                     '<text x="' + (X(tr.tau_s) + 5) + '" y="' + (T + 14) + '" fill="#4caf50" font-size="11">transition ~' +
                     Math.round(tr.tau_s) + ' s</text>';
            }
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
            d.tau.forEach((t, i) => {
                const v = d.mdev[i], e = d.err[i];
                g += '<line x1="' + X(t) + '" x2="' + X(t) + '" y1="' + Y(v + e) + '" y2="' + Y(Math.max(v - e, v * 0.1)) +
                     '" stroke="#00d4ff" stroke-width="1"/><circle cx="' + X(t) + '" cy="' + Y(v) + '" r="2.6" fill="#00d4ff">' +
                     '<title>τ ' + t + ' s: ' + v.toExponential(2) + ' ± ' + e.toExponential(1) + '</title></circle>';
            });
            g += '<text x="' + (L + pw / 2) + '" y="' + (H - 6) + '" fill="#aaa" font-size="12" text-anchor="middle">averaging time τ (s)</text>' +
                 '<text x="14" y="' + (T + ph / 2) + '" fill="#aaa" font-size="12" text-anchor="middle" transform="rotate(-90 14 ' +
                 (T + ph / 2) + ')">modified Allan deviation</text>';
            return '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" style="display:block; max-width:' + W +
                   'px; background:#0f0f23; border:1px solid #333;">' + g + '</svg>' +
                   '<div style="color:#666; font-size:11px; max-width:620px; margin-top:4px;">The unit’s own report of its ' +
                   'oscillator-derived PPS against its GPS solution — how closely it tracks GPS, not the stability of the ' +
                   '10 MHz itself, which needs an independent reference (against a hydrogen maser a Thunderbolt reads about ' +
                   '1e-12 at 1 s, a hump to 1e-11 near its loop time constant, mid-1e-14 at a day: Van Baak, leapsecond.com). ' +
                   'Measured points only. The transition is where the curve stops falling and turns up, beyond which ' +
                   'averaging longer buys nothing; it is claimed only when the points beyond the lowest rise by more than ' +
                   'twice their error, and the shading is the range the data allow. No noise model is drawn: the unit smooths ' +
                   'what it reports, so GPS and oscillator noise cannot be separated by slope. Points reach a quarter of the ' +
                   'unbroken record, and it restarts with the scheduler - a transition at T needs roughly 20 T of record.</div>';
        }
