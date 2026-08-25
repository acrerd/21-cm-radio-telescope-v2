// What runs when the page loads. Last, so everything it calls exists.

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
        // Start log auto-refresh
        logRefreshTimer = setInterval(loadLog, 5000);
        loadBanner();
