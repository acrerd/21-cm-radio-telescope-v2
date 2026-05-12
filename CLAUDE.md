# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project context

This is a complete control system for a Small Radio Telescope (SRT) at Acre Road Observatory, replacing the legacy software in `../acreroad_1420-master`. The original product spec is in `../claude.md` (the parent folder). The legacy hardware spec is in `../acreroad_1420-master/docs/MOTOR_CONTROL_HARDWARE_SPEC.md`.

## Build / run commands

PlatformIO is invoked via the conda-installed binary at `~/.platformio/penv/Scripts/pio.exe` (Windows). All builds should be done from this absolute path — `pio` is not on PATH.

**Arduino Due drive firmware** (this folder):
```
~/.platformio/penv/Scripts/pio.exe run -e due           # build
~/.platformio/penv/Scripts/pio.exe run -e due -t upload # flash
~/.platformio/penv/Scripts/pio.exe run -e simulation    # build host-side simulation
```

**ESP32 controller firmware** (`esp32_controller_arduino/`):
```
cd esp32_controller_arduino
~/.platformio/penv/Scripts/pio.exe run                  # builds esp32s3 + wt32-eth01
~/.platformio/penv/Scripts/pio.exe run -e wt32-eth01 -t upload
~/.platformio/penv/Scripts/pio.exe test -e native       # host unit tests for coordinates
```
The two `native` environments under the controller often fail to link on this Windows host (missing `WinMain`); the two firmware environments (`esp32s3`, `wt32-eth01`) are the canonical targets and must always build clean.

**Receiver scheduler** (`receiver_scheduler/`):
```
cmd //c "c:\Users\graha\Desktop\new_SRT\new_SRT_drive\receiver_scheduler\run_scheduler.bat"
```
Always launch via the absolute path to the .bat — relative paths fail. The bat activates radioconda then runs `python h1_web_scheduler.py`. Web UI on `http://localhost:5000`.

## Architecture

Four loosely-coupled subsystems talking over serial / HTTP:

```
Stellarium ──TCP:10001──┐                       Scheduler (Flask) ──┐
                        ↓                                            ↓
              ESP32 controller (web UI :80, web_server.cpp) ─────────┘
                        ↓
              UART (Serial1, 115200, line-oriented ASCII)
                        ↓
              Arduino Due drive firmware (src/main.cpp)
                        ↓
              Motors / encoders / limit switches
```

**Drive firmware** (`src/main.cpp`, single file ~1900 lines): bare-metal motor control on Arduino Due. Position is tracked in **pulses** (`PULSES_PER_DEGREE = 2`) by ISRs on FALLING edges of reed-switch encoders. Direction is inferred from the static DIR pin at the time of the pulse. Motor sense for both axes is inverted in software via `AZ_DIR_INVERT` / `ALT_DIR_INVERT` macros in `include/config.h` — every read/write of `PIN_DIR_AZ`/`PIN_DIR_ALT` must go through `AZ_DIR()` / `ALT_DIR()`.

**Coordinate model**: position 0 (in pulses) corresponds to the hardware lower limit (`azHwMin = 0°`, `altHwMin = 0°`). Displayed degrees = `position / PULSES_PER_DEGREE`. After homing, both axes read (0, 0). The `getHomeAzOffsetPulses()` / `getHomeAltOffsetPulses()` helpers compute home as `cfg.homeAz/Alt * PULSES_PER_DEGREE` directly — they do **not** subtract `azMin`. Any new code that touches the relationship between pulse counts and degrees must respect this.

**Homing** (`performHoming()`): three-phase. (1) `driveToLimits()` ramps up to full speed and runs into the hard stop until pulses stop for `stallTimeoutMs`. (2) `backOffFromLimits(5°)` reverses with the same ramp. (3) `driveToLimits()` again, slower, to set the precise zero. Both helpers use `calculatePWM(INT32_MAX, startTime)` to get the standard ramp-up profile without ever entering the ramp-down branch. After Phase 3 both `positionAz` and `positionAlt` are zeroed.

**Motion controller** (`updateAxisMotion`): runs from the main loop only when not in `STATE_HOMING`/`STATE_FAULT`. The IDLE→DRIVING transition only fires if a `newTargetAz`/`newTargetAlt` flag was set by a command — this prevents the controller from chasing stray encoder pulses into a limit switch when idle. `executeDrive()` is the only place that sets these flags.

