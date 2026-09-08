// The safety camera tab.
//
// Part of the operator page, split out of one 2144-line file on 2026-08-25.
// Loaded as a classic script: everything here shares one global scope,
// exactly as it did before the split.

        function cameraTabVisible() {
            const tab = document.getElementById('tab-camera');
            return !document.hidden && tab && tab.classList.contains('active');
        }

        // The dropdown: a number of seconds between snapshots, or a live mode.
        function cameraMode() {
            const value = document.getElementById('camAutoRefresh').value;
            if (value === 'live') return {live: true, fps: 0};
            if (value === 'live5') return {live: true, fps: 5};
            return {live: false, every: parseInt(value, 10) || 0};
        }

        // Chained from the end of each capture rather than run on an interval:
        // at 1 s the capture takes a good fraction of the gap, and setInterval
        // would queue requests behind each other the moment one ran long.
        // In live mode this makes sure the feed is up while the tab is on
        // screen and down when it is not.
        function scheduleCameraRefresh() {
            if (camTimer) { clearTimeout(camTimer); camTimer = null; }
            const mode = cameraMode();
            if (!cameraTabVisible()) { stopCameraLive(); return; }
            if (mode.live) { startCameraLive(mode.fps); return; }
            if (!mode.every) return;
            camTimer = setTimeout(refreshCamera, mode.every * 1000);
        }

        function onCameraAutoChange() {
            if (camTimer) { clearTimeout(camTimer); camTimer = null; }
            const mode = cameraMode();
            if (mode.live) { startCameraLive(mode.fps); return; }
            stopCameraLive();
            if (mode.every) refreshCamera();
        }

        // Live video: the <img> is what holds the connection, so it is
        // created here and removed in stopCameraLive - never left in the
        // page with the tab hidden.
        function startCameraLive(fps) {
            if (camLiveImg && camLiveImg.dataset.fps === String(fps)) return;   // already up
            stopCameraLive();
            const status = document.getElementById('camStatus');
            const img = document.createElement('img');
            img.id = 'camLive';
            img.dataset.fps = String(fps);
            img.alt = 'Safety camera, live';
            img.style.cssText = 'max-width:100%; border-radius:6px;';
            img.src = '/api/camera/stream?fps=' + fps + '&t=' + Date.now();
            img.onload = () => {
                status.innerHTML = '<span style="color:#00d4ff;">Live</span>' +
                    '<span style="color:#888;"> &middot; ' + (fps ? fps + ' fps' : 'every frame') + '</span>';
            };
            // The connection ended (stream stopped, scheduler restarted, no
            // camera): say so and leave the last frame rather than a broken
            // image icon, and come back with the next refresh cycle.
            img.onerror = () => {
                if (camLiveImg !== img) return;
                status.innerHTML = '<span style="color:#ff4757;">Live feed ended</span>';
                camLiveImg = null;
                if (cameraTabVisible()) camTimer = setTimeout(scheduleCameraRefresh, 3000);
            };
            const view = document.getElementById('camView');
            view.innerHTML = '';
            view.appendChild(img);
            camLiveImg = img;
            status.innerHTML = '<span style="color:#888;">Connecting&hellip;</span>';
        }

        function stopCameraLive() {
            if (!camLiveImg) return;
            const img = camLiveImg;
            camLiveImg = null;
            img.onerror = null;
            img.src = '';                      // drops the connection
            if (img.parentNode) img.parentNode.removeChild(img);
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
            // In live mode a refresh (re)starts the feed; nothing to fetch.
            if (cameraMode().live) { stopCameraLive(); scheduleCameraRefresh(); return; }
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
                const source = r.headers.get('X-Capture-Source');
                return r.blob().then(blob => ({blob, captured, frames, source}));
            }).then(({blob, captured, frames, source}) => {
                const url = URL.createObjectURL(blob);
                document.getElementById('camView').innerHTML =
                    '<img src="' + url + '" alt="Safety camera view" ' +
                    'style="max-width:100%; border-radius:6px;">';
                // Only after the new frame is on screen, or the browser may
                // still be decoding the old one.
                if (camObjectUrl) URL.revokeObjectURL(camObjectUrl);
                camObjectUrl = url;
                const when = captured ? new Date(captured) : new Date();
                // Frames 0 means the picture came off the live stream, which
                // runs while this tab is watched; otherwise it was a one-shot
                // capture straight off the V4L2 device.
                const how = (source === 'pipewire' || frames === '0') ? 'live'
                    : (frames ? frames + (frames === '1' ? ' frame' : ' frames') : '');
                status.innerHTML = '<span style="color:#00d4ff;">Captured ' +
                    when.toLocaleTimeString() + ' local</span>' +
                    (how ? '<span style="color:#888;"> &middot; ' + how + '</span>' : '');
            }).catch(e => {
                status.innerHTML = '<span style="color:#ff4757;">' + e.message + '</span>';
            }).finally(() => {
                button.disabled = false;
                // Chained even after a failure, so a camera that comes back
                // recovers on its own rather than needing a click.
                scheduleCameraRefresh();
            });
        }
