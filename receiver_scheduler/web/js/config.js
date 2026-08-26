// The Configuration tab.
//
// Part of the operator page, split out of one 2144-line file on 2026-08-25.
// Loaded as a classic script: everything here shares one global scope,
// exactly as it did before the split.

        // ---- Configuration ----
        // The instrument boxes hold the config *overrides*, empty meaning
        // the default from tuning.py; what is actually in force is shown
        // above them. Remembered as loaded so a save can tell what changed
        // and warn before it goes through.
        const INSTRUMENT_BOXES = {
            receiver_lo_hz: ['cfgInstLo', 1e6],
            receiver_sample_rate_hz: ['cfgInstRate', 1e6],
            receiver_gain_db: ['cfgInstGain', 1],
            receiver_h1_channels: ['cfgInstH1Ch', 1],
            receiver_wide_channels: ['cfgInstWideCh', 1],
        };
        let instrumentLoaded = {};

        function readInstrumentBoxes() {
            const out = {};
            for (const [key, [id, scale]] of Object.entries(INSTRUMENT_BOXES)) {
                const v = document.getElementById(id).value.trim();
                out[key] = v === '' ? null : parseFloat(v) * scale;
            }
            const lo = document.getElementById('cfgInstH1Lo').value.trim();
            const hi = document.getElementById('cfgInstH1Hi').value.trim();
            out.receiver_h1_band_hz = (lo === '' && hi === '') ? null
                : [parseFloat(lo) * 1e6, parseFloat(hi) * 1e6];
            return out;
        }

        function fillInstrumentBoxes(cfg) {
            for (const [key, [id, scale]] of Object.entries(INSTRUMENT_BOXES)) {
                const v = cfg[key];
                document.getElementById(id).value = (v == null || v === '') ? '' : (Number(v) / scale);
            }
            const band = cfg.receiver_h1_band_hz;
            document.getElementById('cfgInstH1Lo').value = band ? band[0] / 1e6 : '';
            document.getElementById('cfgInstH1Hi').value = band ? band[1] / 1e6 : '';
            instrumentLoaded = readInstrumentBoxes();
        }

        function instrumentChanged() {
            const now = readInstrumentBoxes();
            return Object.keys(now).some(k => JSON.stringify(now[k]) !== JSON.stringify(instrumentLoaded[k]));
        }

        function resetInstrumentConfig() {
            for (const [, [id]] of Object.entries(INSTRUMENT_BOXES)) document.getElementById(id).value = '';
            document.getElementById('cfgInstH1Lo').value = '';
            document.getElementById('cfgInstH1Hi').value = '';
        }

        function loadConfig() {
            instrumentText = null;             // re-read: it may just have changed
            showInstrument('cfgInstrument');
            fetch('/api/config').then(r => r.json()).then(cfg => {
                fillInstrumentBoxes(cfg);
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
            // The instrument: warn before a change goes through, because the
            // calibrations belong to the tuning and every recording after
            // this one is uncalibrated until they are re-measured.
            if (instrumentChanged()) {
                const ok = confirm(
                    'You are changing the instrument tuning.\n\n'
                    + 'Every scheduled observation from now on records with the new tuning. '
                    + 'The bandpass templates and the gain calibration belong to the old one, '
                    + 'so recordings will be in counts, not kelvin, until you re-measure the '
                    + 'bandpass and the gain on the RF Calibration tab. The simulator\'s band '
                    + 'stays on the old tuning until make_web_data.py --meta is re-run.\n\n'
                    + 'Change the instrument?');
                if (!ok) return;
            }
            Object.assign(cfg, readInstrumentBoxes());
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
                    instrumentLoaded = readInstrumentBoxes();
                    instrumentText = null;
                    showInstrument('cfgInstrument');
                } else {
                    alert('Error saving: ' + (data.error || 'Unknown'));
                }
            });
        }
