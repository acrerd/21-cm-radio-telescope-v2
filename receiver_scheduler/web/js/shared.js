// The chrome every tab sits inside: tab switching, the status bar, the clock,
// the telescope and receiver indicators, and the start/stop tones.
//
// Part of the operator page, split out of one 2144-line file on 2026-08-25.
// Loaded as a classic script: everything here shares one global scope,
// exactly as it did before the split.

        // For the one free-text field an operator types that is then put
        // into the page as HTML (the observation comment).
        function escapeHtml(s) {
            const d = document.createElement('div');
            d.textContent = String(s);
            return d.innerHTML;
        }

        function updateClock() {
            const now = new Date();
            const date = now.toLocaleDateString('en-US', {weekday:'long', year:'numeric', month:'long', day:'numeric'});
            const localTime = now.toLocaleTimeString('en-US', {hour12:false, hour:'2-digit', minute:'2-digit', second:'2-digit'});
            const utcTime = now.toISOString().substring(11, 19);
            document.getElementById('currentDate').textContent = date;
            document.getElementById('currentTime').textContent = localTime;
            document.getElementById('utcTime').textContent = utcTime;
        }

        // Redraw just after each second begins. A setInterval(…, 1000) ticks
        // at whatever phase the page loaded with, so the display changed up to
        // a second after the real second and read ~0.5 s slow on average
        // (2026-09-30). setTimeout to the next boundary also stops timer drift
        // accumulating.
        function tickClock() {
            updateClock();
            setTimeout(tickClock, 1000 - (Date.now() % 1000) + 5);
        }

        // Start-up is boot.js's alone. A second DOMContentLoaded block here,
        // left over from the split into one file per tab (2026-08-25), started
        // every poll twice - status, receiver, and the telescope poll that
        // costs the controller three requests - for every open copy of the page.

        // The form's submit listener lives in boot.js; a second copy here made
        // every Save run twice (idempotent, so harmless, but pointless).

        function localDateStr(d) {
            return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
        }

        // The running observation, matched by identity rather than by name.
        // Two entries can share a name - the simulator names them by target,
        // so a scan booked for now and one for tomorrow are both "Drift scan
        // l=184.6 b=-5.8" - and on 2026-08-26 both rows showed green. The
        // start date and time are what make an entry one booking rather than
        // another; where the running record carries them they must agree.
        function isRunning(obs) {
            if (!currentObs || currentObs.name !== obs.name) return false;
            if (currentObs.start_date && obs.start_date
                && currentObs.start_date !== obs.start_date) return false;
            if (currentObs.start_time && obs.start_time
                && currentObs.start_time !== obs.start_time) return false;
            return true;
        }

        function stopObs() {
            fetch('/api/stop', {method: 'POST'}).then(() => updateStatus());
        }

        function formatRemaining(seconds) {
            if (!seconds) return '';
            const m = Math.floor(seconds / 60);
            const s = Math.floor(seconds % 60);
            return ` (${m}m ${s}s remaining)`;
        }

        function playTone(freqs, duration) {
            if (!soundEnabled) return;
            try {
                const ctx = new (window.AudioContext || window.webkitAudioContext)();
                const stepDur = duration / freqs.length;
                freqs.forEach((freq, i) => {
                    const osc = ctx.createOscillator();
                    const gain = ctx.createGain();
                    osc.type = 'sine';
                    osc.frequency.value = freq;
                    gain.gain.value = 0.15;
                    osc.connect(gain);
                    gain.connect(ctx.destination);
                    osc.start(ctx.currentTime + i * stepDur);
                    osc.stop(ctx.currentTime + (i + 1) * stepDur);
                });
            } catch(e) {}
        }

        function playStartSound() { playTone([440, 554, 659], 0.4); }

        function playStopSound()  { playTone([659, 554, 440], 0.4); }

        // The fixed instrument (issue #27), written into any element that
        // wants to show it. Fetched once and remembered (instrumentText, in
        // state.js): it changes only when the Configuration tab saves it,
        // which clears the memory.
        function showInstrument(elementId) {
            const el = document.getElementById(elementId);
            if (!el) return;
            if (instrumentText) { el.innerHTML = instrumentText; return; }
            fetch('/api/instrument').then(r => r.json()).then(d => {
                if (!d.success) { el.textContent = d.error || 'instrument unknown'; return; }
                const f = (v, n) => Number(v).toFixed(n);
                instrumentText =
                    'LO ' + f(d.lo_mhz, 6) + ' MHz · ' + f(d.sample_rate_mhz, 1) + ' Msps · gain ' + f(d.gain_db, 0) + ' dB<br>'
                    + 'H I sub-band ' + f(d.h1_band_mhz[0], 3) + ' – ' + f(d.h1_band_mhz[1], 3) + ' MHz, '
                    + d.h1_channels + ' ch × ' + f(d.h1_channel_khz, 2) + ' kHz<br>'
                    + 'continuum ' + f(d.continuum_band_mhz[0], 3) + ' – ' + f(d.continuum_band_mhz[1], 3) + ' MHz, '
                    + d.wide_channels + ' ch × ' + f(d.wide_channel_khz, 2) + ' kHz'
                    + (d.overridden && d.overridden.length ? '<br><span style="color:#ffa502;">overridden in config: ' + d.overridden.join(', ') + '</span>' : '');
                el.innerHTML = instrumentText;
            }).catch(() => { el.textContent = 'instrument unavailable'; });
        }

        function inMinutes(ms) {
            const m = Math.max(0, Math.round(ms / 60000));
            if (m < 60) return m + ' min';
            return Math.floor(m / 60) + ' h ' + (m % 60) + ' min';
        }

        // The pulsar monitor's standing order, said before it acts: when it
        // will start, and that anything booked or started by hand comes first.
        // The window times come from /api/status (naive local ISO, which
        // Date parses as local time - the scheduler's frame).
        function pulsarNotice(data) {
            const pm = data.pulsar_monitor;
            if (!pm || !pm.enabled || !pm.window || !pm.window.opens) return '';
            const now = new Date();
            const opens = new Date(pm.window.opens);
            const hhmm = d => d.toTimeString().slice(0, 5);
            const running = data.running ? data.observation : null;
            if (running && running.pulsar_monitor) return '';
            if (running && running.sun_monitor) {
                if (pm.window.open) return ' — pulsar observation takes over now';
                if (running && data.remaining_seconds != null
                        && opens - now < data.remaining_seconds * 1000) {
                    return ' — pulsar observation takes over in ' + inMinutes(opens - now);
                }
                return '';
            }
            if (running || data.background) return '';
            if (pm.holdoff_until) {
                return ' — pulsar observation held off until ' + hhmm(new Date(pm.holdoff_until))
                    + (pm.holdoff_reason ? ' (' + pm.holdoff_reason + ')' : '');
            }
            if (pm.window.open) {
                return pm.waiting ? ' — pulsar observation waiting: ' + pm.waiting
                                  : ' — pulsar observation starting';
            }
            return ' — pulsar observation will start in ' + inMinutes(opens - now)
                + ' (' + hhmm(opens) + ') unless interrupted';
        }

        function updateStatus() {
            fetch('/api/status').then(r => r.json()).then(data => {
                const dot = document.getElementById('statusDot');
                const text = document.getElementById('statusText');
                const btn = document.getElementById('stopBtn');
                if (data.running) {
                    dot.classList.add('running');
                    const remaining = formatRemaining(data.remaining_seconds);
                    // Starting: the telescope is pointing and the receiver
                    // has not been launched yet. Stop works here too - it
                    // aborts the slew wait.
                    text.textContent = data.starting
                        ? `Starting: ${data.observation?.name || '?'} (pointing the telescope)`
                        : `Running: ${data.observation?.name || '?'}${remaining}${pulsarNotice(data)}`;
                    btn.style.display = 'inline-block';
                    if (wasRunning === false) playStartSound();
                    currentObs = data.observation;
                } else if (data.background) {
                    // A hand-started horizon/Sun scan, calibration day, RF
                    // calibration or manual receiver holds the mount and SDR
                    // but is not the scheduled observation, so it has no Stop
                    // here - it is stopped from its own tab.
                    dot.classList.add('running');
                    const b = data.background;
                    let label = b.label || 'Background task';
                    if (b.progress != null && b.total != null) label += ' ' + b.progress + '/' + b.total;
                    else if (b.progress != null) label += ' (' + b.progress + ')';
                    text.textContent = label;
                    btn.style.display = 'none';
                    currentObs = null;
                } else {
                    dot.classList.remove('running');
                    text.textContent = 'Idle' + nextObsCountdown() + pulsarNotice(data);
                    btn.style.display = 'none';
                    if (wasRunning === true) playStopSound();
                    currentObs = null;
                }
                wasRunning = data.running;
                // The list itself is re-read every 30 s (15 of these 2 s
                // polls): it used to be fetched once at load, so an entry
                // booked from another tab, the simulator or the API - or a
                // scheduler restarted with a changed file - never appeared
                // until the page was reloaded by hand (2026-08-26).
                if (++statusPolls % 15 === 0) loadSchedule();
                else renderSchedule();
            });
        }

        function updateTelescope() {
            fetch('/api/telescope').then(r => r.json()).then(data => {
                const dot = document.getElementById('telescopeDot');
                const text = document.getElementById('telescopeText');
                if (!data.configured) {
                    dot.style.background = '#666';
                    text.textContent = 'Telescope: Disabled';
                } else if (!data.connected) {
                    dot.style.background = '#ff4444';
                    text.textContent = 'Telescope: Offline';
                } else {
                    const s = data.status;
                    const t = data.tracking;
                    dot.style.background = '#00ff88';
                    let info = `Alt ${s.alt.toFixed(1)}° Az ${s.az.toFixed(1)}°`;
                    // The controller's tracking flag is set the moment a track
                    // starts, so it read "Tracking" through the whole slew to
                    // the target. The Due's own state says what the mount is
                    // doing; a move of under 1 deg is a tracking step (each
                    // is a brief "Slewing"), not a slew.
                    const st = String(s.status || '');
                    const far = s.target_alt != null && s.target_az != null &&
                        (Math.abs(s.target_alt - s.alt) > 1 || Math.abs(s.target_az - s.az) > 1);
                    if (/homing/i.test(st)) {
                        info += ' [Homing]';
                    } else if (/slewing|reversing/i.test(st) && far) {
                        info += ` [Slewing to ${s.target_alt.toFixed(1)}° ${s.target_az.toFixed(1)}°]`;
                    } else if (t && t.enabled) {
                        info += ' [Tracking]';
                    }
                    text.textContent = info;
                    // An operator offset on the controller is a hand-applied
                    // patch on the pointing; it survives homings and nothing
                    // else shows it, so it is named here whenever it is set.
                    const o = data.offset;
                    if (o && (Math.abs(o.alt) >= 0.005 || Math.abs(o.az) >= 0.005)) {
                        dot.style.background = '#ffa502';
                        const off = document.createElement('span');
                        off.style.color = '#ffa502';
                        off.textContent = ` \u26a0 offset alt ${o.alt >= 0 ? '+' : ''}${o.alt.toFixed(2)}° az ${o.az >= 0 ? '+' : ''}${o.az.toFixed(2)}°`;
                        text.appendChild(off);
                    }
                    // A suspect homing - re-approach a pulse off the zero, or a
                    // false stall - is shown until the next homing replaces it,
                    // because the pointing model is only as good as that zero.
                    if (data.homing && data.homing.level === 'warn') {
                        dot.style.background = '#ffa502';
                        const warn = document.createElement('span');
                        warn.style.color = '#ffa502';
                        warn.textContent = ' \u26a0 ' + data.homing.summary;
                        text.appendChild(warn);
                    } else if (data.homing && data.homing.level === 'pending') {
                        const note = document.createElement('span');
                        note.style.color = '#888';
                        note.textContent = ' \u00b7 ' + data.homing.summary;
                        text.appendChild(note);
                    }
                    text.title = data.homing ? data.homing.summary : '';
                }
            }).catch(() => {
                const dot = document.getElementById('telescopeDot');
                const text = document.getElementById('telescopeText');
                dot.style.background = '#666';
                text.textContent = 'Telescope: --';
            });
        }

        function setReceiverUi(data) {
            const dot = document.getElementById('receiverDot');
            const text = document.getElementById('receiverText');
            const btn = document.getElementById('receiverBootBtn');
            if (!dot || !text || !btn) return;
            if (data.running) {
                dot.classList.add('running');
                dot.style.background = '';
                // Short: the observation's name is already in the status
                // item beside it, so it goes in the tooltip with the PID.
                const owned = data.source === 'observation';
                text.textContent = owned ? 'Receiver: recording' : 'Receiver: GUI';
                const obs = data.observation ? ` (${data.observation})` : '';
                text.title = (owned ? 'Observation' : 'Receiver GUI started by hand')
                           + obs + (data.pid ? ', PID ' + data.pid : '');
                btn.disabled = true;
                btn.title = owned
                    ? 'An observation is using the B200.'
                    : 'The receiver GUI is already running on the console.';
            } else {
                dot.classList.remove('running');
                dot.style.background = data.returncode === null ? '#666' : '#ff9500';
                text.textContent = data.returncode === null ? 'Receiver: Idle' : `Receiver: Stopped (${data.returncode})`;
                text.title = '';
                btn.disabled = false;
                // Says what it is for, because the name no longer has to:
                // this is the one deliberately graphical path in a system that
                // is otherwise entirely headless.
                btn.title = 'Opens the receiver\u2019s Qt window on the observatory '
                          + 'console, for warm-up and checking the band. Not part of '
                          + 'the observing path, and it will fail over ssh - there is '
                          + 'no display. Uses ' + (data.python || 'radioconda Python') + '.';
            }
        }

        function updateReceiver() {
            fetch('/api/receiver/status').then(r => r.json()).then(setReceiverUi).catch(() => {
                setReceiverUi({running: false, returncode: null});
            });
        }

        function bootReceiver() {
            const btn = document.getElementById('receiverBootBtn');
            if (btn) btn.disabled = true;
            fetch('/api/receiver/start', {method: 'POST'})
                .then(r => r.json())
                .then(data => {
                    if (!data.success && !data.running) {
                        alert('Receiver start failed: ' + (data.error || 'Unknown error'));
                    }
                    setReceiverUi(data);
                })
                .catch(e => alert('Receiver start failed: ' + e))
                .finally(updateReceiver);
        }

        // ---- Tabs ----
        function switchTab(name) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            document.querySelector(`.tab[onclick="switchTab('${name}')"]`).classList.add('active');
            document.getElementById('tab-' + name).classList.add('active');
            if (name === 'config') loadConfig();
            if (name === 'log') loadLog();
            if (name === 'sunscan') { pollSunScan(); pollCalDay(); loadCalModel(); refreshPointingModels(); loadBeam(); }
            // Leaving the tab stops the loop; scheduleCameraRefresh cancels
            // itself whenever the camera tab is not the one on screen.
            if (name === 'horizon') { pollHorizon(); loadHorizonProfiles(); pollInterference(); loadInterferenceScans(); }
            if (name === 'observe') refreshObserveTuning();
            if (name === 'rf') {
                rfRefresh(); rfRefreshTarget(); rfShowChosen();
                rfLoadBandpassPlot(); rfLoadGainPlot();
                refreshClocks();
            }
            if (name === 'simulator') showSimulator();
            if (name === 'observe') { loadObserveParams(false); loadObserveLast(); }
            // The live flux panel polls only while its tab is open, and
            // hides itself unless the running observation is a solar track.
            if (name === 'observe') { obvLiveStart(); } else { obvLiveStop(); }
            // Always grab a fresh frame on entering the tab, not just the first
            // time - a stale image from a previous visit is misleading on a
            // safety camera. refreshCamera chains the auto-refresh itself.
            if (name === 'camera') refreshCamera();
            else scheduleCameraRefresh();
        }

        // Open the telescope controller in the right place for wherever this
        // browser is. Remotely the controller is reached through an ssh
        // port-forward at 127.0.0.1:8080; at the observatory console it is the
        // direct link http://192.168.50.120. The scheduler page is served at
        // localhost:5000 in BOTH cases, so it cannot tell which from its own URL
        // - it probes which controller address actually answers and opens that.
        function openController(ev) {
            if (ev) ev.preventDefault();
            const candidates = ['http://127.0.0.1:8080/', 'http://192.168.50.120/'];
            // Open the tab synchronously, inside the click, so the pop-up is not
            // blocked; point it once a probe answers.
            const win = window.open('', '_blank');
            const reach = base => new Promise(resolve => {
                const done = ok => { clearTimeout(timer); resolve(ok); };
                const timer = setTimeout(() => done(false), 1500);
                // no-cors: the controller's response cannot be read cross-origin,
                // but the fetch resolving at all means it answered; a network
                // error (nothing forwarded / no route) rejects.
                fetch(base + 'ping', { mode: 'no-cors', cache: 'no-store' })
                    .then(() => done(true)).catch(() => done(false));
            });
            (async () => {
                for (const base of candidates) {
                    if (await reach(base)) {
                        if (win) win.location = base; else window.open(base, '_blank');
                        return;
                    }
                }
                // Nothing answered: send it to the console address so the browser
                // shows a real error rather than a blank tab.
                const fallback = candidates[candidates.length - 1];
                if (win) win.location = fallback; else window.open(fallback, '_blank');
            })();
            return false;
        }

        // The banner used to be rendered into the page by Jinja. Fetched here
        // instead so the markup is a plain static file with no template engine
        // between it and the browser - which is what removes the whole class of
        // bug where Python consumed a backslash escape before JavaScript ever
        // saw it. Costs a brief flash of the default title on load.
        function loadBanner() {
            fetch('/api/config').then(r => r.json()).then(cfg => {
                if (cfg.banner_name) {
                    document.title = cfg.banner_name;
                    document.getElementById('bannerName').textContent = cfg.banner_name;
                }
                // A subtitle typed on the Configuration tab wins; blank (or the
                // retired "Hydrogen Line" default) shows the telescope itself,
                // from the calibrations in force, so it follows a re-measurement.
                const custom = (cfg.banner_subtitle || '').trim();
                if (custom && custom !== 'Hydrogen Line (21cm) Observation Manager') {
                    document.getElementById('bannerSubtitle').textContent = custom;
                } else {
                    loadBannerDetails();
                }
            }).catch(() => {});
        }

        function loadBannerDetails() {
            fetch('/api/rf/status').then(r => r.json()).then(d => {
                const el = document.getElementById('bannerSubtitle');
                const beam = d.beam || {}, gain = d.gain || {};
                const parts = ['3 m dish', '1.4 GHz'];
                const tips = [];
                if (beam.fwhm_deg) {
                    parts.push('beam ' + Number(beam.fwhm_deg).toFixed(2) + '\u00b0 FWHM' +
                               (beam.solid_angle_sq_deg ? ' (' + Number(beam.solid_angle_sq_deg).toFixed(1) + ' deg\u00b2)' : ''));
                    tips.push('beam: main-lobe solid angle from the Sun drift of ' + (beam.measured_utc || '').slice(0, 10));
                }
                if (gain.t_sys_k) {
                    parts.push('T_sys ' + Math.round(gain.t_sys_k) + ' K');
                    tips.push('T_sys: from the gain fit of ' + (gain.created_utc || '').slice(0, 10) +
                              ' (drifts a few K an hour)');
                }
                // SEFD = 2 k T_sys / A_e, with A_e = lambda^2 / Omega from the
                // measured main lobe - the effective area the flux scale uses. The
                // main lobe is not the whole pattern, so this A_e is an upper bound
                // and the SEFD a lower one.
                if (gain.t_sys_k && beam.solid_angle_sq_deg) {
                    const lambda = 299792458 / 1420.405751e6;
                    const omega = beam.solid_angle_sq_deg * Math.pow(Math.PI / 180, 2);
                    const ae = lambda * lambda / omega;
                    const sefd = 2 * 1.380649e-23 * gain.t_sys_k / ae / 1e-26;
                    parts.push('SEFD ' + (sefd / 1000).toFixed(0) + ' kJy');
                    tips.push('SEFD = 2k T_sys / A_e, A_e = \u03bb\u00b2/\u03a9 = ' + ae.toFixed(2) +
                              ' m\u00b2 from the main lobe at 1420 MHz (an upper bound on A_e, so a lower bound on the SEFD)');
                }
                parts.push('55.90\u00b0 N, 4.31\u00b0 W');
                el.textContent = parts.join('  \u00b7  ');
                el.title = tips.join('\n') || '';
            }).catch(() => {});
        }
        loadBanner();
