#!/usr/bin/env bash

set -u

REPO_ROOT="/home/astro/21-cm-radio-telescope-v2"
WORKSPACE="$REPO_ROOT/receiver_scheduler/SRT Software.code-workspace"
CONTROLLER_HTML="/home/astro/Desktop/1 Open This:     SRT Controller.html"
LOG_FILE="/tmp/srt-software-launcher.log"

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

tile_windows() {
    local wmctrl_bin
    wmctrl_bin="$(command -v wmctrl 2>/dev/null || true)"
    if [[ -z "$wmctrl_bin" ]]; then
        log "Window layout skipped: install wmctrl to enable automatic tiling."
        return
    fi

    local dimensions width height half_width half_height
    dimensions="$(xrandr --current 2>/dev/null | awk '/\*/ {print $1; exit}')"
    if [[ ! "$dimensions" =~ ^([0-9]+)x([0-9]+)$ ]]; then
        log "Window layout skipped: could not determine display dimensions."
        return
    fi
    width="${BASH_REMATCH[1]}"
    height="${BASH_REMATCH[2]}"
    half_width=$((width / 2))
    half_height=$((height / 2))

    # VS Code uses the bottom half; Stellarium and Firefox share the top.
    "$wmctrl_bin" -r "Visual Studio Code" -b remove,maximized_vert,maximized_horz 2>/dev/null || true
    "$wmctrl_bin" -r "Visual Studio Code" -e "0,0,$half_height,$width,$half_height" 2>/dev/null || true
    "$wmctrl_bin" -r "Stellarium" -b remove,maximized_vert,maximized_horz 2>/dev/null || true
    "$wmctrl_bin" -r "Stellarium" -e "0,0,0,$half_width,$half_height" 2>/dev/null || true
    "$wmctrl_bin" -r "Firefox" -b remove,maximized_vert,maximized_horz 2>/dev/null || true
    "$wmctrl_bin" -r "Firefox" -e "0,$half_width,0,$half_width,$half_height" 2>/dev/null || true
    log "Applied SRT workstation window layout (${width}x${height})."
}

main() {
    : > "$LOG_FILE"

    local code_bin firefox_bin stellarium_bin
    code_bin="$(find_command code /snap/bin/code)" || {
        log "Visual Studio Code was not found."
        return 1
    }
    firefox_bin="$(find_command firefox /usr/bin/firefox)" || {
        log "Firefox was not found."
        return 1
    }
    stellarium_bin="$(find_command stellarium /usr/bin/stellarium)" || {
        log "Stellarium was not found."
        return 1
    }
    if [[ ! -f "$CONTROLLER_HTML" ]]; then
        log "SRT Controller page was not found: $CONTROLLER_HTML"
        return 1
    fi

    "$code_bin" --new-window "$WORKSPACE" >> "$LOG_FILE" 2>&1 &
    log "Opened VS Code workspace; its automatic tasks start the H1 scheduler and PlatformIO Serial Monitor."

    sleep 2
    "$firefox_bin" --new-window "$CONTROLLER_HTML" >> "$LOG_FILE" 2>&1 &
    log "Opened SRT Controller in Firefox."

    "$stellarium_bin" >> "$LOG_FILE" 2>&1 &
    log "Started Stellarium."

    (sleep 8; tile_windows) &
}

main "$@"
