#!/usr/bin/env bash
#
# Launch the H1 scheduler, but only if one is not already running.
#
# The VS Code workspace runs this on folderOpen, so it fires every time the
# launcher reuses an existing window or a second window is opened. Starting the
# scheduler again in that situation is not harmless: the second process dies on
# "Address already in use" after the SDR and controller state have been touched,
# and the failure is buried in a task terminal set to "reveal": "never".
#
# Deliberately not a systemd unit — the scheduler starts from the desktop
# "Start SRT Software" launcher and should never come up unattended.

set -u

PYTHON="/home/astro/radioconda/bin/python"
SCHEDULER="/home/astro/21-cm-radio-telescope-v2/receiver_scheduler/h1_web_scheduler.py"
HOST="0.0.0.0"
PORT="5000"
LOCK_FILE="/tmp/srt-h1-scheduler.lock"

# Guards two windows opening close enough together that both pass the check
# below before either has bound the port. Held for the life of the scheduler:
# exec keeps the descriptor open in the process that replaces this one.
exec 9>"$LOCK_FILE"
if ! flock --nonblock 9; then
    echo "The H1 scheduler is already being started by another VS Code window."
    exit 0
fi

# An already-serving scheduler is the normal case, not an error: reuse it.
if curl -sf -m 3 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
    echo "The H1 scheduler is already running on port $PORT — reusing it."
    echo "Open http://localhost:$PORT"
    exit 0
fi

# Bound but not answering as the scheduler. Starting another would only produce
# a confusing bind error, so say what actually holds the port instead.
if ss -ltn "sport = :$PORT" 2>/dev/null | grep -q ":$PORT"; then
    echo "Port $PORT is in use but is not answering as the H1 scheduler."
    echo "Find the owner with:  ss -ltnp 'sport = :$PORT'"
    exit 1
fi

if [[ ! -x "$PYTHON" ]]; then
    echo "The receiver Python was not found at $PYTHON"
    exit 1
fi

echo "Starting the H1 scheduler on http://localhost:$PORT"
exec "$PYTHON" "$SCHEDULER" --host "$HOST" --port "$PORT"
