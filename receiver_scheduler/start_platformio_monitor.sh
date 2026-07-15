#!/usr/bin/env bash

set -u

PIO="/home/astro/.platformio/penv/bin/platformio"
PORT="${SRT_SERIAL_PORT:-/dev/ttyACM0}"
LOCK_FILE="/tmp/srt-platformio-monitor.lock"
ATTEMPT_LOG="/tmp/srt-platformio-monitor-attempt.log"

exec 9>"$LOCK_FILE"
if ! flock --nonblock 9; then
    echo "The SRT PlatformIO Serial Monitor is already running in another VS Code window."
    echo "Close the older monitor terminal before starting another one."
    exit 0
fi

if [[ ! -x "$PIO" ]]; then
    echo "PlatformIO was not found at $PIO"
    exit 1
fi

echo "Waiting for the SRT controller serial port: $PORT"
for _ in {1..30}; do
    [[ -e "$PORT" ]] && break
    sleep 1
done
if [[ ! -e "$PORT" ]]; then
    echo "Serial port $PORT did not appear after 30 seconds."
    echo "Check the Arduino Due USB cable and power, then rerun this task."
    exit 1
fi

for attempt in {1..10}; do
    echo "Starting PlatformIO Serial Monitor (attempt $attempt/10)..."
    "$PIO" device monitor --environment due --port "$PORT" 2>&1 | tee "$ATTEMPT_LOG"
    status=${PIPESTATUS[0]}
    [[ $status -eq 0 ]] && exit 0

    if grep -Eq "Could not exclusively lock port|Resource temporarily unavailable|Device or resource busy" "$ATTEMPT_LOG"; then
        holder="$(fuser "$PORT" 2>/dev/null || true)"
        if [[ -n "$holder" ]]; then
            echo "Port $PORT is currently owned by process:$holder"
        fi
        echo "The serial port is temporarily busy; retrying in 3 seconds..."
        sleep 3
        continue
    fi
    exit "$status"
done

echo "Could not open $PORT after 10 attempts. Close any older serial monitor or Arduino IDE connection and rerun this task."
exit 1
