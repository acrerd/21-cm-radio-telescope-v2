#!/usr/bin/env bash

set -u

REPO_ROOT="/home/astro/21-cm-radio-telescope-v2"
WORKSPACE="$REPO_ROOT/receiver_scheduler/SRT Software.code-workspace"
CONTROLLER_URL="http://192.168.50.120/"
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

window_ids() {
    wmctrl -l 2>/dev/null | awk '{print $1}' | tr '\n' ' '
}

wait_for_window() {
    local title_pattern="$1"
    local previous_ids="$2"
    local timeout_seconds="${3:-45}"
    local deadline=$((SECONDS + timeout_seconds))
    local id title

    while ((SECONDS < deadline)); do
        while read -r id _ _ title; do
            [[ -n "$id" ]] || continue
            if [[ "$title" =~ $title_pattern && " $previous_ids " != *" $id "* ]]; then
                printf '%s\n' "$id"
                return 0
            fi
        done < <(wmctrl -l 2>/dev/null)
        sleep 1
    done

    # Single-instance applications may reuse an existing window. Fall back to
    # the first matching title if no new window appeared.
    while read -r id _ _ title; do
        if [[ "$title" =~ $title_pattern ]]; then
            printf '%s\n' "$id"
            return 0
        fi
    done < <(wmctrl -l 2>/dev/null)
    return 1
}

place_window() {
    local id="$1"
    local geometry="$2"
    wmctrl -i -r "$id" -b remove,fullscreen 2>/dev/null || true
    wmctrl -i -r "$id" -b remove,maximized_vert,maximized_horz 2>/dev/null || true
    sleep 0.5
    wmctrl -i -r "$id" -e "$geometry" 2>/dev/null || true
}

tile_windows() {
    local previous_firefox_ids="$1"
    local previous_stellarium_ids="$2"
    local wmctrl_bin
    wmctrl_bin="$(command -v wmctrl 2>/dev/null || true)"
    if [[ -z "$wmctrl_bin" ]]; then
        log "Window layout skipped: install wmctrl to enable automatic tiling."
        return
    fi

    local firefox_id stellarium_id code_id
    # Ubuntu's confined Firefox snap must use its native session backend. It may
    # therefore be invisible to wmctrl on Wayland; do not delay other windows
    # for the full application timeout when that happens.
    firefox_id="$(wait_for_window 'Firefox' "$previous_firefox_ids" 12 || true)"
    stellarium_id="$(wait_for_window 'Stellarium' "$previous_stellarium_ids" 45 || true)"
    code_id="$(wmctrl -l 2>/dev/null | awk 'BEGIN{IGNORECASE=1} /Visual Studio Code/ {print $1; exit}')"

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

    # VS Code uses the bottom half; Stellarium and Firefox share the top. Some
    # applications alter their own geometry late in startup, so apply twice.
    local pass
    for pass in 1 2; do
        [[ -z "$code_id" ]] || place_window "$code_id" "0,0,$half_height,$width,$half_height"
        [[ -z "$stellarium_id" ]] || place_window "$stellarium_id" "0,0,0,$half_width,$half_height"
        [[ -z "$firefox_id" ]] || place_window "$firefox_id" "0,$half_width,0,$half_width,$half_height"
        sleep 2
    done

    if [[ -z "$firefox_id" || -z "$stellarium_id" ]]; then
        log "Window layout incomplete: Firefox id='${firefox_id:-missing}', Stellarium id='${stellarium_id:-missing}'."
    else
        log "Applied SRT workstation window layout (${width}x${height})."
    fi
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
    "$code_bin" --reuse-window "$WORKSPACE" >> "$LOG_FILE" 2>&1 &
    log "Opened VS Code workspace; its automatic tasks start the H1 scheduler and PlatformIO Serial Monitor."

    local previous_firefox_ids previous_stellarium_ids
    previous_firefox_ids="$(window_ids)"
    sleep 2
    "$firefox_bin" --new-window "$CONTROLLER_URL" >> "$LOG_FILE" 2>&1 &
    log "Opened live SRT Controller website in Firefox."

    previous_stellarium_ids="$(window_ids)"
    QT_QPA_PLATFORM=xcb "$stellarium_bin" >> "$LOG_FILE" 2>&1 &
    log "Started Stellarium."

    tile_windows "$previous_firefox_ids" "$previous_stellarium_ids" &
}

main "$@"
