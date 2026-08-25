// The safety camera tab.
//
// Part of the operator page, split out of one 2144-line file on 2026-08-25.
// Loaded as a classic script: everything here shares one global scope,
// exactly as it did before the split.

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
                    when.toLocaleTimeString() + ' local</span>' +
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
