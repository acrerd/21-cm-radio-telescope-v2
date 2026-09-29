// The clocks: the Thunderbolt (10 MHz and PPS), this computer's NTP and the
// controller's NTP, from /api/clock. A line under the banner's times shows
// the worst of them on every tab; the Clocks card on the RF calibration tab
// shows all three, and the Thunderbolt's last six hours.

        const CLOCK_COLOURS = {ok: '#4caf50', warn: '#ff9500', stale: '#ff4444', bad: '#ff4444', absent: '#666'};
        const CLOCK_NAMES = {thunderbolt: 'Reference (Thunderbolt)', host: 'This computer', controller: 'Controller'};

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
                tbDetail = 'osc ' + s.osc_offset_ppb.toFixed(3) + ' ppb · PPS ' + s.pps_offset_ns.toFixed(1) +
                           ' ns · DAC ' + s.dac_v.toFixed(4) + ' V · ' + s.temperature_c.toFixed(1) + ' °C' +
                           (tb.satellites ? ' · ' + tb.satellites.n_sats + ' sats' : '') +
                           ' · ' + escapeClock(s.disciplining_activity_text);
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
            const series = [['osc_ppb', '10 MHz offset', 'ppb'], ['pps_ns', 'PPS offset', 'ns'],
                            ['dac_v', 'DAC', 'V'], ['temp_c', 'Temperature', '°C']];
            const t0 = h[0].t, t1 = h[h.length - 1].t || t0 + 1;
            const W = 260, H = 60;
            box.innerHTML = series.map(([key, label, unit]) => {
                const v = h.map(p => p[key]);
                const lo = Math.min(...v), hi = Math.max(...v), span = (hi - lo) || 1;
                const pts = h.map((p, i) => ((p.t - t0) / ((t1 - t0) || 1) * W).toFixed(1) + ',' +
                                            (H - (v[i] - lo) / span * H).toFixed(1)).join(' ');
                const last = v[v.length - 1];
                return '<div style="display:inline-block; margin:0 18px 10px 0;">' +
                       '<div style="color:#888; font-size:11px;">' + label + ': ' + last.toPrecision(4) + ' ' + unit +
                       ' <span style="color:#555;">(' + lo.toPrecision(3) + '…' + hi.toPrecision(3) + ')</span></div>' +
                       '<svg width="' + W + '" height="' + H + '" style="background:#0f0f23; border:1px solid #333;">' +
                       '<polyline fill="none" stroke="#00d4ff" stroke-width="1" points="' + pts + '"/></svg></div>';
            }).join('') +
            '<div style="color:#555; font-size:11px;">' + ((t1 - t0) / 3600).toFixed(1) + ' h to now</div>';
        }
