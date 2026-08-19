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
~/.platformio/penv/Scripts/pio.exe run                  # builds wt32-eth01 (default)
~/.platformio/penv/Scripts/pio.exe run -e wt32-eth01 -t upload
~/.platformio/penv/Scripts/pio.exe test -e native       # host unit tests
```
The deployed board is the **WT32-ETH01** and it is the default build. The `esp32s3` environment is legacy (that board is no longer used) — keep it compiling when convenient but WT32-ETH01 is the canonical target and must always build clean.

The `native` environment runs `test/test_coordinates` and `test/test_pointing` on the host — the pure computation, no board attached. It builds only `coordinates.cpp` and `pointing.cpp` (`build_src_filter`); `test/shim/` supplies the little of `Arduino.h`/`Preferences.h` that `pointing.cpp` touches, so the firmware source is compiled exactly as it is flashed rather than copied into the test. On the observatory Linux host use `~/.platformio/penv/bin/pio test -e native`.

**Receiver scheduler** (`receiver_scheduler/`):
```
cmd //c "c:\Users\graha\Desktop\new_SRT\new_SRT_drive\receiver_scheduler\run_scheduler.bat"
```
Always launch via the absolute path to the .bat — relative paths fail. The bat activates radioconda then runs `python h1_web_scheduler.py`. Web UI on `http://localhost:5000`.
On the observatory Linux host, the scheduler re-execs under `/home/astro/radioconda/bin/python` when available. The controller web UI and OTA target are `http://192.168.50.120/`.

