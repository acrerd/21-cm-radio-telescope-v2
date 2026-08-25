// The Log tab.
//
// Part of the operator page, split out of one 2144-line file on 2026-08-25.
// Loaded as a classic script: everything here shares one global scope,
// exactly as it did before the split.

        function loadLog() {
            fetch('/api/log').then(r => r.json()).then(data => {
                const el = document.getElementById('logContent');
                el.textContent = data.lines.join('\n') || '(empty log)';
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