**Stall detection** requires *both* `(now - driveStartTime) > stallTimeoutMs` *and* `(now - lastPulse) > stallTimeoutMs`, so a stale `lastPulse` from a long idle period can't trigger a false stall the moment a new slew starts. When a stall happens while driving negative within 2° of the hardware limit, it's treated as "arrived at limit" — position snaps to the limit and `targetAz/Alt` are clamped, no fault.

**Encoder ISR debouncing**: `lastPulseAz/Alt` is **only** updated on pulses that pass the debounce window. Updating on every edge (the original bug) caused total pulse loss when motor speed approached the debounce window — every edge reset the timer and nothing ever counted. Don't reintroduce that.

**Current sensing**: `checkCurrentLimits()` always uses the **filtered** current (`filteredCurrentAz/Alt`), never the raw reading. The zero-current sensor offset is **continuously re-tracked** while each axis is `MOTION_IDLE` (in `updateFilteredCurrents()`), so long-term hall-effect drift settles toward zero on its own.

**Driver fault flags**: H-bridge fault outputs (`FF1`/`FF2`) are noisy at motor stop/start due to inductive kickback and supply sag. `checkFaultFlags()` requires the same fault to persist for `FAULT_FLAG_PERSIST_COUNT` (5) consecutive calls before reporting it.

**Status output dedup**: `outputStatus()` builds a `statusCompare` string excluding the live current readings, then prints to the programming-port `Serial` only when that string differs from the last printed one. The full `statusLine` (with currents) always goes to `Serial1` (controller) so the ESP32 sees every update. STATUS commands received over Serial1 (`cmdFromSerial1 == true`) respond only to Serial1 and never touch the programming-port dedup buffer.

**Configuration**: `Config cfg` lives in RAM and is persisted to flash via DueFlashStorage. All defaults are `#define`s in `include/config.h`. `loadDefaults()` must initialize every field of `cfg` — missing any field causes garbage values until `SAVE` is called. Add new tunable parameters by: (1) adding a `#define DEFAULT_X`, (2) adding a field to the `Config` struct, (3) initializing it in `loadDefaults`, (4) handling it in `processSetCommand`, (5) printing it in `showConfig` and `showHelp`.

**Simulation environment** (`-e simulation`): overrides `analogWrite`/`digitalWrite`/`analogRead`/`attachInterrupt` via macros so the firmware runs without real hardware. `simulatePulses()` is called from the main loop and inside the homing while-loops to advance position based on commanded PWM. When you change the homing flow or any in-loop motor control, it must still work in simulation — both `due` and `simulation` environments must build cleanly.

**ESP32 controller** (`esp32_controller_arduino/src/`): Arduino-framework PlatformIO project, two target boards (`esp32s3`, `wt32-eth01`). `state.h` is the single shared `SRTState` struct — all subsystems mutate it directly. Coordinate transforms are in `coordinates.cpp` (Julian date, GMST, precession J2000↔date, RA/Dec↔Alt/Az, galactic↔equatorial, sun/moon ephemeris). The web UI is a single embedded HTML string in `index_html.h` — heavy use of inline JS and `fetch()` against the various web_server.cpp endpoints. The `/status` endpoint computes RA/Dec and galactic l/b on the fly from the live alt/az; goto/track endpoints reject targets below the horizon (alt < 0) before mutating state.

## Conventions

- **Editor**: never use `cat`/`echo`/`sed` from Bash; use the dedicated Read/Edit/Write tools.
- **Forward slashes** in paths even on Windows (Bash tool runs in MSYS).
- **Status line format** (don't break it — the ESP32 parser is positional):
  `Alt:%.1f Az:%.1f Ialt:%.1fA Iaz:%.1fA Status:<state> [<fault>] -> Alt:%.1f Az:%.1f Cal:ON|OFF`
- **Don't use TodoWrite** for trivial in-conversation tasks. The user has it disabled in spirit and gentle reminders to use it should be ignored unless the task is multi-step and would actually benefit.
- **Don't run destructive git commands** (`reset --hard`, `push --force`, `--no-verify`) without explicit instruction. Commits are created on user request only.
