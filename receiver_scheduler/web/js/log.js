// The Log tab.
//
// Part of the operator page, split out of one 2144-line file on 2026-08-25.
// Loaded as a classic script: everything here shares one global scope,
// exactly as it did before the split.

        function loadLog() {
            const lines = document.getElementById('logLines').value || 100;
            fetch('/api/log?lines=' + lines).then(r => r.json()).then(data => {
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

        // Search the whole log - the current file and the rotated older ones -
        // which the live view, the last `log_lines` of one file, cannot reach
        // (2026-10-16: a holdover a week back was 650 lines up). Results are
        // newest first; auto-refresh pauses so they are not overwritten.
        function searchLog() {
            const q = document.getElementById('logSearchQ').value.trim();
            const since = document.getElementById('logSearchSince').value;
            const until = document.getElementById('logSearchUntil').value;
            const level = document.getElementById('logSearchLevel').value;
            const after = document.getElementById('logSearchAfter').value;
            const info = document.getElementById('logSearchInfo');
            const el = document.getElementById('logContent');
            if (!q && !since && !until && !level) {
                info.textContent = 'give some text, a date or a level';
                return;
            }
            const box = document.getElementById('logAutoRefresh');
            box.checked = false;
            clearInterval(logRefreshTimer);
            logRefreshTimer = null;
            info.textContent = 'searching...';
            const p = new URLSearchParams({q: q, since: since, until: until, level: level, after: after, limit: 500});
            fetch('/api/log/search?' + p.toString()).then(r => r.json()).then(data => {
                if (data.error) { info.textContent = data.error; return; }
                const shown = data.matches.length;
                info.textContent = data.total + ' match' + (data.total === 1 ? '' : 'es')
                    + (data.truncated ? ', newest ' + shown + ' shown' : '') + ' in ' + data.files.join(', ');
                el.textContent = shown
                    ? data.matches.map(m => m[0] + m.slice(1).map(l => '\n    ' + l).join('')).join(after > 0 ? '\n\n' : '\n')
                    : '(nothing found)';
                el.scrollTop = 0;
            }).catch(() => { info.textContent = 'search failed'; });
        }

        function clearLogSearch() {
            document.getElementById('logSearchInfo').textContent = '';
            const box = document.getElementById('logAutoRefresh');
            box.checked = true;
            clearInterval(logRefreshTimer);       // never two timers
            toggleLogRefresh();
            loadLog();
        }

        // The auto-refresh is started once, in boot.js. It was also started
        // here, so there were two timers and the page held only one: unticking
        // Auto-refresh, or a search, stopped one while the other went on
        // redrawing the live log every 5 s (2026-10-16).
