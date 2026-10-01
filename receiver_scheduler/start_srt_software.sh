#!/usr/bin/env bash
#
# The desktop "Start SRT Sofware" launcher: starts the H1 scheduler (if one is
# not already serving) and opens the controller's page in Firefox.
#
# VS Code and Stellarium were taken out on 2026-10-01 at the operator's request.
# VS Code's workspace task was what started the scheduler, so the scheduler is
# now started here directly, through the same start_scheduler.sh guard.
# Stellarium is still installed and configured (telescope at
# 192.168.50.120:10001) and is started by hand.

set -u

REPO_ROOT="/home/astro/21-cm-radio-telescope-v2"
CONTROLLER_URL="http://192.168.50.120/"
LOG_FILE="/tmp/srt-software-launcher.log"
SCHEDULER_CONSOLE="/tmp/srt-scheduler-console.log"

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"
}

find_command() {
    local name="$1"
    local fallback="$2"
    if command -v "$name" >/dev/null 2>&1; then
        command -v "$name"
    elif [[ -x "$fallback" ]]; then
        printf '%s\n' "$fallback"
    else
        return 1
    fi
}

main() {
    : > "$LOG_FILE"

    # Detached, so closing the desktop session's launcher does not take the
    # scheduler with it. start_scheduler.sh reuses a scheduler already serving.
    setsid "$REPO_ROOT/receiver_scheduler/start_scheduler.sh" >> "$SCHEDULER_CONSOLE" 2>&1 < /dev/null &
    log "Started the H1 scheduler (or found it running); console output in $SCHEDULER_CONSOLE."

    local firefox_bin
    firefox_bin="$(find_command firefox /usr/bin/firefox)" || {
        log "Firefox was not found."
        return 1
    }
    "$firefox_bin" --new-window "$CONTROLLER_URL" >> "$LOG_FILE" 2>&1 &
    log "Opened live SRT Controller website in Firefox."
}

main "$@"
