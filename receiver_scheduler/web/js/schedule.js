// The Scheduler tab: the observation list, the add/edit modal, drift-scan
// derivation and the satellite TLE lookup.
//
// Part of the operator page, split out of one 2144-line file on 2026-08-25.
// Loaded as a classic script: everything here shares one global scope,
// exactly as it did before the split.

        // The simulator's target list, offered in the schedule form as
        // "Famous targets". [name, l, b, note] - galactic degrees. A copy of
        // TARGETS in astro_simulator/web/js/ui.js (which must keep working
        // with no scheduler present, so it cannot be fetched from here);
        // test_page_structure.py holds the two lists equal, which is what
        // makes the copy safe. The chosen target is saved as an ordinary
        // galactic entry - the picker is sugar, the storage is coordinates.
        const FAMOUS_TARGETS = [
            ["Galactic centre wings", 0.0, 0.0, "broad velocity wings"],
            ["Inner Galaxy (l=30)", 30.0, 0.0, "terminal velocities"],
            ["Vulpecula rift (l=60)", 60.0, 0.0, "local + Sagittarius arm"],
            ["Cygnus X (l=80)", 80.0, 0.0, "star-forming complex"],
            ["Outer Arm (l=110)", 110.0, 0.0, "distant spiral arm"],
            ["Perseus arm (l=134)", 134.0, -1.0, "double-peaked line"],
            ["Anticentre (l=180)", 180.0, 0.0, "zero-velocity direction"],
            ["Rosette (l=206)", 206.0, -2.0, "3rd quadrant plane"],
            ["Third quadrant (l=220)", 220.0, 0.0, "negative velocities"],
            ["M31 (Andromeda)", 121.17, -21.57, "H I at -300 km/s"],
            ["M33 (Triangulum)", 133.6, -31.3, "H I at -180 km/s"],
            ["LMC", 280.5, -32.9, "H I at +280 km/s"],
            ["SMC", 302.8, -44.3, "H I at +160 km/s"],
            ["HVC Complex A", 150.0, 35.0, "infalling, -180 km/s"],
            ["HVC Complex C", 100.0, 45.0, "infalling, -120 km/s"],
            ["Smith Cloud", 39.0, -13.0, "infalling, +100 km/s"],
            ["Lockman Hole", 150.0, 53.0, "minimum H I, off-position"],
            ["Celestial pole", 122.9, 27.1, "zero drift rate"],
            // The continuum sources from the simulator's menu. The fixed
            // three are their catalogue RA/Dec converted to galactic once
            // (they do not move); the guard test re-derives the conversion
            // from ephemeris.js, so a typo here fails with the source's name.
            ["Cyg A", 76.190, 5.756, "brightest extragalactic, 1590 Jy"],
            ["Cas A", 111.735, -2.130, "supernova remnant, slowly fading"],
            ["Tau A", 184.557, -5.784, "Crab nebula, 875 Jy"],
            // The Sun and Moon are ephemeris objects: the entry is saved as
            // coord_system "object" and the controller does the astronomy.
            ["Sun", "object", "sun", "tracked ephemeris"],
            ["Moon", "object", "moon", "geocentric tracking, up to 0.9° low"],
        ];

        function populateFamousTargets() {
            const sel = document.getElementById('obsFamousTarget');
            if (sel.options.length) return;       // once is enough
            FAMOUS_TARGETS.forEach(([name, l, b, note], i) => {
                const o = document.createElement('option');
                o.value = i;
                o.textContent = name + ' — ' + note;
                sel.appendChild(o);
            });
        }

        function onFamousTargetChange() {
            // Offer the target's name as the observation's, but never fight
            // a name the operator typed: only fill a box that is empty, still
            // the default, or holding another famous name.
            const nameBox = document.getElementById('obsName');
            const current = nameBox.value.trim();
            const t = FAMOUS_TARGETS[parseInt(
                document.getElementById('obsFamousTarget').value)];
            if (!t) return;
            if (!current || current === 'New Observation'
                || FAMOUS_TARGETS.some(([n]) => n === current)) {
                nameBox.value = t[0];
            }
            const modeSel = document.getElementById('obsFamousMode');
            if (t[1] === 'object') {
                // The Sun and Moon are ephemeris objects: the drift-pointing
                // machinery works in fixed frames, so they are track-only.
                modeSel.value = 'track';
                modeSel.disabled = true;
            } else {
                modeSel.disabled = false;
                // Fill the (hidden) coordinate boxes so the drift machinery -
                // the T/window derivation and the pointing preview - reads the
                // target exactly as if it had been typed.
                document.getElementById('coord1Deg').value = t[1];
                document.getElementById('coord1Min').value = 0;
                document.getElementById('coord1Sec').value = 0;
                document.getElementById('coord2Deg').value = t[2];
                document.getElementById('coord2Min').value = 0;
                document.getElementById('coord2Sec').value = 0;
                document.getElementById('obsDriftFrame').value = 'galactic';
            }
            // No updateCoordLabels() here: updateCoordLabels calls this
            // function, and calling back would recurse. The select's inline
            // handler invokes both, in order.
        }

        function famousDriftChosen() {
            const t = FAMOUS_TARGETS[parseInt(
                document.getElementById('obsFamousTarget').value)];
            return document.getElementById('obsCoordSystem').value === 'famous'
                && t && t[1] !== 'object'
                && document.getElementById('obsFamousMode').value === 'drift';
        }

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

        // Chronological, always: the server stores the schedule that way and
        // the page re-sorts whatever it holds before drawing, so a list loaded
        // from an older file or edited in place never shows out of order.
        // Undated or untimed entries go last. Sorted in place, since the row
        // buttons address entries by index into this same array.
        function sortSchedule() {
            // Two tiers: what can still run, in start order, then what has
            // expired, also in start order - the past is history, not the
            // first thing on the list. Expiry is judged now, so an entry
            // moves down the moment its slot dies, without a reload.
            const when = obs => {
                if (!obs.start_time) return Infinity;
                const t = new Date(`${obs.start_date || localDateStr(new Date())}T${obs.start_time}`).getTime();
                return Number.isFinite(t) ? t : Infinity;
            };
            const key = obs => (isExpired(obs) ? 1 : 0);
            schedule.sort((a, b) => (key(a) - key(b)) || (when(a) - when(b)));
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
            // The names, not a boolean: the save alert must say *what* it
            // clashes with. "Clashes with another observation" sent someone
            // hunting through expired calibration days when the culprit was
            // the same entry's own first, silently-successful save.
            return clashes;
        }

        function updateCoordLabels() {
            const sys = document.getElementById('obsCoordSystem').value;
            const isObject = sys === 'object';
            const isSat = sys === 'satellite';
            const isCal = sys === 'calibration';
            const isDrift = sys === 'drift';
            const isHorizon = sys === 'horizon';
            const isFamous = sys === 'famous';
            if (isFamous) { populateFamousTargets(); onFamousTargetChange(); }
            // A famous target in drift mode is an ordinary drift entry with
            // the boxes filled from the list: it needs the drift block (the
            // beam-crossing time T and window) and the same derived times.
            const isFamousDrift = isFamous && famousDriftChosen();
            document.getElementById('famousSelector').style.display = isFamous ? '' : 'none';
            document.getElementById('objectSelector').style.display = isObject ? '' : 'none';
            document.getElementById('satelliteInput').style.display = isSat ? '' : 'none';
            document.getElementById('calibrationInput').style.display = isCal ? '' : 'none';
            document.getElementById('horizonInput').style.display = isHorizon ? '' : 'none';
            document.getElementById('driftInput').style.display =
                (isDrift || isFamousDrift) ? '' : 'none';
            // A horizon scan has no target: it goes to every azimuth in turn.
            // A famous target's coordinates come from the list, not the boxes.
            document.getElementById('coordInputs').style.display =
                (isObject || isSat || isCal || isHorizon || isFamous) ? 'none' : '';
            // Drift scans derive start time and duration from T and the window
            document.getElementById('obsStartTime').disabled = isDrift || isFamousDrift;
            document.getElementById('obsDuration').disabled = isDrift || isFamousDrift;
            // The famous list is galactic; letting the frame dropdown say
            // radec would silently misread l/b as hours and degrees.
            document.getElementById('obsDriftFrame').disabled = isFamousDrift;
            if (isFamousDrift) updateDriftDerived();
            if (isObject || isSat || isCal || isHorizon || isFamous) return;
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
            const d = parseFloat(deg) || 0, m = parseInt(min) || 0, s = parseFloat(sec) || 0;
            const sign = d < 0 ? -1 : 1;
            return sign * (Math.abs(d) + m / 60 + s / 3600);
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
            // The three fields are additive - dms_to_decimal sums them as
            // given - and the degrees field may itself be fractional: a
            // famous target stores l=111.735 with min/sec zero. So sum first,
            // then decompose for display: 111.735 shows as 111° 44' 06",
            // not truncated to 111°.
            const d0 = parseFloat(deg) || 0;
            const total = (d0 < 0 ? -1 : 1)
                * (Math.abs(d0) + (parseInt(min) || 0) / 60
                   + (parseFloat(sec) || 0) / 3600);
            const sign = total < 0 ? '-' : '+';
            const a = Math.abs(total);
            let D = Math.trunc(a);
            let M = Math.trunc((a - D) * 60);
            let S = ((a - D) * 60 - M) * 60;
            // Carry a seconds value that would *display* as 60.0
            if (S >= 59.95) { S = 0; M += 1; }
            if (M >= 60) { M = 0; D += 1; }
            if (isRA) return `${sign === '-' ? '-' : ''}${D}h ${M}m ${S.toFixed(1)}s`;
            return `${sign}${D}° ${M}' ${S.toFixed(1)}"`;
        }

        function formatCoordDisplay(obs) {
            const sys = obs.coord_system || 'altaz';
            if (sys === 'object') {
                const name = obs.object_name || 'unknown';
                return `Object: ${name.charAt(0).toUpperCase() + name.slice(1)}`;
            }
            if (sys === 'satellite') {
                const tle = obs.tle_text || '';
                const name = tle.split('\n')[0] || 'Satellite';
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
                    // not be the ones just sent. Adopt what it stored - the
                    // previous version announced the trim in an alert while
                    // the list and the edit window went on showing the
                    // untrimmed times, a schedule that would never run.
                    const ok = resp.ok && d.success !== false;
                    if (ok && Array.isArray(d.schedule)) {
                        schedule = d.schedule;
                        renderSchedule();
                    }
                    if (d.horizon_notes && d.horizon_notes.length) {
                        alert('Local horizon: ' + d.horizon_notes.join(' | '));
                    }
                    return {ok: ok,
                            error: d.error || ('HTTP ' + resp.status)};
                }))
              .catch(e => ({ok: false, error: String(e)}));
        }

        function formatEndTime(obs) {
            // For the currently running observation, show the actual end time from the server
            if (isRunning(obs) && currentObs.ends_at) {
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
            sortSchedule();
            const list = document.getElementById('scheduleList');
            if (schedule.length === 0) {
                list.innerHTML = '<div class="empty-state">No observations scheduled.</div>';
                return;
            }
            list.innerHTML = schedule.map((obs, i) => {
              const dead = neverRunsReason(obs);
              const running = isRunning(obs);
              // Queued: enabled, still to come, and not the one running now.
              const queued = obs.enabled && !dead && !running;
              return `
                <div class="schedule-item ${obs.enabled ? '' : 'disabled'} ${dead ? 'wont-run' : ''} ${running ? 'current-obs' : queued ? 'queued' : ''}">
                    <input autocomplete="off" type="checkbox" class="checkbox" ${obs.enabled ? 'checked' : ''} onchange="toggleEnabled(${i})">
                    <div class="schedule-info">
                        <div class="field"><div class="field-label">Name</div><div class="field-value">${obs.name}${dead ? '<span class="tag-wont-run">' + dead + '</span>' : ''}</div></div>
                        ${obs.comment ? '<div class="field" style="grid-column:1 / -1;"><div class="field-label">Comment</div><div class="field-value" style="white-space:pre-wrap; color:#aab;">' + escapeHtml(obs.comment) + '</div></div>' : ''}
                        <div class="field"><div class="field-label">Start (local)</div><div class="field-value">${obs.start_date || 'Today'} ${obs.start_time}</div></div>
                        <div class="field"><div class="field-label">End (local)</div><div class="field-value">${formatEndTime(obs)}</div></div>
                        ${obs.horizon_note && !obs.horizon_blocked ? '<div class="field"><div class="field-label">Local horizon</div><div class="field-value" style="color:#ffa502;">' + obs.horizon_note + '</div></div>' : ''}
                        <div class="field"><div class="field-label">Coordinates</div><div class="field-value">${formatCoordDisplay(obs)}</div></div>
                        <div class="field"><div class="field-label">Frequency</div><div class="field-value">${obs.center_freq_mhz} MHz</div></div>
                        <div class="field"><div class="field-label">BW / Gain</div><div class="field-value">${obs.bandwidth_mhz} MHz / ${obs.gain_db} dB</div></div>
                        <div class="field"><div class="field-label">Cal / End</div><div class="field-value">${obs.calibrator ? 'CAL' : '-'} / ${({home:'Home',stow:'Stow'})[obs.end_action] || '-'}</div></div>
                        <div class="field"><div class="field-label">Channels</div><div class="field-value">${obs.channels}</div></div>
                        <div class="field"><div class="field-label">Integration</div><div class="field-value">${obs.integration_time_s}s</div></div>
                    </div>
                    <div class="schedule-actions">
                        <button class="btn btn-success btn-icon" onclick="runNow(${i})" title="Start this entry now, regardless of its booked time. Refused if something is already recording.">▶</button>
                        <button class="btn btn-secondary btn-icon" onclick="cloneObs(${i})" title="Open a copy of this entry with the times cleared, to book it again.">⧉</button>
                        <button class="btn btn-secondary btn-icon" onclick="editObs(${i})" title="Edit this entry. Not while it is running.">✎</button>
                        <button class="btn btn-danger btn-icon" onclick="deleteObs(${i})" title="Remove this entry from the schedule. Not while it is running.">✕</button>
                        ${simulatable(obs) ? `<button class="btn btn-secondary btn-icon" onclick="simulateObs(${i})" title="Open this entry in the Simulator: its pointing, band and settings, with the clock pinned to its start (a drift scan, to its beam-crossing time).">🔭</button>` : ''}
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
            document.getElementById('obsComment').value = obs.comment || '';
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
            const clashes = checkClash();
            if (clashes && clashes.length) {
                alert('Cannot save: this observation overlaps "'
                      + clashes.join('", "') + '". Edit or delete that entry '
                      + 'first (disabled entries never count).');
                return;
            }
            const i = parseInt(document.getElementById('obsIndex').value);
            // A famous target is saved as an ordinary galactic entry: the
            // picker chooses the coordinates, the storage holds them. Decimal
            // degrees go in the deg field with min/sec zero - dms_to_decimal
            // sums the three as given, so l=121.17 arrives intact. The Sun
            // and Moon rows are the exception: they are ephemeris objects and
            // save as coord_system "object", same as the dedicated dropdown.
            const famous = document.getElementById('obsCoordSystem').value === 'famous'
                ? FAMOUS_TARGETS[parseInt(
                      document.getElementById('obsFamousTarget').value)]
                : null;
            const famousObject = famous && famous[1] === 'object';
            // Famous + drift mode is an ordinary drift entry: same derived
            // start (T - window), same beam-crossing astronomy, coordinates
            // from the list. The boxes already hold them (onFamousTargetChange
            // fills them), but the list is used directly for the same reason
            // as the tracked case - it is the copy that cannot be mistyped.
            const famousDrift = famous && !famousObject
                && document.getElementById('obsFamousMode').value === 'drift';
            const isDrift = famousDrift
                || document.getElementById('obsCoordSystem').value === 'drift';
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
                name: document.getElementById('obsName').value
                      || (famous ? famous[0] : ''),
                comment: document.getElementById('obsComment').value.trim(),
                coord_system: famousObject ? 'object'
                              : famousDrift ? 'drift'
                              : famous ? 'galactic'
                              : document.getElementById('obsCoordSystem').value,
                object_name: famousObject ? famous[2]
                             : document.getElementById('obsObjectName').value,
                tle_text: document.getElementById('obsTleText').value,
                coord1_deg: famousObject ? 0 : famous ? famous[1]
                            : parseFloat(document.getElementById('coord1Deg').value) || 0,
                coord1_min: famous ? 0
                            : parseInt(document.getElementById('coord1Min').value) || 0,
                coord1_sec: famous ? 0
                            : parseFloat(document.getElementById('coord1Sec').value) || 0,
                coord2_deg: famousObject ? 0 : famous ? famous[2]
                            : parseFloat(document.getElementById('coord2Deg').value) || 0,
                coord2_min: famous ? 0
                            : parseInt(document.getElementById('coord2Min').value) || 0,
                coord2_sec: famous ? 0
                            : parseFloat(document.getElementById('coord2Sec').value) || 0,
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
            // Make a second Save an edit of the same entry, not a second add.
            // Saving is silent when it works, so a double-click - or a "did
            // that take?" second press - used to find the first press's copy
            // already in the schedule and refuse itself as a clash with it:
            // the entry was saved and the alert said it could not be. Pointing
            // obsIndex at the saved entry makes the operation idempotent.
            document.getElementById('obsIndex').value = i >= 0 ? i : schedule.length - 1;
            closeModal();
            renderSchedule();
            autoSave();
        }

        // Which entries have a sky position the simulator can show. Satellites,
        // calibration days and horizon scans do not.
        function simulatable(obs) {
            return ['galactic', 'radec', 'altaz', 'drift', 'object'].includes(obs.coord_system);
        }

        // The mirror of the simulator's Schedule button: hand an entry to the
        // simulator as a deep link, in whatever frame the entry is in - the
        // simulator does the astronomy - with the clock pinned to the entry's
        // moment, so the horizon, the Sun and the Moon are drawn as they will
        // be then. A drift scan is pinned to its beam-crossing time T and
        // opens in continuum mode with the drift panel; anything else to its
        // start, with tau as the whole integration.
        function simulateObs(i) {
            const o = schedule[i];
            const q = new URLSearchParams();
            const sys = o.coord_system;
            const c1 = dmsToDecimalJs(o.coord1_deg, o.coord1_min, o.coord1_sec);
            const c2 = dmsToDecimalJs(o.coord2_deg, o.coord2_min, o.coord2_sec);
            const frame = sys === 'drift' ? (o.drift_frame || 'radec') : sys;
            if (frame === 'galactic') { q.set('l', c1); q.set('b', c2); }
            else if (frame === 'radec') { q.set('ra', c1 * 15); q.set('dec', c2); }   // hours -> degrees
            else if (frame === 'altaz') { q.set('alt', c1); q.set('az', c2); }
            else if (sys === 'object') { q.set('target', o.object_name || 'sun'); }
            else { alert('This entry has no sky position to simulate.'); return; }
            const drift = sys === 'drift' || sys === 'altaz';
            const date = o.start_date || localDateStr(new Date());
            let when = new Date(`${date}T${o.start_time || '12:00'}`);
            if (drift) {
                q.set('mode', 'cont');
                q.set('sd', o.duration_minutes || 240);
                if (o.integration_time_s) q.set('tint', o.integration_time_s);
                if (o.drift_time) {
                    let T = new Date(`${date}T${o.drift_time}`);
                    if (T < when) T = new Date(T.getTime() + 86400000);   // T past midnight
                    when = T;
                }
            } else {
                q.set('tint', (o.duration_minutes || 30) * 60);
            }
            if (!isNaN(when)) q.set('time', when.toISOString());
            if (o.bandwidth_mhz) q.set('bw', o.bandwidth_mhz);
            if (o.center_freq_mhz) q.set('fc', o.center_freq_mhz);
            if (o.channels) q.set('nc', o.channels);
            showSimulator('/simulator/?' + q.toString());
            switchTab('simulator');
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
            return ` — Next: ${nearestName} in ${t}`;
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
                        `Rise: ${p.rise_time_local} (Az ${p.rise_az}\u00b0)<br>` +
                        `Max:  ${p.max_time_utc} UTC (El ${p.max_el}\u00b0)<br>` +
                        `Set:  ${p.set_time_local} (Az ${p.set_az}\u00b0)<br>` +
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