**Controller network** (issue #10, done 2026-08-19; full build procedure in `docs/OBSERVATORY_HOST_SETUP.md`): the controller is *not* on the observatory LAN. It sits on a private point-to-point link — a TP-Link TG-3468 (`enp5s0`, `r8169`) in the observatory computer, NetworkManager connection `srt-link`, `ipv4.method shared`, host at `192.168.50.1/24`. Shared mode supplies the address, the DHCP/DNS server and the NAT together; the last matters because the controller syncs from `pool.ntp.org` and time errors become pointing errors through GMST — a link with no route out mispoints overnight. The controller stays on **DHCP**: its address is pinned by MAC in `/etc/NetworkManager/dnsmasq-shared.d/srt.conf` (`dhcp-host=70:4B:CA:58:59:8B,192.168.50.120`), so nothing on the controller is configured for this link and the whole change reverses by moving one cable back to the campus switch. Second fallback is the WiFi AP at `192.168.4.1`, which is up regardless of Ethernet settings. Access from other machines on the observatory LAN is deliberately not set up. When verifying NAT, a **fresh sync after a reboot** is the real proof: `sync_count` reset to 1 with a small `last_sync_age_s`. `"source":"NTP"` alone proves nothing about the *network*, because `NTP_SERVER_FALLBACK` is a numeric address — the clock syncs whether or not DNS works, and did so all through the period when the controller could not resolve a name at all. (This is not the #9 caveat, which was about a stale clock being reported as synced and is fixed: success is now only ever declared by `onTimeSync()`.)

## Architecture

Four loosely-coupled subsystems talking over serial / HTTP:

```
Stellarium ──TCP:10001──┐                       Scheduler (Flask) ──┐
                        ↓                                            ↓
              ESP32 controller (web UI :80 at 192.168.50.120, web_server.cpp) ─────────┘
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

**Galactic plane target**: `getGalacticPlaneTrackingTarget()` walks outwards along b = 0 from the galactic centre and returns the first longitude at or above `settings.galacticMinAlt` (45° default), preferring the higher of the two equidistant candidates. That floor is an **acquisition** threshold, not a horizon — the target is followed down afterwards until `settings.horizonAlt` parks the dish. It is high on purpose: the galactic centre is at Dec −28.9° and culminates at 5.2° from Glasgow, so it never clears the 10° horizon (0 hours in a year) and the old "Galactic Bulge" button was permanently in fallback. At 45° a point exists ~73% of the time and never within ~45° of longitude of the centre. Endpoint is `/track/galactic-plane`, target name `"Galactic Plane"`, `/ephemeris` key `plane`.

**Pointing frames** (`esp32_controller_arduino/src/pointing.{h,cpp}`): two coordinate frames meet at the ESP32 and the boundary is that file. **True alt/az** is where the dish looks on the sky, computed with the *true* site lat/lon; **drive** is what the mount mechanically is, encoder pulses quantised to 0.5°, and the Due works only in drive coordinates. `trueToDrive()` converts one way (applying refraction, then the model terms), `driveToTrue()` inverts it by fixed point on `trueToDrive()` itself so the two cannot drift apart. Every goto, track and manual move goes through `trueToDrive()` before `SRTSerial::sendDriveTarget()`; every *measured* position — `/status` RA/Dec and galactic l/b, Stellarium — goes through `driveToTrue()` first. Arrival and slew-completion checks stay wholly inside the drive frame.

Consequences that must not be undone: the **observing horizon** (`settings.horizonAlt`) is a true-frame test applied *before* the transform, while the **mount limits** (`mountAltMin/Max`, `mountAzMin/Max`) are drive-frame and clamp *after* it — these were once merged into a single `effectiveTrackingHorizonAlt(mountAltMin)`, which is why neither was testable alone. The operator's `/offset` boxes are a deliberate sky-frame displacement layered on top of the calibration, never a replacement for it. **Refraction is applied whether or not a model is loaded** (it is physics, not calibration), so `sun_scan.refraction_deg()` must stay identical to `refractionDeg()` in `pointing.cpp` and the fit must subtract it, or it gets counted twice.

The ESP32 `settings.stowAlt/stowAz` is the position the dish **parks** at, and it is the one exception to the rule above: it is held in **drive** coordinates and the pointing model is deliberately bypassed on the stow path (`main.cpp` park-on-set, `/go-home`). Parking is mechanical — "leave the mount here" — not an observation. The default is zenith facing south (90, 180), and at the zenith the distinction is not academic: azimuth is degenerate there and the model's `tan(alt)` azimuth term asks for a ~50° correction that moves the beam by exactly nothing, so through the model it would park at drive azimuth 170. It is still clamped to the mount limits, and `state.targetAlt/targetAz` are deliberately *not* written by `/go-home` because those hold a true-frame target.

`settings.stowAlt/stowAz` is unrelated to the Due's `cfg.homeAlt/homeAz`, which are also drive coordinates but mean the *encoder origin* — what the limit-switch stall corresponds to. Both were once called "home"; do not merge them again.

**Pointing model wire format**: the scheduler fits it (`sun_scan.fit_pointing_model`), builds the document with `pointing_model_document()`, and POSTs it to `/pointing/apply`; the controller stores it in its own NVS namespace (so a settings reset cannot discard it) and `/pointing/clear` erases it. The document carries the fitted terms (`IE`, `IA`, `AN`, `AE`, and optionally `CA`, `NPAE`, `TF`) and nothing derived from them. Unknown term names are ignored so a richer model can be pushed to older firmware; a missing `version` or a malformed number rejects the whole document rather than applying half of it. This replaced pushing the model as a fictitious `observer_lat`/`observer_lon` plus a write to the operator's offset boxes — the observer position is now the true site position and `/apply` no longer touches it.

Scan records carry `record_version` (`sun_scan._SCAN_RECORD_VERSION`). Version 2 records hold an absolute (T, D) datum — the Sun's true position and the **reported drive position** the mount reached — so every fit is absolute and no delta composition is needed. Version 1 records are residuals against a model that is no longer knowable and are skipped by the fit. Because D is now measured rather than guessed at, the old `_MOUNT_QUANTISATION_SIGMA_DEG` floor is gone.

**ESP32 controller** (`esp32_controller_arduino/src/`): Arduino-framework PlatformIO project, two target boards (`esp32s3`, `wt32-eth01`). `state.h` is the single shared `SRTState` struct — all subsystems mutate it directly. Coordinate transforms are in `coordinates.cpp` (Julian date, GMST, precession J2000↔date, RA/Dec↔Alt/Az, galactic↔equatorial, sun/moon ephemeris). The web UI is a single embedded HTML string in `index_html.h` — heavy use of inline JS and `fetch()` against the various web_server.cpp endpoints. `/ping` and `/network` are minimal diagnostics kept ahead of the full UI handler. In `/status`, `alt`/`az` are the Due's **drive** position (the scheduler's slew check and `sun_scan.py`'s record of where a source was found both need that frame) and `true_alt`/`true_az` are the sky position they correspond to; RA/Dec and galactic l/b are computed from the latter. Goto/track endpoints reject targets below `settings.horizonAlt` before mutating state.

**Receiver scheduler runtime** (`receiver_scheduler/h1_web_scheduler.py`): Flask scheduler defaults to controller `http://192.168.50.120` (the private link above). Receiver processes always use `receiver_python_path` (radioconda by default), and the scheduler re-execs under that interpreter unless already there. A manual receiver boot is only for warm-up/testing; scheduled observations stop it before acquiring the SDR, and `/api/receiver/status` reports whether the active process is manual or observation-owned. A scheduled observation always wins: it cancels a running Sun scan/calibration day and waits (`SUN_SCAN_PREEMPT_TIMEOUT`) for the SDR before starting. `start_observation` claims the start briefly under `process_lock`, then does pointing/slew-wait unlocked and abortable via `start_abort` — do not reintroduce blocking work under that lock. Failed or short-lived starts back off after `MAX_START_FAILURES` per schedule slot. CORS is restricted to the configured controller origins — the ESP32 page's scheduler buttons only work if `srt_controller_url`/fallbacks match its origin. `/api/config` accepts only keys present in `_DEFAULT_CONFIG`, and observation filenames are contained inside the data folder.

**Outstanding work lives in GitHub issues** — `gh issue list`, repo `acrerd/21-cm-radio-telescope-v2`. Read them with `gh issue view N --comments`; each carries the finding, `file:line` references, a "Suggested approach" section, and its verification steps. They supersede the former `docs/code_review_2026-08-05.md` and `docs/POINTING_CALIBRATION_IMPROVEMENTS.md`, both removed (the review remains in git history).

Issues labelled **`needs-hardware`** (#1-#4, #7, #9) cannot be verified on a dev host — they need a bench ESP32, `tools/due_emulator.py`, or the mount, and are compile-check-only elsewhere. #5, #6 and the scheduler half of #8 are fully testable under pytest. All high-severity scheduler/sun-scan items from the review are already fixed; the ESP32 firmware findings are all still open, chiefly the missing cross-task locking (#1).

`tools/due_emulator.py` emulates the Due serial protocol over a USB-UART adapter for that bench work (wiring in its docstring; requires pyserial). Before fixing C7 in #3 (`/goto` silently enables tracking), check which callers rely on goto-implies-tracking. Several issues encode conclusions that took considerable analysis to reach — notably in #7 (do **not** add a 0.25° tracking lead; `round()` already performs it) and #8 (the tilt terms are sound; record (T, D) pairs rather than composing deltas). The reasoning is stated in each issue; re-deriving from first principles tends to reach the plausible-but-wrong version.

**Scheduler/receiver tests**: run with the radioconda Python: `python -m pytest receiver_scheduler/test_scheduler.py receiver_scheduler/test_sun_scan.py` (156 tests as of 2026-08-19). `TestFlaskAPI::test_post_config` fails on the observatory host because it makes a real HTTP call and reaches the live controller, which then syncs the observer location back over the value the test just set — a test-isolation bug, unrelated to whatever you are changing; check it against a clean tree before chasing it. The receiver GUI runs without hardware via `--sdr demo`; two receiver instances conflict over the default `h1_data.h5` file lock.

**Sun scan calibration** (`receiver_scheduler/sun_scan.py`): scan offsets are sky/cross-elevation offsets. Commands recompute the Sun position before each point, expand mount azimuth by `cos(alt)`, clamp to the usable azimuth range, and save both mount azimuth correction (`az_error_deg`) and fitted sky correction (`az_error_sky_deg`) against the mid-scan Sun position.

## Conventions

- **Editor**: never use `cat`/`echo`/`sed` from Bash; use the dedicated Read/Edit/Write tools.
- **Forward slashes** in paths even on Windows (Bash tool runs in MSYS).
- **Status line format** (don't break it — the ESP32 parser is positional):
  `Alt:%.1f Az:%.1f Ialt:%.1fA Iaz:%.1fA Status:<state> [<fault>] -> Alt:%.1f Az:%.1f Cal:ON|OFF`
- **Don't use TodoWrite** for trivial in-conversation tasks. The user has it disabled in spirit and gentle reminders to use it should be ignored unless the task is multi-step and would actually benefit.
- **Don't run destructive git commands** (`reset --hard`, `push --force`, `--no-verify`) without explicit instruction. Commits are created on user request only.
