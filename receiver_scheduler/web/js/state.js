// Shared page state.
// 
// Loaded first because let and const are not hoisted: a tab file running before
// these were initialised would meet them in the temporal dead zone and throw,
// rather than seeing undefined.
//
// Part of the operator page, split out of one 2144-line file on 2026-08-25.
// Loaded as a classic script: everything here shares one global scope,
// exactly as it did before the split.

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
            comment: "",
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

        let driftNextTransit = null;

        let wasRunning = null;

        let soundEnabled = true;

        let tleResultsData = [];

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

        // ---- Observe ----
        // Stamp of the hand-off already on the form, so opening the tab picks up
        // a Realise that happened since it was last looked at, and does not
        // overwrite edits made here in the meantime.
        let obvAppliedStamp = null;

        // ---- Horizon scan ----
        let hzPollTimer = null;

        // ---- what the receiver will actually be tuned to ----
        // The B210 is a direct-conversion receiver, so the tuned frequency
        // lands on the FFT's DC bin and UHD's automatic offset correction
        // subtracts whatever is there - including the line. The LO is
        // therefore offset, and the sample rate raised if it must be to keep
        // the line in the flat part of the band. Saying so here means the
        // numbers typed above are never silently replaced.
        let obvTuningTimer = null;

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

        // ---- Safety camera ----
        // Fetched as a blob rather than pointed at with an <img src>: it keeps
        // the previous frame on screen while the next is being taken, and a
        // failure arrives as the server's actual message instead of a broken
        // image icon.
        let camObjectUrl = null;

        let camTimer = null;

        // ---- Sun Scan ----
        let ssPollTimer = null;

        // ---- Calibration Day ----
        let cdPollTimer = null;

        // Number of scans on file, from /api/calday/data. Held here rather
        // than written straight into cdStatus: loadCalModel() and pollCalDay()
        // are both in flight when the tab opens, and whichever landed last
        // used to win — which is how a running calibration day could be
        // reported as "Idle".
        let cdArchiveCount = null;

        // ---- Log ----
        let logRefreshTimer = null;
