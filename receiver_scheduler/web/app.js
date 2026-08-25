
        let schedule = [];
        let currentObs = null;

        const COORD_CONFIG = {
            altaz: {
                c1: 'Altitude', c2: 'Azimuth', u1: 'deg',
                c1_min: 0, c1_max: 90,      // Alt: 0 to 90 (above horizon)
                c2_min: 0, c2_max: 359      // Az: 0 to 359
            },
            radec: {
                c1: 'Right Ascension', c2: 'Declination', u1: 'h',
                c1_min: 0, c1_max: 23,      // RA: 0h to 23h (+ min/sec)
                c2_min: -90, c2_max: 90     // Dec: -90 to +90
            },
            galactic: {
                c1: 'Galactic Longitude (l)', c2: 'Galactic Latitude (b)', u1: 'deg',
                c1_min: 0, c1_max: 359,     // l: 0 to 359
                c2_min: -90, c2_max: 90     // b: -90 to +90
            }
        };

        const DEFAULTS = {
            name: "New Observation",
            coord_system: "altaz",
            coord1_deg: 45, coord1_min: 0, coord1_sec: 0,
            coord2_deg: 180, coord2_min: 0, coord2_sec: 0,
            start_date: "", start_time: "12:00",
            duration_minutes: 30,
            center_freq_mhz: 1420.405752,
            bandwidth_mhz: 2.4,
            gain_db: 40,
            channels: 4096,
            integration_time_s: 3.0,
            filename: "",
            sdr_type: "b210",
            calibrator: false,
            end_action: "none",
            enabled: true,
            drift_frame: "radec",
            drift_time: "12:00",
            drift_window_min: 30
        };

        function updateClock() {
            const now = new Date();
            const date = now.toLocaleDateString('en-US', {weekday:'long', year:'numeric', month:'long', day:'numeric'});
            const localTime = now.toLocaleTimeString('en-US', {hour12:false, hour:'2-digit', minute:'2-digit', second:'2-digit'});
            const utcTime = now.toISOString().substring(11, 19);
            document.getElementById('currentDate').textContent = date;
            document.getElementById('currentTime').textContent = localTime;
            document.getElementById('utcTime').textContent = utcTime;
        }

        document.addEventListener('DOMContentLoaded', () => {
            updateClock();
            setInterval(updateClock, 1000);
            loadSchedule();
            updateStatus();
            updateTelescope();
            updateReceiver();
            setInterval(updateStatus, 2000);
            setInterval(updateTelescope, 5000);
            setInterval(updateReceiver, 3000);
            fetch('/api/config').then(r => r.json()).then(cfg => {
                soundEnabled = cfg.sound_enabled !== false;
            });
        });

        document.getElementById('obsForm').addEventListener('submit', e => {
            e.preventDefault();
            saveObservation();
        });

        function updateEndTime() {
            const date = document.getElementById('obsStartDate').value || localDateStr(new Date());
            const time = document.getElementById('obsStartTime').value;
            const dur = parseInt(document.getElementById('obsDuration').value) || 0;
            if (!time) return;
            const start = new Date(`${date}T${time}`);
            const end = new Date(start.getTime() + dur * 60000);
            const hh = String(end.getHours()).padStart(2,'0');
            const mm = String(end.getMinutes()).padStart(2,'0');
            document.getElementById('obsEndTime').value = `${hh}:${mm}`;
            checkClash();
        }

        function getObsInterval(obs) {
            const date = obs.start_date || localDateStr(new Date());
            const start = new Date(`${date}T${obs.start_time}`);
            const end = new Date(start.getTime() + (obs.duration_minutes || 0) * 60000);
            return {start, end};
        }

        function checkClash() {
            const editIdx = parseInt(document.getElementById('obsIndex').value);
            const date = document.getElementById('obsStartDate').value || localDateStr(new Date());
            const time = document.getElementById('obsStartTime').value;
            const dur = parseInt(document.getElementById('obsDuration').value) || 0;
            if (!time) return;
            const newStart = new Date(`${date}T${time}`);
            const newEnd = new Date(newStart.getTime() + dur * 60000);
            const warn = document.getElementById('clashWarning');
            const clashes = [];
            schedule.forEach((obs, i) => {
                if (i === editIdx || !obs.enabled || !obs.start_time) return;
                const {start, end} = getObsInterval(obs);
                if (newStart < end && newEnd > start) {
                    clashes.push(obs.name);
                }
            });
            if (clashes.length > 0) {
                warn.textContent = 'Clashes with: ' + clashes.join(', ');
                warn.style.display = 'block';
            } else {
                warn.style.display = 'none';
            }
            return clashes.length > 0;
        }

        function updateCoordLabels() {
            const sys = document.getElementById('obsCoordSystem').value;
            const isObject = sys === 'object';
            const isSat = sys === 'satellite';
            const isCal = sys === 'calibration';
            const isDrift = sys === 'drift';
            const isHorizon = sys === 'horizon';
            document.getElementById('objectSelector').style.display = isObject ? '' : 'none';
            document.getElementById('satelliteInput').style.display = isSat ? '' : 'none';
            document.getElementById('calibrationInput').style.display = isCal ? '' : 'none';
            document.getElementById('horizonInput').style.display = isHorizon ? '' : 'none';
            document.getElementById('driftInput').style.display = isDrift ? '' : 'none';
            // A horizon scan has no target: it goes to every azimuth in turn.
            document.getElementById('coordInputs').style.display =
                (isObject || isSat || isCal || isHorizon) ? 'none' : '';
            // Drift scans derive start time and duration from T and the window
            document.getElementById('obsStartTime').disabled = isDrift;
            document.getElementById('obsDuration').disabled = isDrift;
            if (isObject || isSat || isCal || isHorizon) return;
            if (isDrift) updateDriftDerived();
            const cfg = COORD_CONFIG[isDrift ? document.getElementById('obsDriftFrame').value : sys];
            document.getElementById('coord1Label').textContent = cfg.c1;
            document.getElementById('coord2Label').textContent = cfg.c2;
            document.getElementById('coord1Unit1').textContent = cfg.u1;
            // Set min/max limits
            const c1 = document.getElementById('coord1Deg');
            const c2 = document.getElementById('coord2Deg');
            c1.min = cfg.c1_min; c1.max = cfg.c1_max;
            c2.min = cfg.c2_min; c2.max = cfg.c2_max;
            // Clamp current values to valid range
            c1.value = Math.max(cfg.c1_min, Math.min(cfg.c1_max, c1.value));
            c2.value = Math.max(cfg.c2_min, Math.min(cfg.c2_max, c2.value));
        }

        function onCoordChange() {
            if (document.getElementById('obsCoordSystem').value === 'drift') {
                updateDriftDerived();
            }
        }

        function dmsToDecimalJs(deg, min, sec) {
            const d = parseInt(deg) || 0, m = parseInt(min) || 0, s = parseFloat(sec) || 0;
            const sign = d < 0 ? -1 : 1;
            return sign * (Math.abs(d) + m / 60 + s / 3600);
        }

        function localDateStr(d) {
            return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
        }

        function driftBeamDate() {
            // Date of the beam-crossing time T: the entry's date field (or today)
            const date = document.getElementById('obsStartDate').value || localDateStr(new Date());
            const t = document.getElementById('obsDriftTime').value;
            return t ? new Date(`${date}T${t}`) : null;
        }

        function updateDriftDerived() {
            const Tdt = driftBeamDate();
            if (!Tdt) return;
            const w = parseInt(document.getElementById('obsDriftWindow').value) || 30;
            const startDt = new Date(Tdt.getTime() - w * 60000);
            document.getElementById('obsStartTime').value =
                String(startDt.getHours()).padStart(2,'0') + ':' + String(startDt.getMinutes()).padStart(2,'0');
            document.getElementById('obsDuration').value = 2 * w;
            updateEndTime();
            fetchDriftPreview();
        }

        let driftNextTransit = null;
        function fetchDriftPreview() {
            const Tdt = driftBeamDate();
            if (!Tdt) return;
            const frame = document.getElementById('obsDriftFrame').value;
            const c1 = dmsToDecimalJs(document.getElementById('coord1Deg').value,
                                      document.getElementById('coord1Min').value,
                                      document.getElementById('coord1Sec').value);
            const c2 = dmsToDecimalJs(document.getElementById('coord2Deg').value,
                                      document.getElementById('coord2Min').value,
                                      document.getElementById('coord2Sec').value);
            const params = new URLSearchParams({
                frame: frame, coord1: c1, coord2: c2,
                date: localDateStr(Tdt),
                time: document.getElementById('obsDriftTime').value
            });
            fetch('/api/drift_preview?' + params).then(r => r.json()).then(data => {
                const el = document.getElementById('driftPreview');
                if (!data.success) {
                    el.textContent = 'Preview unavailable: ' + (data.error || 'unknown error');
                    el.style.color = '#ff4757';
                    driftNextTransit = null;
                    return;
                }
                driftNextTransit = {date: data.next_transit_date, time: data.next_transit_time};
                let text = `At T: Alt ${data.alt.toFixed(1)}°, Az ${data.az.toFixed(1)}°`;
                if (data.warnings.length) text += ' — ' + data.warnings.join('; ');
                text += ` | next transit ${data.next_transit_date} ${data.next_transit_time}`;
                el.textContent = text;
                el.style.color = data.reachable ? (data.warnings.length ? '#ffa502' : '#2ed573') : '#ff4757';
            }).catch(() => {
                document.getElementById('driftPreview').textContent = 'Preview unavailable';
                driftNextTransit = null;
            });
        }

        function useNextTransit() {
            if (!driftNextTransit) { fetchDriftPreview(); return; }
            document.getElementById('obsStartDate').value = driftNextTransit.date;
            document.getElementById('obsDriftTime').value = driftNextTransit.time;
            updateDriftDerived();
        }

        function formatCoord(deg, min, sec, isRA) {
            const d = parseInt(deg) || 0;
            const m = parseInt(min) || 0;
            const s = parseFloat(sec) || 0;
            if (isRA) {
                return `${d}h ${m}m ${s.toFixed(1)}s`;
            }
            const sign = d < 0 ? '-' : '+';
            return `${sign}${Math.abs(d)}° ${m}' ${s.toFixed(1)}"`;
        }

        function formatCoordDisplay(obs) {
            const sys = obs.coord_system || 'altaz';
            if (sys === 'object') {
                const name = obs.object_name || 'unknown';
                return `Object: ${name.charAt(0).toUpperCase() + name.slice(1)}`;
            }
            if (sys === 'satellite') {
                const tle = obs.tle_text || '';
                const name = tle.split('\\n')[0] || 'Satellite';
                return `Sat: ${name.substring(0, 20)}`;
            }
            if (sys === 'calibration') {
                const n = obs.cal_grid_n || 5;
                const interval = obs.cal_interval_min || 30;
                return `Cal: ${n}x${n} every ${interval}min`;
            }
            if (sys === 'drift') {
                const isRA = (obs.drift_frame || 'radec') === 'radec';
                const c1 = formatCoord(obs.coord1_deg, obs.coord1_min, obs.coord1_sec, isRA);
                const c2 = formatCoord(obs.coord2_deg, obs.coord2_min, obs.coord2_sec, false);
                return `Drift ${isRA ? 'RA/Dec' : 'Gal'}: ${c1}, ${c2} @ ${obs.drift_time} ±${obs.drift_window_min}min`;
            }
            const isRA = sys === 'radec';
            const c1 = formatCoord(obs.coord1_deg, obs.coord1_min, obs.coord1_sec, isRA);
            const c2 = formatCoord(obs.coord2_deg, obs.coord2_min, obs.coord2_sec, false);
            const labels = {altaz: 'Alt/Az', radec: 'RA/Dec', galactic: 'Gal'};
            return `${labels[sys]}: ${c1}, ${c2}`;
        }

        function loadSchedule() {
            fetch('/api/schedule').then(r => r.json()).then(data => {
                schedule = data;
                renderSchedule();
            });
        }

        function saveSchedule() {
            // The server rejects clashing schedules with a 400 and a reason.
            // Announcing success regardless meant edits silently vanished on
            // the next reload, with nothing on screen to explain it.
            postSchedule().then(r => {
                if (r.ok) { alert('Schedule saved!'); }
                else { alert('Schedule NOT saved: ' + r.error); }
            });
        }

        // Single place that POSTs the schedule and reports what the server said.
        function postSchedule() {
            return fetch('/api/schedule', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(schedule)
            }).then(resp => resp.json().catch(() => ({}))
                .then(d => {
                    // The server trims each window to the part where the target
                    // clears the measured horizon, so the times it stored may
                    // not be the ones just sent. Say so, and reload the list so
                    // what is on screen is what will run.
                    if (d.horizon_notes && d.horizon_notes.length) {
                        alert('Local horizon: ' + d.horizon_notes.join(' | '));
                    }
                    return {ok: resp.ok && d.success !== false,
                            error: d.error || ('HTTP ' + resp.status)};
                }))
              .catch(e => ({ok: false, error: String(e)}));
        }

        function formatEndTime(obs) {
            // For the currently running observation, show the actual end time from the server
            if (currentObs && currentObs.name === obs.name && currentObs.ends_at) {
                const end = new Date(currentObs.ends_at);
                const hh = String(end.getHours()).padStart(2,'0');
                const mm = String(end.getMinutes()).padStart(2,'0');
                return hh + ':' + mm + ' (live)';
            }
            if (obs.end_time) {
                return (obs.end_date !== obs.start_date ? obs.end_date + ' ' : '') + obs.end_time;
            }
            return obs.duration_minutes + ' min';
        }

        // End of an entry's one and only slot, or null if it does not have one.
        //
        // This mirrors scheduler_thread() rather than reading the End column,
        // so what the list greys out is exactly what the background thread
        // would refuse to start. Two details have to be copied:
        //
        //   - the window is start_time + duration_minutes. end_date/end_time
        //     are display fields; the scheduler never reads them, and nothing
        //     keeps the two in step if someone edits one.
        //   - a dateless entry is not a past entry. The scheduler fills in
        //     today's date on every pass, so it comes round again every day
        //     and must never be greyed out. (find_clashes() makes the same
        //     distinction, for the same reason.)
        function obsSlotEnd(obs) {
            if (!obs.start_date || !obs.start_time) return null;
            const start = new Date(`${obs.start_date}T${obs.start_time}`);
            if (isNaN(start)) return null;
            return new Date(start.getTime() + (obs.duration_minutes || 0) * 60000);
        }

        function isExpired(obs) {
            const end = obsSlotEnd(obs);
            // The 60 s is scheduler_thread()'s own cutoff: it needs more than a
            // minute left in the window before it will take a slot, so the last
            // minute is already dead time and is shown as such.
            return end !== null && end.getTime() - Date.now() <= 60000;
        }

        // Why this entry can never run, or null if it still can. Expiry is the
        // ordinary case. The other two are only reachable through a hand-edited
        // or imported schedule.json - the Add form requires a start time - and
        // there they are invisible faults: scheduler_thread() skips the entry
        // outright while it sits in the list looking perfectly normal. Named
        // rather than lumped in with "Expired", because "its time has passed"
        // and "this entry is malformed" want different fixes.
        function neverRunsReason(obs) {
            if (!obs.start_time) return 'No start time';
            if (obs.start_date && obsSlotEnd(obs) === null) return 'Bad date';
            // Marked by the server when the whole window is behind the measured
            // horizon. The scheduler honours the mark and skips it, so saying
            // "won't run" here is a statement of fact, not a prediction.
            if (obs.horizon_blocked && obs.respect_local_horizon !== false) {
                return 'Behind the horizon';
            }
            return isExpired(obs) ? 'Expired' : null;
        }

        function renderSchedule() {
            const list = document.getElementById('scheduleList');
            if (schedule.length === 0) {
                list.innerHTML = '<div class="empty-state">No observations scheduled.</div>';
                return;
            }
            list.innerHTML = schedule.map((obs, i) => {
              const dead = neverRunsReason(obs);
              return `
                <div class="schedule-item ${obs.enabled ? '' : 'disabled'} ${dead ? 'wont-run' : ''} ${currentObs?.name === obs.name ? 'current-obs' : ''}">
                    <input autocomplete="off" type="checkbox" class="checkbox" ${obs.enabled ? 'checked' : ''} onchange="toggleEnabled(${i})">
                    <div class="schedule-info">
                        <div class="field"><div class="field-label">Name</div><div class="field-value">${obs.name}${dead ? '<span class="tag-wont-run">' + dead + '</span>' : ''}</div></div>
                        <div class="field"><div class="field-label">Start</div><div class="field-value">${obs.start_date || 'Today'} ${obs.start_time}</div></div>
                        <div class="field"><div class="field-label">End</div><div class="field-value">${formatEndTime(obs)}</div></div>
                        ${obs.horizon_note && !obs.horizon_blocked ? '<div class="field"><div class="field-label">Local horizon</div><div class="field-value" style="color:#ffa502;">' + obs.horizon_note + '</div></div>' : ''}
                        <div class="field"><div class="field-label">Coordinates</div><div class="field-value">${formatCoordDisplay(obs)}</div></div>
                        <div class="field"><div class="field-label">Frequency</div><div class="field-value">${obs.center_freq_mhz} MHz</div></div>
                        <div class="field"><div class="field-label">BW / Gain</div><div class="field-value">${obs.bandwidth_mhz} MHz / ${obs.gain_db} dB</div></div>
                        <div class="field"><div class="field-label">Cal / End</div><div class="field-value">${obs.calibrator ? 'CAL' : '-'} / ${({home:'Home',stow:'Stow'})[obs.end_action] || '-'}</div></div>
                        <div class="field"><div class="field-label">Channels</div><div class="field-value">${obs.channels}</div></div>
                        <div class="field"><div class="field-label">Integration</div><div class="field-value">${obs.integration_time_s}s</div></div>
                    </div>
                    <div class="schedule-actions">
                        <button class="btn btn-success btn-icon" onclick="runNow(${i})" title="Run Now">▶</button>
                        <button class="btn btn-secondary btn-icon" onclick="cloneObs(${i})" title="Clone">⧉</button>
                        <button class="btn btn-secondary btn-icon" onclick="editObs(${i})" title="Edit">✎</button>
                        <button class="btn btn-danger btn-icon" onclick="deleteObs(${i})" title="Delete">✕</button>
                    </div>
                </div>
              `;
            }).join('');
        }

        function openAddModal() {
            document.getElementById('modalTitle').textContent = 'Add Observation';
            document.getElementById('obsIndex').value = -1;
            fillForm(DEFAULTS);
            document.getElementById('obsModal').classList.add('active');
        }

        function isRunning(obs) {
            return currentObs && currentObs.name === obs.name;
        }

        function editObs(i) {
            if (isRunning(schedule[i])) {
                alert('Cannot edit while running. Stop the observation first.');
                return;
            }
            document.getElementById('modalTitle').textContent = 'Edit Observation';
            document.getElementById('obsIndex').value = i;
            fillForm(schedule[i]);
            document.getElementById('obsModal').classList.add('active');
        }

        function cloneObs(i) {
            document.getElementById('modalTitle').textContent = 'Clone Observation';
            document.getElementById('obsIndex').value = -1;
            const clone = Object.assign({}, schedule[i]);
            clone.name = clone.name + ' (copy)';
            clone.start_date = '';
            clone.start_time = '';
            clone.end_date = '';
            clone.end_time = '';
            clone.filename = '';
            fillForm(clone);
            document.getElementById('obsModal').classList.add('active');
        }

        function fillForm(obs) {
            document.getElementById('obsName').value = obs.name || DEFAULTS.name;
            document.getElementById('obsCoordSystem').value = obs.coord_system || DEFAULTS.coord_system;
            document.getElementById('obsObjectName').value = obs.object_name || 'sun';
            document.getElementById('obsTleText').value = obs.tle_text || '';
            document.getElementById('coord1Deg').value = obs.coord1_deg ?? DEFAULTS.coord1_deg;
            document.getElementById('coord1Min').value = obs.coord1_min ?? DEFAULTS.coord1_min;
            document.getElementById('coord1Sec').value = obs.coord1_sec ?? DEFAULTS.coord1_sec;
            document.getElementById('coord2Deg').value = obs.coord2_deg ?? DEFAULTS.coord2_deg;
            document.getElementById('coord2Min').value = obs.coord2_min ?? DEFAULTS.coord2_min;
            document.getElementById('coord2Sec').value = obs.coord2_sec ?? DEFAULTS.coord2_sec;
            document.getElementById('obsStartDate').value = obs.start_date || '';
            document.getElementById('obsStartTime').value = obs.start_time || DEFAULTS.start_time;
            document.getElementById('obsDuration').value = obs.duration_minutes ?? DEFAULTS.duration_minutes;
            document.getElementById('obsCenterFreq').value = obs.center_freq_mhz ?? DEFAULTS.center_freq_mhz;
            document.getElementById('obsBandwidth').value = obs.bandwidth_mhz ?? DEFAULTS.bandwidth_mhz;
            document.getElementById('obsGain').value = obs.gain_db ?? DEFAULTS.gain_db;
            document.getElementById('obsChannels').value = obs.channels || DEFAULTS.channels;
            document.getElementById('obsIntegration').value = obs.integration_time_s ?? DEFAULTS.integration_time_s;
            document.getElementById('obsSdrType').value = obs.sdr_type || DEFAULTS.sdr_type;
            document.getElementById('obsCalibrator').value = obs.calibrator ? 'on' : 'off';
            document.getElementById('obsEndAction').value = obs.end_action || 'none';
            // Entries saved before this field existed have it undefined, and
            // default to on - safe because the check only ever warns.
            document.getElementById('obsRespectHorizon').checked =
                obs.respect_local_horizon !== false;
            document.getElementById('obsFilename').value = obs.filename || '';
            document.getElementById('obsCalGridN').value = obs.cal_grid_n || 5;
            document.getElementById('obsCalSpacing').value = obs.cal_spacing_deg || 1.5;
            document.getElementById('obsCalInterval').value = obs.cal_interval_min || 30;
            document.getElementById('obsHorizonAzStep').value = obs.horizon_az_step || 5;
            document.getElementById('obsHorizonAltStep').value = obs.horizon_alt_step || 5;
            document.getElementById('obsHorizonAzStart').value = obs.horizon_az_start ?? 5;
            document.getElementById('obsHorizonAzEnd').value = obs.horizon_az_end ?? 350;
            document.getElementById('obsDriftFrame').value = obs.drift_frame || DEFAULTS.drift_frame;
            document.getElementById('obsDriftTime').value = obs.drift_time || DEFAULTS.drift_time;
            document.getElementById('obsDriftWindow').value = obs.drift_window_min ?? DEFAULTS.drift_window_min;
            updateCoordLabels();
            updateEndTime();
        }

        function closeModal() {
            document.getElementById('obsModal').classList.remove('active');
        }

        function autoSave() {
            // Auto-save schedule to server whenever changes are made
            postSchedule().then(r => {
                const el = document.getElementById('autoSaveWarning');
                if (r.ok) {
                    console.log('Schedule auto-saved');
                    if (el) { el.style.display = 'none'; }
                    return;
                }
                // Auto-save is silent when it works, but must not be silent
                // when it fails - this is the path that loses edits.
                console.warn('Auto-save failed:', r.error);
                if (el) { el.textContent = 'Not saved: ' + r.error; el.style.display = 'block'; }
            });
        }

        function saveObservation() {
            if (checkClash()) {
                alert('Cannot save: this observation clashes with another scheduled observation.');
                return;
            }
            const i = parseInt(document.getElementById('obsIndex').value);
            const isDrift = document.getElementById('obsCoordSystem').value === 'drift';
            let startDate = document.getElementById('obsStartDate').value || localDateStr(new Date());
            let startTime = document.getElementById('obsStartTime').value;
            let duration = parseInt(document.getElementById('obsDuration').value);
            if (isDrift) {
                // The date field holds the date of T; a scan whose window opens
                // before midnight starts on the previous day.
                const w = parseInt(document.getElementById('obsDriftWindow').value) || 30;
                const Tdt = new Date(`${startDate}T${document.getElementById('obsDriftTime').value}`);
                const driftStart = new Date(Tdt.getTime() - w * 60000);
                startDate = localDateStr(driftStart);
                startTime = String(driftStart.getHours()).padStart(2,'0') + ':' + String(driftStart.getMinutes()).padStart(2,'0');
                duration = 2 * w;
            }
            const startDt = new Date(`${startDate}T${startTime}`);
            const endDt = new Date(startDt.getTime() + duration * 60000);
            const endDate = localDateStr(endDt);
            const endTime = String(endDt.getHours()).padStart(2,'0') + ':' + String(endDt.getMinutes()).padStart(2,'0');
            const obs = {
                name: document.getElementById('obsName').value,
                coord_system: document.getElementById('obsCoordSystem').value,
                object_name: document.getElementById('obsObjectName').value,
                tle_text: document.getElementById('obsTleText').value,
                coord1_deg: parseInt(document.getElementById('coord1Deg').value) || 0,
                coord1_min: parseInt(document.getElementById('coord1Min').value) || 0,
                coord1_sec: parseFloat(document.getElementById('coord1Sec').value) || 0,
                coord2_deg: parseInt(document.getElementById('coord2Deg').value) || 0,
                coord2_min: parseInt(document.getElementById('coord2Min').value) || 0,
                coord2_sec: parseFloat(document.getElementById('coord2Sec').value) || 0,
                start_date: startDate,
                start_time: startTime,
                end_date: endDate,
                end_time: endTime,
                duration_minutes: duration,
                center_freq_mhz: parseFloat(document.getElementById('obsCenterFreq').value),
                bandwidth_mhz: parseFloat(document.getElementById('obsBandwidth').value),
                gain_db: parseFloat(document.getElementById('obsGain').value),
                channels: parseInt(document.getElementById('obsChannels').value),
                integration_time_s: parseFloat(document.getElementById('obsIntegration').value),
                sdr_type: document.getElementById('obsSdrType').value,
                calibrator: document.getElementById('obsCalibrator').value === 'on',
                end_action: document.getElementById('obsEndAction').value,
                respect_local_horizon:
                    document.getElementById('obsRespectHorizon').checked,
                filename: document.getElementById('obsFilename').value,
                cal_grid_n: parseInt(document.getElementById('obsCalGridN').value) || 5,
                cal_spacing_deg: parseFloat(document.getElementById('obsCalSpacing').value) || 1.5,
                cal_interval_min: parseInt(document.getElementById('obsCalInterval').value) || 30,
                horizon_az_step: parseFloat(document.getElementById('obsHorizonAzStep').value) || 5,
                horizon_alt_step: parseFloat(document.getElementById('obsHorizonAltStep').value) || 5,
                horizon_az_start: parseFloat(document.getElementById('obsHorizonAzStart').value) || 5,
                horizon_az_end: parseFloat(document.getElementById('obsHorizonAzEnd').value) || 350,
                drift_frame: document.getElementById('obsDriftFrame').value,
                drift_time: document.getElementById('obsDriftTime').value,
                drift_window_min: parseInt(document.getElementById('obsDriftWindow').value) || 30,
                enabled: i >= 0 ? schedule[i].enabled : true
            };
            if (i >= 0) { schedule[i] = obs; } else { schedule.push(obs); }
            closeModal();
            renderSchedule();
            autoSave();
        }

        function deleteObs(i) {
            if (isRunning(schedule[i])) {
                alert('Cannot delete while running. Stop the observation first.');
                return;
            }
            if (confirm('Delete this observation?')) {
                schedule.splice(i, 1);
                renderSchedule();
                autoSave();
            }
        }

        function toggleEnabled(i) {
            if (isRunning(schedule[i])) {
                alert('Cannot disable while running. Stop the observation first.');
                return;
            }
            schedule[i].enabled = !schedule[i].enabled;
            renderSchedule();
            autoSave();
        }

        function runNow(i) {
            fetch('/api/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(schedule[i])
            }).then(r => r.json()).then(data => {
                if (data.success) updateStatus();
                else alert('Failed: ' + (data.error || 'Unknown'));
            });
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

        let wasRunning = null;
        let soundEnabled = true;

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

        function nextObsCountdown() {
            const now = new Date();
            let nearest = null;
            let nearestName = '';
            schedule.forEach(obs => {
                if (!obs.enabled || !obs.start_time) return;
                const date = obs.start_date || localDateStr(now);
                const start = new Date(`${date}T${obs.start_time}`);
                if (start > now && (!nearest || start < nearest)) {
                    nearest = start;
                    nearestName = obs.name;
                }
            });
            if (!nearest) return '';
            const diff = Math.floor((nearest - now) / 1000);
            if (diff <= 0) return '';
            const h = Math.floor(diff / 3600);
            const m = Math.floor((diff % 3600) / 60);
            const s = diff % 60;
            let t = '';
            if (h > 0) t += h + 'h ';
            t += m + 'm ' + s + 's';
            return ` \u2014 Next: ${nearestName} in ${t}`;
        }

        function updateStatus() {
            fetch('/api/status').then(r => r.json()).then(data => {
                const dot = document.getElementById('statusDot');
                const text = document.getElementById('statusText');
                const btn = document.getElementById('stopBtn');
                if (data.running) {
                    dot.classList.add('running');
                    const remaining = formatRemaining(data.remaining_seconds);
                    text.textContent = `Running: ${data.observation?.name || '?'}${remaining}`;
                    btn.style.display = 'inline-block';
                    if (wasRunning === false) playStartSound();
                    currentObs = data.observation;
                } else {
                    dot.classList.remove('running');
                    text.textContent = 'Idle' + nextObsCountdown();
                    btn.style.display = 'none';
                    if (wasRunning === true) playStopSound();
                    currentObs = null;
                }
                wasRunning = data.running;
                renderSchedule();
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
                    if (t && t.enabled) {
                        info += ' [Tracking]';
                    }
                    text.textContent = info;
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
                const label = data.source === 'observation' ? 'Observation' : 'Started';
                const obs = data.observation ? ` (${data.observation})` : '';
                text.textContent = `Receiver: ${label}${obs}${data.pid ? ' #' + data.pid : ''}`;
                btn.disabled = true;
                btn.title = data.source === 'observation'
                    ? 'A scheduled observation is using the B210 receiver.'
                    : 'The B210 receiver process is already running.';
            } else {
                dot.classList.remove('running');
                dot.style.background = data.returncode === null ? '#666' : '#ff9500';
                text.textContent = data.returncode === null ? 'Receiver: Idle' : `Receiver: Stopped (${data.returncode})`;
                btn.disabled = false;
                btn.title = `Start the B210 receiver with ${data.python || 'radioconda Python'}.`;
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

        function exportSchedule() {
            const blob = new Blob([JSON.stringify(schedule, null, 2)], {type: 'application/json'});
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'h1_schedule.json';
            a.click();
        }

        function clearPast() {
            const before = schedule.length;
            // Removes exactly the rows badged "Expired" - past entries only,
            // not the malformed ones neverRunsReason() also greys out, since
            // those want fixing rather than silently deleting. It used to
            // substitute today's date for a dateless entry, which deleted
            // recurring entries that were still going to run tomorrow.
            schedule = schedule.filter(obs => !isExpired(obs));
            const removed = before - schedule.length;
            if (removed > 0) {
                renderSchedule();
                autoSave();
            }
            alert(removed > 0 ? `Removed ${removed} past observation(s).` : 'No past observations to clear.');
        }

        let tleResultsData = [];

        function fetchTle() {
            const query = document.getElementById('tleSearch').value.trim();
            if (!query) { alert('Enter a satellite name or NORAD ID.'); return; }
            const info = document.getElementById('passInfo');
            info.textContent = 'Fetching from CelesTrak...';
            info.style.color = '#888';
            fetch('/api/fetch_tle', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({query: query})
            }).then(r => r.json()).then(data => {
                if (data.success && data.results.length > 0) {
                    tleResultsData = data.results;
                    if (data.results.length === 1) {
                        // Single result - use it directly
                        document.getElementById('obsTleText').value = data.results[0].tle;
                        document.getElementById('tleResults').style.display = 'none';
                        info.textContent = 'TLE fetched - click Compute Next Pass';
                        info.style.color = '#00ff88';
                    } else {
                        // Multiple results - show dropdown
                        const sel = document.getElementById('tleResultSelect');
                        sel.innerHTML = data.results.map((r, i) =>
                            `<option value="${i}">${r.name}</option>`
                        ).join('');
                        document.getElementById('tleResults').style.display = '';
                        selectTleResult();
                        info.textContent = data.results.length + ' satellites found - select one';
                        info.style.color = '#00d4ff';
                    }
                } else {
                    document.getElementById('tleResults').style.display = 'none';
                    info.textContent = data.error || 'Not found';
                    info.style.color = '#ff4757';
                }
            }).catch(e => {
                info.textContent = 'Error: ' + e;
                info.style.color = '#ff4757';
            });
        }

        function selectTleResult() {
            const idx = parseInt(document.getElementById('tleResultSelect').value);
            if (tleResultsData[idx]) {
                document.getElementById('obsTleText').value = tleResultsData[idx].tle;
            }
        }

        function predictPass() {
            const tle = document.getElementById('obsTleText').value.trim();
            if (!tle) { alert('Paste or load a TLE first.'); return; }
            const info = document.getElementById('passInfo');
            info.textContent = 'Computing...';
            info.style.color = '#888';
            fetch('/api/predict_pass', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({tle_text: tle})
            }).then(r => r.json()).then(data => {
                if (data.success) {
                    const p = data.pass;
                    // Auto-fill schedule fields
                    document.getElementById('obsStartDate').value = p.start_date;
                    document.getElementById('obsStartTime').value = p.start_time;
                    document.getElementById('obsDuration').value = Math.ceil(p.duration_minutes);
                    document.getElementById('obsName').value = document.getElementById('obsName').value || p.name;
                    updateEndTime();
                    info.textContent = 'Pass found!';
                    info.style.color = '#00ff88';
                    document.getElementById('passDetails').style.display = 'block';
                    document.getElementById('passDetails').innerHTML =
                        `<b>${p.name}</b><br>` +
                        `Rise: ${p.rise_time_local} (Az ${p.rise_az}\\u00b0)<br>` +
                        `Max:  ${p.max_time_utc} UTC (El ${p.max_el}\\u00b0)<br>` +
                        `Set:  ${p.set_time_local} (Az ${p.set_az}\\u00b0)<br>` +
                        `Duration: ${p.duration_minutes} min`;
                } else {
                    info.textContent = data.error || 'No pass found';
                    info.style.color = '#ff4757';
                    document.getElementById('passDetails').style.display = 'none';
                }
            }).catch(e => {
                info.textContent = 'Error: ' + e;
                info.style.color = '#ff4757';
            });
        }

        function loadTleFile(e) {
            const file = e.target.files[0];
            if (file) {
                const reader = new FileReader();
                reader.onload = ev => {
                    document.getElementById('obsTleText').value = ev.target.result.trim();
                    document.getElementById('passInfo').textContent = 'TLE loaded from file';
                    document.getElementById('passInfo').style.color = '#00d4ff';
                };
                reader.readAsText(file);
            }
            e.target.value = '';
        }

        function loadFile(e) {
            const file = e.target.files[0];
            if (file) {
                const reader = new FileReader();
                reader.onload = ev => {
                    try {
                        schedule = JSON.parse(ev.target.result);
                        renderSchedule();
                        alert('Loaded!');
                    } catch { alert('Invalid JSON'); }
                };
                reader.readAsText(file);
            }
            e.target.value = '';
        }

        // ---- Tabs ----
        function switchTab(name) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            document.querySelector(`.tab[onclick="switchTab('${name}')"]`).classList.add('active');
            document.getElementById('tab-' + name).classList.add('active');
            if (name === 'config') loadConfig();
            if (name === 'log') loadLog();
            if (name === 'sunscan') { pollSunScan(); pollCalDay(); loadCalModel(); }
            // Leaving the tab stops the loop; scheduleCameraRefresh cancels
            // itself whenever the camera tab is not the one on screen.
            if (name === 'horizon') { pollHorizon(); loadHorizonProfiles(); }
            if (name === 'observe') refreshObserveTuning();
            if (name === 'rf') {
                rfRefresh(); rfRefreshTarget(); rfShowChosen();
                rfLoadBandpassPlot(); rfLoadGainPlot();
            }
            if (name === 'simulator') showSimulator();
            if (name === 'observe') { loadObserveParams(false); loadObserveLast(); }
            if (name === 'camera' && !camObjectUrl) refreshCamera();
            else scheduleCameraRefresh();
        }

        // ---- Simulator ----
        // Built on first visit and then left alone. It fetches ~33 MB of sky
        // data and decodes it to a ~80 MB cube, so rebuilding the frame on
        // every tab switch would repeat the whole load; hiding it costs
        // nothing, since .tab-content already toggles display.
        //
        // Built here rather than in the markup for a second reason: a canvas
        // laid out inside a display:none parent sizes to zero. switchTab adds
        // .active before calling this, so by now the host is on screen.
        let simFrame = null;

        function showSimulator() {
            if (simFrame) return;
            const host = document.getElementById('simHost');
            host.innerHTML = '';
            simFrame = document.createElement('iframe');
            simFrame.src = '/simulator/';
            simFrame.style.cssText = 'width:100%; height:100%; border:0; display:block;';
            simFrame.title = 'Sky simulator';
            host.appendChild(simFrame);
        }

        // ---- Observe ----
        // Stamp of the hand-off already on the form, so opening the tab picks up
        // a Realise that happened since it was last looked at, and does not
        // overwrite edits made here in the meantime.
        let obvAppliedStamp = null;

        function onObserveModeChange() {
            const drift = document.getElementById('obvMode').value === 'drift';
            // In drift mode the duration IS the scan length, and the source
            // transits at its mid-point; saying so beats a tooltip.
            document.getElementById('obvDurationLabel').innerHTML =
                drift ? 'Scan length <span class="unit">(min)</span> &mdash; transit at mid-point'
                      : 'Total integration time <span class="unit">(min)</span>';
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
            const drift = document.getElementById('obvMode').value === 'drift';
            const num = (id, dflt) => {
                const v = parseFloat(document.getElementById(id).value);
                return Number.isFinite(v) ? v : dflt;
            };
            const l = num('obvL', 0), b = num('obvB', 0);
            const obs = Object.assign({}, DEFAULTS, {
                name: document.getElementById('obvName').value.trim() || 'Simulator target',
                coord_system: drift ? 'drift' : 'galactic',
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

        // ---- Horizon scan ----
        let hzPollTimer = null;

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


        // ---- what the receiver will actually be tuned to ----
        // The B210 is a direct-conversion receiver, so the tuned frequency
        // lands on the FFT's DC bin and UHD's automatic offset correction
        // subtracts whatever is there - including the line. The LO is
        // therefore offset, and the sample rate raised if it must be to keep
        // the line in the flat part of the band. Saying so here means the
        // numbers typed above are never silently replaced.
        let obvTuningTimer = null;

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


        // ---- RF calibration ----
        // Two measurements that own the dish for a couple of minutes each. The
        // page polls only while the tab is open and something is running: this
        // controller has known cross-task locking weaknesses (issue #1), so idle
        // tabs must not sit on it.
        let rfPollTimer = null;
        let rfTickTimer = null;
        let rfPlotIsCurrent = false;
        let rfEndsAt = null;      // ms since epoch, or null for an untimed stage
        let rfTotalS = null;

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

        // ---- Safety camera ----
        // Fetched as a blob rather than pointed at with an <img src>: it keeps
        // the previous frame on screen while the next is being taken, and a
        // failure arrives as the server's actual message instead of a broken
        // image icon.
        let camObjectUrl = null;
        let camTimer = null;

        function cameraTabVisible() {
            const tab = document.getElementById('tab-camera');
            return !document.hidden && tab && tab.classList.contains('active');
        }

        // Chained from the end of each capture rather than run on an interval:
        // at 1 s the capture takes a good fraction of the gap, and setInterval
        // would queue requests behind each other the moment one ran long.
        function scheduleCameraRefresh() {
            if (camTimer) { clearTimeout(camTimer); camTimer = null; }
            const every = parseInt(document.getElementById('camAutoRefresh').value, 10);
            if (!every || !cameraTabVisible()) return;
            camTimer = setTimeout(refreshCamera, every * 1000);
        }

        function onCameraAutoChange() {
            if (camTimer) { clearTimeout(camTimer); camTimer = null; }
            if (parseInt(document.getElementById('camAutoRefresh').value, 10)) refreshCamera();
        }

        // A hidden tab keeps its timers in some browsers and throttles them in
        // others; neither should leave the camera streaming, so pause outright
        // and pick up again when the page comes back.
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                if (camTimer) { clearTimeout(camTimer); camTimer = null; }
            } else {
                scheduleCameraRefresh();
            }
        });

        function refreshCamera() {
            const button = document.getElementById('camRefreshBtn');
            const status = document.getElementById('camStatus');
            button.disabled = true;
            status.textContent = 'Capturing…';
            fetch('/api/camera/snapshot', {cache: 'no-store'}).then(r => {
                if (!r.ok) {
                    return r.json()
                        .catch(() => ({error: 'HTTP ' + r.status}))
                        .then(d => { throw new Error(d.error || ('HTTP ' + r.status)); });
                }
                const captured = r.headers.get('X-Capture-Time');
                const frames = r.headers.get('X-Capture-Frames');
                return r.blob().then(blob => ({blob, captured, frames}));
            }).then(({blob, captured, frames}) => {
                const url = URL.createObjectURL(blob);
                document.getElementById('camView').innerHTML =
                    '<img src="' + url + '" alt="Safety camera view" ' +
                    'style="max-width:100%; border-radius:6px;">';
                // Only after the new frame is on screen, or the browser may
                // still be decoding the old one.
                if (camObjectUrl) URL.revokeObjectURL(camObjectUrl);
                camObjectUrl = url;
                const when = captured ? new Date(captured) : new Date();
                status.innerHTML = '<span style="color:#00d4ff;">Captured ' +
                    when.toLocaleTimeString() + '</span>' +
                    (frames ? '<span style="color:#888;"> &middot; ' + frames +
                              (frames === '1' ? ' frame' : ' frames') + '</span>' : '');
            }).catch(e => {
                status.innerHTML = '<span style="color:#ff4757;">' + e.message + '</span>';
            }).finally(() => {
                button.disabled = false;
                // Chained even after a failure, so a camera that comes back
                // recovers on its own rather than needing a click.
                scheduleCameraRefresh();
            });
        }

        // ---- Sun Scan ----
        let ssPollTimer = null;

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

        // ---- Calibration Day ----
        let cdPollTimer = null;
        // Number of scans on file, from /api/calday/data. Held here rather
        // than written straight into cdStatus: loadCalModel() and pollCalDay()
        // are both in flight when the tab opens, and whichever landed last
        // used to win — which is how a running calibration day could be
        // reported as "Idle".
        let cdArchiveCount = null;

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

        // ---- Configuration ----
        function loadConfig() {
            fetch('/api/config').then(r => r.json()).then(cfg => {
                document.getElementById('cfgBannerName').value = cfg.banner_name || '';
                document.getElementById('cfgBannerSubtitle').value = cfg.banner_subtitle || '';
                document.getElementById('cfgControllerUrl').value = cfg.srt_controller_url || '';
                document.getElementById('cfgSlewTimeout').value = cfg.slew_timeout || 300;
                document.getElementById('cfgPositionTolerance').value = cfg.position_tolerance || 0.5;
                document.getElementById('cfgObsLat').value = cfg.observer_lat ?? 55.9;
                document.getElementById('cfgObsLon').value = cfg.observer_lon ?? -4.3;
                document.getElementById('cfgObsElev').value = cfg.observer_elevation ?? 50;
                document.getElementById('cfgMinElev').value = cfg.min_elevation ?? 10;
                document.getElementById('cfgCameraDevice').value = cfg.camera_device || '';
                document.getElementById('cfgCameraResolution').value = cfg.camera_resolution || '';
                document.getElementById('cfgReceiverPythonPath').value = cfg.receiver_python_path || cfg.python_path || '';
                document.getElementById('cfgDataFolder').value = cfg.data_output_folder || '';
                document.getElementById('cfgLogLines').value = cfg.log_lines || 100;
                document.getElementById('cfgSoundEnabled').value = cfg.sound_enabled !== false ? 'true' : 'false';
                soundEnabled = cfg.sound_enabled !== false;
            });
        }

        function saveConfig() {
            // Refuse the whole save rather than store an empty mask: silently
            // dropping a typo here would let the next calibration day scan
            // straight through the trees.
            const cfg = {
                banner_name: document.getElementById('cfgBannerName').value,
                banner_subtitle: document.getElementById('cfgBannerSubtitle').value,
                srt_controller_url: document.getElementById('cfgControllerUrl').value,
                slew_timeout: parseInt(document.getElementById('cfgSlewTimeout').value) || 300,
                position_tolerance: parseFloat(document.getElementById('cfgPositionTolerance').value) || 0.5,
                observer_lat: parseFloat(document.getElementById('cfgObsLat').value) || 0,
                observer_lon: parseFloat(document.getElementById('cfgObsLon').value) || 0,
                observer_elevation: parseFloat(document.getElementById('cfgObsElev').value) || 0,
                min_elevation: parseFloat(document.getElementById('cfgMinElev').value) || 10,
                camera_device: document.getElementById('cfgCameraDevice').value,
                camera_resolution: document.getElementById('cfgCameraResolution').value,
                receiver_python_path: document.getElementById('cfgReceiverPythonPath').value,
                data_output_folder: document.getElementById('cfgDataFolder').value,
                log_lines: parseInt(document.getElementById('cfgLogLines').value) || 100,
                sound_enabled: document.getElementById('cfgSoundEnabled').value === 'true',
            };
            soundEnabled = cfg.sound_enabled;
            fetch('/api/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(cfg)
            }).then(r => r.json()).then(data => {
                if (data.success) {
                    const el = document.getElementById('configSaved');
                    el.style.display = 'inline';
                    setTimeout(() => el.style.display = 'none', 3000);
                    updateTelescope();
                } else {
                    alert('Error saving: ' + (data.error || 'Unknown'));
                }
            });
        }

        // ---- Log ----
        let logRefreshTimer = null;

        function loadLog() {
            fetch('/api/log').then(r => r.json()).then(data => {
                const el = document.getElementById('logContent');
                el.textContent = data.lines.join('\\n') || '(empty log)';
                el.scrollTop = el.scrollHeight;
            }).catch(() => {
                document.getElementById('logContent').textContent = 'Error loading log';
            });
        }

        function toggleLogRefresh() {
            if (document.getElementById('logAutoRefresh').checked) {
                logRefreshTimer = setInterval(loadLog, 5000);
            } else {
                clearInterval(logRefreshTimer);
                logRefreshTimer = null;
            }
        }

        // Start log auto-refresh
        logRefreshTimer = setInterval(loadLog, 5000);

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
                document.getElementById('bannerSubtitle').textContent =
                    cfg.banner_subtitle || '';
            }).catch(() => {});
        }
        loadBanner();
