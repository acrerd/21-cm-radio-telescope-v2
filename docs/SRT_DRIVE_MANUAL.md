# SRT Motor Driver - Operations Manual

**Version 1.2**
**Acre Road Observatory, Glasgow**

---

## Table of Contents

1. [Overview](#1-overview)
2. [Building and Uploading](#2-building-and-uploading)
3. [Hardware Connections](#3-hardware-connections)
4. [Serial Communication](#4-serial-communication)
5. [Status Messages](#5-status-messages)
6. [Commands](#6-commands)
7. [System States](#7-system-states)
8. [Fault Conditions](#8-fault-conditions)
9. [Startup and Homing](#9-startup-and-homing)
10. [Configuration](#10-configuration)
11. [Simulation Mode](#11-simulation-mode)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Overview

The SRT Motor Driver manages the alt-azimuth drive system for the Small Radio Telescope. It provides:

- Automatic homing on startup
- Position tracking via reed switch encoders (0.5 degree resolution)
- Smooth motion with acceleration/deceleration ramps
- Overcurrent and fault protection
- Serial interface for control and monitoring
- Simulation mode for testing without hardware

All positions are **drive coordinates**: encoder pulses counted from the lower limit switches, 2 per degree. Refraction and the pointing model are applied upstream by the ESP32 controller (`esp32_controller_arduino/src/pointing.cpp`); the Due knows nothing of the sky.

The firmware is `src/main.cpp`; its defaults are in `include/config.h`.

### Specifications

| Parameter | Value |
|-----------|-------|
| Microcontroller | Arduino Due (ARM Cortex-M3) |
| Position Resolution | 0.5 degrees (2 pulses per degree) |
| Altitude Range | 0 to 90 degrees |
| Azimuth Range | 0 to 355 degrees |
| Position after homing | Alt 0, Az 0 (the lower limit switches) |
| Current Limit | 5 Amps (configurable) |
| Serial Baud Rate | 115200 |
| Firmware banner | v1.2 |

---

## 2. Building and Uploading

The project uses [PlatformIO](https://platformio.org/) for building and uploading. Two build environments are provided:

| Environment | Purpose | Build Flag |
|-------------|---------|------------|
| `due` | Real hardware (default) | None |
| `simulation` | Software-only testing | `-DSIMULATION_MODE` |

A change to the homing flow or to any in-loop motor control must build cleanly in both.

### 2.1 Prerequisites

- [PlatformIO CLI](https://docs.platformio.org/en/latest/core/installation.html) or [VS Code with PlatformIO extension](https://platformio.org/install/ide?install=vscode)
- USB cable (Programming Port on the Arduino Due)

`pio` is not on PATH on the observatory machines. Use `~/.platformio/penv/bin/pio` on the Linux host and `~/.platformio/penv/Scripts/pio.exe` on Windows in place of `pio` below.

### 2.2 Building for Real Hardware

```bash
pio run -e due
```

### 2.3 Uploading to the Arduino Due

```bash
pio run -e due --target upload
```

### 2.4 Building for Simulation Mode

```bash
pio run -e simulation
```

Upload the simulation firmware the same way:

```bash
pio run -e simulation --target upload
```

### 2.5 Switching Environments in VS Code

Click the **environment selector** in the PlatformIO toolbar at the bottom of the VS Code window. Choose `due` for real hardware or `simulation` for testing. All build, upload, and monitor commands will then use the selected environment.

### 2.6 Serial Monitor

```bash
pio device monitor
```

Or use the VS Code PlatformIO serial monitor button.

---

## 3. Hardware Connections

### 3.1 Arduino Due Pinout

| Function | Pin | Wire Color | Notes |
|----------|-----|------------|-------|
| **Fault Flags** |
| Az Fault Flag 1 | 5 | Blue | Motor driver status |
| Az Fault Flag 2 | 4 | Purple | Motor driver status |
| Alt Fault Flag 1 | 7 | Blue | Motor driver status |
| Alt Fault Flag 2 | 6 | Purple | Motor driver status |
| **Motor Control** |
| Az PWM | 8 | Green | Speed control (inverted) |
| Az Direction | 9 | Yellow | Logical LOW=West, HIGH=East; pin level inverted (`AZ_DIR_INVERT`) |
| Alt PWM | 10 | Green | Speed control (inverted) |
| Alt Direction | 11 | Yellow | Logical LOW=Down, HIGH=Up; pin level inverted (`ALT_DIR_INVERT`) |
| **Driver Reset** |
| Az Reset | 22 | Black | HIGH=enabled |
| Alt Reset | 24 | White | HIGH=enabled |
| **Position Encoders** |
| Az Pulses | 12 | Yellow | Reed switch, rising edge |
| Alt Pulses | 13 | Blue | Reed switch, rising edge |
| **Current Sensors** | | | 44mV/A, bipolar, 3.3V ADC ref |
| Az Current | A1 | Black | Analog input |
| Alt Current | A0 | White | Analog input |
| **Calibrator** |
| Calibrator Control | 26 | --- | Output, active HIGH; drives nothing (section 6.5) |

Both motors are wired in reverse. `AZ_DIR_INVERT` and `ALT_DIR_INVERT` in `config.h` invert the level in software, so the direction pin is LOW when the firmware drives East/Up. Every write goes through the `AZ_DIR()` / `ALT_DIR()` macros.

### 3.2 USB Ports

The Arduino Due has two USB ports:

| Port | Location | Use |
|------|----------|-----|
| **Programming Port** | Near DC jack | Programming + serial terminal (Due CLI) |
| **Native USB Port** | Near reset button | ESP32 serial bridge (monitoring ESP32 traffic) |

**Use the Programming Port** for uploading firmware and the Due's own serial terminal.

The **Native USB Port** forwards traffic between the PC and the ESP32 via Serial1. Every byte the ESP32 sends the Due is copied to it, and anything typed on it goes to the ESP32. It does not show the Due's own output.

> **Important:** The Native USB port uses USB CDC, which requires the host to assert DTR before data will appear. PlatformIO's serial monitor (`pio device monitor`) asserts DTR automatically. Tools like Termite and PuTTY do not, and will show no output. To monitor the Native USB port, use:
>
> ```
> pio device monitor -p COM<n> -b 115200
> ```

### 3.3 WT32-ETH01 Interface (Serial1)

The Due communicates with the WT32-ETH01 (ESP32) via hardware UART:

| Due Pin | Function | WT32-ETH01 Pin |
|---------|----------|----------------|
| 18 | TX1 (transmit) | IO14 (RX) |
| 19 | RX1 (receive) | IO4 (TX) |
| GND | Ground | GND |

**Note:** Both the Due and ESP32 operate at 3.3V logic - direct connection is used, no level shifter required. Avoid IO32/IO33 on WT32-ETH01 RS-485 variants (labelled CFG/485_EN).

### 3.4 Encoders

Each axis has one reed switch, 2 pulses per degree. The ISRs (`pulseAzISR`, `pulseAltISR`) are attached on the **RISING** edge (since commit 5b25734, 2026-05-12) and take the direction from the DIR pin at the moment of the pulse.

- **Runt filter:** the ISR waits 1 ms (`PULSE_MIN_WIDTH_US`) and drops the edge if the pin no longer reads HIGH.
- **Debounce:** a pulse is counted only if at least `DEBOUNCE` (az, 50 ms) or `DEBOUNCE_ALT` (alt, 100 ms) has passed since the last *accepted* pulse. The timestamp `lastPulseAz/Alt` is updated only on accepted pulses. Updating it on every edge once made the counter lose every pulse as the pulse period approached the window.
- The altitude window sits 28% under the full-speed pulse period (128 ms at 3.9 deg/s). Each homing limit line prints the ISR's statistics (`[enc: rejected ..., min accepted ..., max rejected ..., window ...]`).
- **Backlash:** after an azimuth direction change the first `BACKLASH` x 2 pulses are absorbed uncounted (default 0).

---

## 4. Serial Communication

### 4.1 Connection Settings

| Parameter | Value |
|-----------|-------|
| Baud Rate | 115200 |
| Data Bits | 8 |
| Stop Bits | 1 |
| Parity | None |
| Line Ending | CR or LF or CRLF |

### 4.2 Output Format

Status line format. The ESP32 parser is positional: do not change it.

```
Alt:%.1f Az:%.1f Ialt:%.1fA Iaz:%.1fA Status:<state> [<fault>] -> Alt:%.1f Az:%.1f *HH
```

`[<fault>]` appears only in FAULT, `-> Alt Az` (the target) only while Slewing or Reversing. Every line ends in ` *HH`, the XOR of every character before the ` *` in hex; the controller rejects a line whose checksum fails, which is what catches a truncated or corrupted line. Until 2026-10-02 the line ended `Cal:ON|OFF` instead (issue #39), and the controller still accepts that ending.

```
Alt:45.0 Az:180.0 Ialt:0.0A Iaz:0.0A Status:Ready *7D
Alt:30.0 Az:150.0 Ialt:2.1A Iaz:1.9A Status:Slewing -> Alt:45.0 Az:180.0 *16
Alt:30.0 Az:150.0 Ialt:5.2A Iaz:0.0A Status:FAULT [Altitude motor overcurrent] *6E
```

**Fields:**

| Field | Description | Units |
|-------|-------------|-------|
| `Alt` | Current altitude position (counter / 2) | Degrees |
| `Az` | Current azimuth position (counter / 2) | Degrees |
| `Ialt` | Altitude motor current, signed (IIR filtered, ~1s settling) | Amps |
| `Iaz` | Azimuth motor current, signed and printed negated (IIR filtered, ~1s settling) | Amps |
| `Status` | System state | See section 5.1 |
| `Cal` | Calibrator pin state | ON/OFF |

Where it goes:

- **Programming port:** printed when the line less its currents differs from the last one printed, so once per 0.5 degree step while slewing and not on every current change. `STATUS` forces a print.
- **Serial1:** the full line, unsolicited, at most once per `STATUS_INTERVAL_MS` (1000 ms), and at once in answer to a `STATUS` received on Serial1.

### 4.3 Controller Protocol (Serial1)

The ESP32 sends the same commands as the programming port. The Due answers on Serial1 only with the lines below. Everything else (echoes, `Slewing to`, errors, `CONFIG`, `HELP`, `Homing: Backing off limits...`, `Homing complete`) goes to the programming port only.

| Event | Reply on Serial1 |
|-------|------------------|
| Drive command accepted | `ACK DRIVE <alt> <az>`: the target as rounded to the 0.5 degree pulse grid |
| Drive command refused | `ERR DRIVE limits` (tested first), `ERR DRIVE fault`, `ERR DRIVE homing` |
| Line not recognised | `ERR UNKNOWN <line>` (first 24 characters) |
| `STATUS` | The status line, at once, on Serial1 only. The programming port's dedup buffer is not touched. |
| Unsolicited | The status line at most once a second |
| Homing progress | The `Homing:` lines marked (S1) in section 9.2 |

The ESP32 re-sends a target not acknowledged within 2 s once, counts a second silence as lost, and reports the counts as `drive_ack` in its `/status`. Commands are not checksummed: a digit lost on the wire in a drive target is obeyed. The ACK echoes the target as the Due understood it. Serial1 lines beginning `Alt:` are ignored (a looped-back status line).

**During a homing** the Due reads both ports on every loop pass (`homingServiceSerial`, issue #34):

- `STATUS` is answered with the live line.
- `STOP` aborts the homing into `FAULT_HOMING_ABORTED`. The programming port prints `Homing ABORTED: STOP received - position unknown, RESET then HOME`; the controller sees `Status:FAULT [Homing aborted by STOP - RESET then HOME]`.
- Anything else, drive targets included, is refused with `Homing: busy - ignored '<line>'` on the programming port and on the port it came from. Lines over 31 characters are dropped. `ERR DRIVE homing` is therefore not what a drive during a homing receives.

Commands never queue behind a homing.

---

## 5. Status Messages

### 5.1 System Status Values

| Status | Description |
|--------|-------------|
| `Initializing` | System is starting up |
| `Homing` | Homing sequence running (also shown during `PROBE`) |
| `Ready` | Idle, waiting for commands |
| `Slewing` | Moving to target position |
| `Reversing` | An axis is ramping down and waiting for its coast to end before DIR flips |
| `FAULT` | Error condition - motors stopped |

### 5.2 Startup Messages

On power-up, the programming port shows the lines below. `<...>` stands for measured values. Status lines interleave with them and are omitted here, and the order of the two axes' limit lines depends on which stops first.

```
=================================
SRT Motor Driver v1.2
Acre Road Observatory, Glasgow
ESP32 Bridge: Native USB <-> Serial1
=================================

Type HELP for commands.

Calibrating current sensors...
  Az offset: <V>V
  Alt offset: <V>V
Starting homing sequence...
Homing: current zero re-measured at rest (shift az <A> A, alt <A> A)
Homing: Drive to limits...
Homing: Altitude limit reached at <n> pulses (<deg> deg) [enc: ...] I=<A>A
Homing: Azimuth limit reached at <n> pulses (<deg> deg) [enc: ...] I=<A>A det[...]
Homing: first approach - az cut edge not captured, az arrived at speed, alt arrived at speed
Homing: Backing off limits...
Homing: Re-approach limits...
Homing: Azimuth limit reached at <n> pulses (<deg> deg) [enc: ...] I=<A>A det[...]
Homing: Altitude limit reached at <n> pulses (<deg> deg) [enc: ...] I=<A>A
Homing: Az zero on the first edge after the current cut (cut <A>-><A> A, cut->edge <ms> ms, coast <n> pulses beyond it, reed HIGH at rest)
Homing: Alt zero on first edge (reed <HIGH|LOW> at rest, edge after <ms> ms, levels <levels>)
Homing: Moving to home position...

Homing complete. Position: Alt=0.0 Az=0.0
Ready. Type HELP for commands.
```

A boot homing starts its counter at 0 wherever the mount is, so it normally arrives at speed and takes the re-approach, as above. Section 9.2 gives every line.

In simulation mode, an additional banner line is shown:

```
=================================
SRT Motor Driver v1.2
Acre Road Observatory, Glasgow
*** SIMULATION MODE ***
ESP32 Bridge: Native USB <-> Serial1
=================================
```

### 5.3 Command Acknowledgments

When a valid drive command is received, the programming port shows:

```
Slewing to Alt:45.0 Az:200.0
```

preceded by `WARNING: Altitude|Azimuth xx.x outside software limits (min to max deg)` when the target is past a software limit but within the tolerance (section 6.7). A drive command from Serial1 is answered there with `ACK DRIVE` (section 4.3).

### 5.4 Error Messages

| Message | Cause |
|---------|-------|
| `ERROR: Cannot slew while in FAULT state. Power cycle to reset.` | Drive command in FAULT. `RESET` clears the fault; a power cycle is not needed. |
| `ERROR: Cannot home while in FAULT state. Power cycle to reset.` | `HOME` in FAULT. Send `RESET` first. |
| `Homing: busy - ignored '<line>'` | Any command other than `STATUS` or `STOP` during a homing |
| `ERROR: Altitude xx.x exceeds hardware limits (min to max deg)` | Alt outside the hardware limits |
| `ERROR: Azimuth xx.x exceeds hardware limits (min to max deg)` | Az outside the hardware limits |
| `ERROR: Altitude xx.x exceeds software limits + tolerance (min to max deg)` | Alt more than 4 degrees past a software limit |
| `ERROR: Azimuth xx.x exceeds software limits + tolerance (min to max deg)` | Az more than 4 degrees past a software limit |
| `Unknown command: X` then `Type HELP for commands.` | Unrecognised command word |
| `Usage: DRIVE <altitude> <azimuth>` | `DRIVE` without two numbers |
| `Usage: SET <parameter> <value>` / `ERROR: Unknown parameter 'X'` | Malformed `SET` |

---

## 6. Commands

All commands are case-insensitive.

### 6.1 Motion Commands

| Command | Description |
|---------|-------------|
| `<alt> <az>` | Slew to position (e.g., `45.0 180.0`); the line must start with a digit, `-` or `.` |
| `DRIVE <alt> <az>` | Slew to position (explicit form) |
| `HOME` | Run the homing sequence (section 9.2). Refused in FAULT. |
| `STOP` | Stop both motors at once, no ramp. Prints `STOPPED`; no fault; the mount stays put until the next drive command. During a homing it aborts the homing into FAULT (section 4.3). |
| `RESET` | Clear a fault: prints `Fault cleared. Use HOME to re-home.` and returns to Ready with the counters unchanged. Otherwise prints `No fault to reset.` |

Targets are rounded to the nearest pulse (0.5 degree).

**Examples:**
```
45 180           Slew to Alt=45, Az=180
DRIVE 45.5 200   Slew to Alt=45.5, Az=200
HOME             Re-run homing sequence
STOP             Immediate stop
RESET            Clear fault (does not re-home: send HOME next)
```

### 6.2 Information Commands

| Command | Description |
|---------|-------------|
| `STATUS` | Show current position and status |
| `CONFIG` | Show all configuration parameters |
| `HELP` or `?` | Show command help |

### 6.3 Configuration Commands

| Command | Description |
|---------|-------------|
| `SET <param> <value>` | Set a configuration parameter (RAM only) |
| `SAVE` | Save configuration to flash memory |
| `LOAD` | Load configuration from flash memory (defaults if none is valid) |
| `DEFAULTS` | Reset to the `config.h` defaults (RAM only; `SAVE` to keep) |

**SET Parameters:**

*Hardware Limits (ends of travel, section 6.7):*

| Parameter | Description | Default | Units |
|-----------|-------------|---------|-------|
| `ALTHWMIN` | Altitude hardware minimum | 0 | degrees |
| `ALTHWMAX` | Altitude hardware maximum | 90 | degrees |
| `AZHWMIN` | Azimuth hardware minimum | 0 | degrees |
| `AZHWMAX` | Azimuth hardware maximum | 355 | degrees |

*Software Limits (operational, inside hardware limits):*

| Parameter | Description | Default | Units |
|-----------|-------------|---------|-------|
| `ALTMIN` | Altitude software minimum | 0 | degrees |
| `ALTMAX` | Altitude software maximum | 90 | degrees |
| `AZMIN` | Azimuth software minimum | 2 | degrees |
| `AZMAX` | Azimuth software maximum | 353 | degrees |

*Other Parameters:*

| Parameter | Description | Default | Units |
|-----------|-------------|---------|-------|
| `HOMEALT` | Altitude the axis drives to after zeroing, and reads there | 0 | degrees |
| `HOMEAZ` | Azimuth the axis drives to after zeroing, and reads there | 0 | degrees |
| `RAMPUP` | Acceleration time | 500 | ms |
| `RAMPDOWN` | Deceleration distance | 7 | degrees |
| `STOPRAMP` | Reversal deceleration time | 300 | ms |
| `CURRENT` | Overcurrent threshold | 5.0 | Amps |
| `STALL` | Stall detection timeout; also the pulse silence that marks a limit switch during homing | 2000 | ms |
| `DEBOUNCE` | Azimuth encoder debounce window | 50 | ms |
| `DEBOUNCE_ALT` | Altitude encoder debounce window | 100 | ms |
| `BACKLASH` | Azimuth backlash compensation (az only) | 0.0 | degrees |

`HOMEALT`/`HOMEAZ` are drive coordinates. The limit-switch zero is counter 0 whatever they are set to; they only move the mount off it at the end of a homing (section 9.2). Leave both at 0. They are unrelated to the controller's stow position (`settings.stowAlt/stowAz` on the ESP32).

**Examples:**
```
SET AZMAX 350        Set azimuth upper software limit to 350 degrees
SET CURRENT 4.5      Set current limit to 4.5 Amps
SET DEBOUNCE_ALT 100 Set altitude encoder debounce to 100 ms
SAVE                 Save changes to flash
CONFIG               Verify settings
```

### 6.4 Diagnostic Commands

Output goes to the programming port only.

| Command | Description |
|---------|-------------|
| `XTRACE [s]` | Stream both raw current readings every loop for `s` seconds (default 60), with the zero offsets frozen at the moment of the command, so an offset the idle tracker would otherwise absorb stays visible. Header `XTRACE START ms rawAz rawAlt stateAz stateAlt posAz posAlt`, then `X ...` lines, then `XTRACE END`. Drive one axis meanwhile and watch the other's reading. |
| `PROBE [cycles]` | Azimuth limit-switch characterisation (issue #32), 1-12 cycles, default 3. Re-measures the current zero, then per cycle: creep out 3 degrees by count, settle 600 ms, creep in until pulses stop for 1 s, rest 800 ms. Ends 3 degrees out. Streams `t_ms,level,pos,raw_mA,phase` every 4 ms (phases `o`, `s`, `i`, `r`) between `PROBE START` and `PROBE END`. Start it with the azimuth near its lower limit: it does not drive there, and each creep is capped at 15 s. Altitude is untouched. Status reads `Homing` throughout; serial is not read, so `STOP` does not reach it. A driver fault or overcurrent aborts with `PROBE ABORTED: fault`. Refused in FAULT. |
| `TEST2` / `TEST2H` | Force Due pin 2 LOW / HIGH for a multimeter check (`Pin 2 forced LOW - measure with multimeter`). |

Building with `-DHOMING_TRACE` streams the azimuth creep of every homing on the programming port: `T <ms> <counter> <reed> <signed A> <armed><cut> <low count>`.

### 6.5 Calibrator Commands

Removed on 2026-10-02 (issue #39): the noise diode the `CAL` command switched was gone, and pin 26 is now simply held low. `CAL` is an unknown command.

### 6.6 Motion Profile

The system uses smooth motion profiles to prevent structural oscillation:

- **Start:** an axis leaves idle only when a drive command has set a new target (`executeDrive`). Stray encoder pulses while idle are not chased.
- **Acceleration:** linear ramp from `PWM_MIN_SPEED` (100) to full speed (0) over `RAMPUP` (default 500 ms)
- **Cruise:** Full speed
- **Deceleration:** quadratic ramp over `RAMPDOWN` (default 7 degrees = 14 pulses): PWM = 218 - (pulses remaining)^2, clamped to 0-100
- **Finish:** at the target count the PWM stops. After 50 ms the reed pin is sampled 10 times over 1 ms; if it is not steadily LOW the axis creeps at `PWM_MIN_SPEED` until it is, for at most 2 s.
- **Reversal:** a target on the other side ramps the PWM down linearly over `STOPRAMP` (default 300 ms). DIR flips only once no pulse has arrived for `REVERSAL_SETTLE_MS` (400 ms), bounded by `STOPRAMP` + the stall timeout. `Status:Reversing` covers the ramp and the wait. The ISR signs a pulse by the DIR pin, so a coast pulse after the flip would count backwards (issue #47, flashed 2026-09-24).

Commands can be sent at any time, even while moving. This enables smooth tracking of celestial sources with continuous position updates. Motion control runs only outside HOMING and FAULT.

### 6.7 Position Limits (Two-Tier System)

The controller uses a two-tier limit system for safe operation.

**Hardware Limits** (ends of travel):

| Axis | Minimum | Maximum |
|------|---------|---------|
| Altitude | 0 degrees, limit switch | 90 degrees, **encoder count only, no switch** |
| Azimuth | 0 degrees, limit switch | 355 degrees, limit switch |

The lower limit switches are microswitches that cut the motor current in the driving direction. They are not mechanical stop faces. The firmware sees one as pulses ceasing, and homing uses them as the encoder zero.

There is no switch at the altitude maximum. Every limit is a comparison against the counter, so all four constrain where the firmware *believes* the dish is. At the altitude maximum nothing else backs that up: after lost counts it has been exceeded (2026-08-21, found radiometrically hours later; issue #16). A target inside these limits is validated against the count, not safe by construction.

**Software Limits** (operational, inside hardware limits):

| Axis | Minimum | Maximum |
|------|---------|---------|
| Altitude | 0 degrees | 90 degrees |
| Azimuth | 2 degrees | 353 degrees |

Normal operation stays within software limits. In azimuth they leave a 2-degree margin from the hardware limits; in altitude they coincide.

**Software Limit Tolerance** (4 degrees, `SOFTWARE_LIMIT_TOLERANCE`):

A target up to 4 degrees beyond a software limit is accepted with a `WARNING`, but never beyond a hardware limit. Targets beyond software limits + tolerance are rejected.

---

## 7. System States

```
            boot               complete
   INIT ----------> HOMING ------------------> READY <--------+
                     ^  |                      |  ^           |
                HOME |  | fault, STOP    drive |  | arrived   | RESET
                     |  v                      v  |           |
                  READY FAULT <---- fault --- SLEWING         |
                        |                                     |
                        +-------------------------------------+
```

READY also goes to FAULT on a position-bounds or driver-flag fault. `RESET` returns FAULT to READY with the counters unchanged; `HOME` is refused until it has.

### State Descriptions

| State | Firmware state | Status string | Motors |
|-------|----------------|---------------|--------|
| **INIT** | `STATE_INIT` | `Initializing` | Stopped |
| **HOMING** | `STATE_HOMING` | `Homing` | Running |
| **READY** | `STATE_IDLE` | `Ready` | Stopped |
| **SLEWING** | `STATE_DRIVING` | `Slewing` or `Reversing` | Running |
| **FAULT** | `STATE_FAULT` | `FAULT` | Stopped |

---

## 8. Fault Conditions

When a fault occurs, the system immediately stops both motors and enters the FAULT state. The fault is reported in the status output. Outside a homing the main loop checks, in order: position bounds, driver fault flags, overcurrent, and (only while Slewing) stall. The homing loops run their own driver-flag and overcurrent checks (section 8.6) and no bounds check.

### 8.1 Motor Driver Faults

These are detected via the motor driver's fault flag pins. The flags are noisy at motor start and stop (inductive kickback, supply sag), so the same fault must persist for 5 consecutive checks (`FAULT_FLAG_PERSIST_COUNT`) before it is reported.

| Fault | Description | Likely Cause |
|-------|-------------|--------------|
| `Azimuth motor short circuit` | FF1=LOW, FF2=HIGH | Wiring short, motor failure |
| `Altitude motor short circuit` | FF1=LOW, FF2=HIGH | Wiring short, motor failure |
| `Azimuth motor overheating` | FF1=HIGH, FF2=LOW | Prolonged high-current operation |
| `Altitude motor overheating` | FF1=HIGH, FF2=LOW | Prolonged high-current operation |
| `Azimuth motor undervoltage` | FF1=HIGH, FF2=HIGH | Power supply issue |
| `Altitude motor undervoltage` | FF1=HIGH, FF2=HIGH | Power supply issue |

### 8.2 Current Faults

| Fault | Description | Threshold |
|-------|-------------|-----------|
| `Azimuth motor overcurrent` | Motor drawing too much current | > 5.0 Amps |
| `Altitude motor overcurrent` | Motor drawing too much current | > 5.0 Amps |

The test uses the filtered current (IIR, alpha 0.02 per 10 ms loop, ~0.5 s time constant), only while that axis is moving. The sensor zero is measured at boot (50 samples), re-measured at rest at the start of every homing (64 samples), and tracked slowly once an axis has been idle for 2 s outside a homing.

**Possible causes:**
- Mechanical obstruction
- Binding in gears or drive train
- Motor failure
- Overloaded drive

### 8.3 Stall Faults

| Fault | Description | Detection |
|-------|-------------|-----------|
| `Azimuth motor stalled` | No encoder pulses while driving | 2 seconds timeout |
| `Altitude motor stalled` | No encoder pulses while driving | 2 seconds timeout |

A stall needs both the time since the drive started and the time since the last accepted pulse to exceed `STALL`, so a stale pulse timestamp from a long idle cannot fault a slew the moment it starts. Every stall test reads the pulse time before the clock (`msSincePulse`). Stalls are checked only while Slewing, never during a homing, where the pulse silence is how a limit switch is found.

**Arrived at limit:** an axis that stalls while driving negative with its count within 2 degrees of its hardware minimum is taken to be at the switch. No fault is raised: the counter is set to the minimum, the target is clamped to it, and `Az limit reached (snap to limit)` or `Alt limit reached (snap to limit)` is printed.

**Possible causes:**
- Motor not turning
- Encoder failure
- Belt/coupling slipped
- Mechanical jam

### 8.4 Position Bounds Faults

| Fault | Description | Detection |
|-------|-------------|-----------|
| `Azimuth position out of bounds` | Position exceeds physical limits | > 5 deg beyond hardware limit |
| `Altitude position out of bounds` | Position exceeds physical limits | > 5 deg beyond hardware limit |

**Possible causes:**
- Encoder noise adding false pulses
- Mechanical hard stop broken
- Encoder missed pulses accumulating over time
- Motor drove past limit (overcurrent didn't trigger)

This is a sanity check on the counter: if the counted position exceeds what is physically possible, something is wrong and the system stops immediately. It cannot catch a dish that has physically passed 90 degrees in altitude while the counter reads less (section 6.7).

### 8.5 Recovery from Faults

**Option A: RESET, then HOME**

1. Investigate and resolve the fault cause
2. Send `RESET`. The firmware prints `Fault cleared. Use HOME to re-home.` and returns to Ready with the counters unchanged.
3. Send `HOME`. After a stall, a bounds fault or an aborted homing the counters cannot be trusted until it has run.

**Option B: Power cycle**

1. Turn off the controller
2. Investigate and resolve the fault cause
3. Turn on the controller
4. The system will re-home automatically

### 8.6 Homing Faults

| Fault | Printed on the programming port | Cause |
|-------|---------------------------------|-------|
| `Homing aborted by STOP - RESET then HOME` | `Homing ABORTED: STOP received - position unknown, RESET then HOME` | `STOP` during a homing |
| `Azimuth motor stalled` / `Altitude motor stalled` | `Homing ABORTED: Az stall during back-off` (or `Alt`) | No pulse for `STALL` while backing off |
| | `Homing ABORTED: no encoder edge leaving the stop` | No edge within 3x `STALL` on the zeroing creep |
| | `Homing ABORTED: Azimuth motor not responding` (or `Altitude`) | No pulse for `STALL` on the drive to `HOMEALT/HOMEAZ` |
| Driver flag or overcurrent | `Homing ABORTED: <fault>` | As sections 8.1 and 8.2 |

---

## 9. Startup and Homing

### 9.1 Startup Sequence

On power-up, the system performs the following sequence:

1. **Initialize hardware**
   - Configure all GPIO pins
   - Enable motor drivers (reset pins HIGH)
   - Set motors to stopped state, DIR positive, pin 26 LOW
   - Attach the encoder interrupts (RISING), set the ADC to 12 bits

2. **Initialize serial ports**
   - Programming port (USB) at 115200 baud
   - Serial1 (pins 18/19) at 115200 baud
   - Native USB bridge to Serial1

3. **Load configuration** from flash, or the `config.h` defaults if flash holds no valid configuration

4. **Measure the current-sensor zero** with the motors off (50 samples, 10 ms apart)

5. **Homing sequence** (section 9.2)

6. **Enter READY state**
   - Output status (section 4.2)
   - Accept commands

**Note:** a homing takes up to about a minute. Skipping the re-approach saves about 40 s.

### 9.2 Homing Sequence

`performHoming()` in `src/main.cpp`. Lines marked (S1) also go to Serial1.

1. **Current zero.** Motors stopped, 500 ms wait, then 64 samples of each sensor are taken as the zero for this homing (S1):
   `Homing: current zero re-measured at rest (shift az <A> A, alt <A> A)`.
   The idle zero tracker does not run during a homing.

2. **First approach** (`driveToLimits`), `Homing: Drive to limits...` (S1).
   - Both axes drive negative with the ramp-up profile.
   - Inside a band of +-5 pulses (+-2.5 degrees, `HOMING_SLOW_APPROACH_PULSES`) of counter zero, each axis stops for 400 ms (`HOMING_SLOW_BRAKE_MS`) and then creeps at `PWM_MIN_SPEED`. Met at full speed, the coast past the current cut scattered the rest over most of a magnet pitch (issue #32).
   - An axis is at its limit when no pulse has passed its debounce for `STALL`. Each stop prints (S1):
     `Homing: Azimuth limit reached at <n> pulses (<deg> deg) [enc: ...] I=<A>A det[armed= creep= cut= low= edges= cutToStall= brake=]`
     and the same for altitude, without `det[...]`.
   - On the first approach `<n>` is a fixed switch-detection offset plus any count drift since the previous homing. The offset is normally −2.0 to +0.5° (most often alt −1.0°, az −0.5°, whether the last homing was an hour or a day ago); only a reading outside that range is drift.
   - A boot homing starts at counter 0 wherever the mount is: it creeps its first 2.5 degrees, then runs at speed.

3. **Azimuth cut edge.** While azimuth creeps, the raw current (8 ADC conversions averaged) is read every pass.
   - The detector arms when the current exceeds 0.3 A (`HOMING_CREEP_CURRENT_A`), taking the driving sign from the creep's opening surge.
   - It books the cut when the **signed** driving-direction current falls below 0.3 A for three consecutive readings, at the counter and time of the first of the three. Signed, because the cut does not take the reading to zero: it flips it, and a reverse transient of 0.5-1 A follows every cut for 150-400 ms.
   - The first counted edge after the cut is the azimuth reference (`azCutEdgePosition`). The cut is repeatable; the 0.3-0.7 degree coast after it is not.
   - More than two edges after the cut means the axis was still driving (S1):
     `Homing: Az <n> edges followed the supposed current cut - not a cut, capture discarded`.

4. **Re-approach, or not.** The first approach reports (S1):
   `Homing: first approach - az cut edge captured|not captured, az arrived creeping|at speed, alt arrived creeping|at speed`.
   - If the cut edge was captured and altitude met its switch at creep, the first approach has done everything a second would (issue #33) (S1):
     `Homing: Re-approach skipped - both axes met the switch at creep and the azimuth cut edge was captured`.
   - Otherwise `Homing: Backing off limits...`. Both counters are set to 0 and both axes drive 5 degrees positive by count. Then the firmware waits until no pulse has arrived for 400 ms (`HOMING_SETTLE_MS`, bounded by `STALL`): the ISR signs a pulse by the DIR pin, so a coast pulse after the reversal would count backwards.
   - Then `Homing: Re-approach limits...` (S1) and a second approach as in steps 2-3.

5. **Zero** (`refineZeroPositiveEdge`). The reed level at rest is read on each axis (5 samples over 50 ms, majority).
   - **Azimuth, cut edge captured:** the counter is set to the pulses coasted past the cut edge (S1):
     `Homing: Az zero on the first edge after the current cut (cut <A>-><A> A, cut->edge <ms> ms, coast <n> pulses beyond it, reed HIGH|LOW at rest)`.
     A HIGH rest keeps that counter. A LOW rest coasted through the cut dwell: the axis creeps positive to the first counted edge, the dwell's entry, and takes the counter there (S1):
     `Homing: Az counter set at the cut dwell's entry (reed LOW at rest, edge after <ms> ms, levels <levels>)`.
   - **Azimuth, no capture:** `Homing: Az no edge followed the current cut - zeroing on the first edge of the creep` (S1). The axis creeps positive and zeroes on the first edge (S1):
     `Homing: Az zero on first edge of the creep (fallback; reed ... at rest, edge after <ms> ms, levels <levels>)`.
   - **Altitude** always creeps positive and zeroes on its first edge (S1):
     `Homing: Alt zero on first edge (reed HIGH|LOW at rest, edge after <ms> ms, levels <levels>)`.
   - Zeroing on a positive-going edge puts the zero on the same edges tracking reads. `<levels>` is the reed's level history during the creep (`L>H@<ms>...`).

6. **Home offset.** `Homing: Moving to home position...`. Each axis drives `HOMEALT`/`HOMEAZ` x 2 pulses positive, and the counters are then set to `HOMEALT`/`HOMEAZ`. Both default to 0, so nothing moves and the mount reads (0, 0) at its zero:
   ```
   Homing complete. Position: Alt=0.0 Az=0.0
   Ready. Type HELP for commands.
   ```

The first-approach counters and the skip are how the controller and scheduler judge a homing: the ESP32 reports `reapproach_skipped`, and the scheduler accepts null second-approach counters on that word alone.

---

## 10. Configuration

### 10.1 Defaults and Flash

`Config cfg` lives in RAM. At boot it is read from flash (DueFlashStorage) and checked against the magic `0x53525431` ("SRT1") and a checksum. The `DEFAULT_*` values in `include/config.h` are used only when that check fails, or after `DEFAULTS`. A configuration saved in flash overrides a changed default in a rebuilt firmware until `DEFAULTS` and `SAVE` are sent. Check `CONFIG` on the mount rather than reading `config.h`.

### 10.2 Settable Defaults

| Define | Value | SET parameter |
|--------|-------|---------------|
| `DEFAULT_ALT_HW_MIN` | 0.0 | `ALTHWMIN` |
| `DEFAULT_ALT_HW_MAX` | 90.0 | `ALTHWMAX` |
| `DEFAULT_AZ_HW_MIN` | 0.0 | `AZHWMIN` |
| `DEFAULT_AZ_HW_MAX` | 355.0 | `AZHWMAX` |
| `DEFAULT_ALT_MIN` | 0.0 | `ALTMIN` |
| `DEFAULT_ALT_MAX` | 90.0 | `ALTMAX` |
| `DEFAULT_AZ_MIN` | 2.0 | `AZMIN` |
| `DEFAULT_AZ_MAX` | 353.0 | `AZMAX` |
| `DEFAULT_HOME_ALT` | 0.0 | `HOMEALT` |
| `DEFAULT_HOME_AZ` | 0.0 | `HOMEAZ` |
| `DEFAULT_RAMP_UP_MS` | 500 | `RAMPUP` |
| `DEFAULT_RAMP_DOWN_DEG` | 7.0 | `RAMPDOWN` |
| `DEFAULT_STOP_RAMP_MS` | 300 | `STOPRAMP` |
| `DEFAULT_CURRENT_LIMIT` | 5.0 | `CURRENT` |
| `DEFAULT_STALL_TIMEOUT` | 2000 (`SIM_STALL_TIMEOUT_MS` 200 in simulation) | `STALL` |
| `DEFAULT_DEBOUNCE_MS` | 50 | `DEBOUNCE` |
| `DEFAULT_DEBOUNCE_ALT_MS` | 100 | `DEBOUNCE_ALT` |
| `DEFAULT_BACKLASH_AZ` | 0.0 | `BACKLASH` |

A new tunable needs all five steps: the `#define`, a `Config` field, `loadDefaults()`, `processSetCommand()`, and `showConfig()`/`showHelp()`. A field `loadDefaults()` misses holds garbage until `SAVE`.

### 10.3 Fixed Values (rebuild to change)

In `include/config.h`:

| Define | Value | Meaning |
|--------|-------|---------|
| `PULSES_PER_DEGREE` | 2 | Encoder resolution |
| `SOFTWARE_LIMIT_TOLERANCE` | 4.0 deg | Allowed excursion past a software limit |
| `REVERSAL_SETTLE_MS` | 400 | Pulse silence before DIR flips on a reversal |
| `HOMING_SETTLE_MS` | 400 | Pulse silence after the homing back-off |
| `HOMING_SLOW_APPROACH_PULSES` | 5 | Creep band around the expected zero |
| `HOMING_SLOW_BRAKE_MS` | 400 | Stop before creeping into the switch |
| `HOMING_CREEP_CURRENT_A` | 0.3 | Driving-direction current that separates driving from cut |
| `PWM_STOP` / `PWM_FULL_SPEED` / `PWM_MIN_SPEED` | 255 / 0 / 100 | Inverted PWM |
| `CURRENT_SENSOR_OFFSET_V` / `CURRENT_SENSOR_SENSITIVITY` | 1.65 V / 0.044 V/A | Sensor nominal zero and scale |
| `STATUS_INTERVAL_MS` | 1000 | Unsolicited Serial1 status period |
| `MAIN_LOOP_DELAY_MS` | 10 | Main loop period |
| `ENABLE_SERIAL1` | 1 | Set to 0 to disable Serial1 |

In `src/main.cpp`: `FAULT_FLAG_PERSIST_COUNT` 5, `POSITION_BOUNDS_TOLERANCE` 5.0 deg, `PULSE_MIN_WIDTH_US` 1000, `OFFSET_TRACK_IDLE_LOOPS` 200 (2 s), `CURRENT_FILTER_ALPHA` 0.02.

After changing any of these, rebuild and upload:

```bash
pio run -e due --target upload
```

---

## 11. Simulation Mode

Simulation mode allows you to test the full control system - state machine, motion profiles, serial commands, homing sequence, position tracking, and safety logic - without any drive hardware connected. It is enabled at compile time via the `simulation` PlatformIO environment.

### 11.1 How It Works

When built with `-DSIMULATION_MODE`, the firmware replaces the motor, encoder and fault-flag I/O with software stubs using preprocessor macros. The core control logic (state machine, motion profiles, serial commands) runs unmodified - only the lowest-level hardware interface is swapped out.

#### Position Feedback (Pulse Simulation)

On real hardware, reed switch encoders generate interrupt-driven pulses as the motors turn (2 pulses per degree, RISING edge). The ISRs read the motor direction pin to determine whether to increment or decrement the position counter.

In simulation mode, a `simulatePulses()` function replaces the interrupt-driven feedback. It runs every loop iteration (10ms) and:

1. Reads the **shadow PWM value** to determine motor speed. The PWM uses inverted logic (255 = stopped, 0 = full speed), so the speed fraction is calculated as:

   ```
   speed = (255 - pwm) / 255
   ```

2. Reads the **shadow direction state** to determine whether to increment or decrement position.

3. **Accumulates fractional pulses** over real elapsed time using the configured maximum speed (`SIM_MAX_SPEED_DEG_S`, default 180 deg/s):

   ```
   pulses_this_tick = speed * max_pulse_rate * dt
   ```

   A floating-point accumulator tracks sub-pulse fractions. When the accumulator reaches 1.0, a whole pulse is generated and the position counter is updated, exactly as the real ISR would.

4. **Updates the `lastPulse` timestamp** with each generated pulse, which keeps the stall detection logic happy (no false stall faults while the simulated motor is running).

5. **Simulates physical hard stops** at the configured **hardware** limits (`cfg.azHwMin`, `cfg.azHwMax`, `cfg.altHwMin`, `cfg.altHwMax`). When the simulated position reaches a limit, pulse generation stops. This naturally causes the stall detection timeout to fire - exactly as a real motor stalling against a limit switch would. This is what makes the homing sequence work in simulation: the motors "drive" toward the limits, "stall" when they arrive, and the homing logic detects it normally.

The `simulatePulses()` function is called from every loop that drives a motor, nine places in all:
- The main `loop()`, after `updateMotion()`
- `driveToLimits()` (both approaches)
- `backOffFromLimits()`: the drive and the settle wait
- `refineZeroPositiveEdge()`
- The home-offset loop in `performHoming()`
- The three creep loops of `probeAzSwitch()` (`PROBE`)

#### Current Sensing

The current sensors (44mV/A, bipolar) are read via the Due's 12-bit ADC (3.3V reference). In simulation mode, the current sensor pins (A0, A1) read real hardware — only motor control, encoder, and fault flag I/O is simulated. This allows testing the current sensing and calibration with real sensors while the motor control loop runs in simulation.

With no motor current the azimuth cut detector does not arm, so a simulated homing takes the re-approach and the fallback creep zero. The cut-edge capture and the re-approach skip are not exercised.

#### Fault Flag Pins

The motor driver fault flags (FF1/FF2 for each axis) are read via `digitalRead()`. In simulation mode, `digitalRead()` returns `LOW` for the four fault-flag pins; since both flags LOW indicates normal operation, no driver faults will be detected. Other pins, the encoder pins included, are read from the hardware, so the reed-level reads in homing (`reedRestsHigh`) and in the end-of-slew finish see whatever the bench pins show.

#### GPIO and Interrupts

`attachInterrupt()` becomes a no-op. `pinMode()` is a no-op for the motor, encoder, fault-flag and driver-reset pins and real for all others. Motor control writes (`analogWrite` for PWM, `digitalWrite` for direction) are intercepted and stored in shadow variables; other `digitalWrite` calls (driver reset, calibrator) reach the pins. `analogReadResolution()` is not overridden: the current sensors need the real 12-bit ADC.

### 11.2 Simulation Parameters

These are defined in `include/config.h` under the `SIMULATION_MODE` guard:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SIM_MAX_SPEED_DEG_S` | 180.0 | Maximum simulated motor speed in degrees/second at full PWM |
| `SIM_INITIAL_AZ_DEG` | 20.0 | Simulated starting azimuth before homing (degrees) |
| `SIM_INITIAL_ALT_DEG` | 10.0 | Simulated starting altitude before homing (degrees) |
| `SIM_STALL_TIMEOUT_MS` | 200 | Stall timeout loaded by `loadDefaults()` in simulation |

The initial position values set how far the simulated telescope must drive during homing before it reaches the limits. The defaults sit close to the limits so a simulated homing is short; larger values give a longer homing test. A configuration saved to flash by a hardware build carries its own `STALL` (2000 ms) into a simulation build until `DEFAULTS`.

### 11.3 What Is Tested vs. What Is Not

| Tested in simulation | Not tested |
|----------------------|------------|
| State machine transitions (INIT, HOMING, IDLE, DRIVING, FAULT) | Real EMI/noise on reed switch signals |
| Motion profiles (acceleration, cruise, deceleration, reversal) | Actual motor driver behaviour |
| Homing sequence (limit detection, back-off, re-approach, creep zero) | Current-cut detection, cut-edge zero, re-approach skip |
| Serial command parsing and dispatch | Real current draw and inrush |
| Position limit enforcement | Mechanical stall characteristics |
| Stall detection logic (triggers naturally at simulated limits) | Interrupt timing edge cases |
| Configuration management (SET/SAVE/LOAD/DEFAULTS) | Motor driver fault flag hardware, flash storage wear |

### 11.4 Example Session

```
=================================
SRT Motor Driver v1.2
Acre Road Observatory, Glasgow
*** SIMULATION MODE ***
ESP32 Bridge: Native USB <-> Serial1
=================================

Type HELP for commands.

Calibrating current sensors...
  Az offset: <V>V
  Alt offset: <V>V
Starting homing sequence...
Homing: current zero re-measured at rest (shift az <A> A, alt <A> A)
Homing: Drive to limits...
...
Homing: Az zero on first edge of the creep (fallback; ...)
Homing: Alt zero on first edge (...)
Homing: Moving to home position...

Homing complete. Position: Alt=0.0 Az=0.0
Ready. Type HELP for commands.

Alt:0.0 Az:0.0 Ialt:0.0A Iaz:0.0A Status:Ready *45
> 45 270
Slewing to Alt:45.0 Az:270.0
Alt:0.5 Az:0.5 Ialt:0.0A Iaz:0.0A Status:Slewing -> Alt:45.0 Az:270.0 *26
Alt:1.0 Az:1.0 Ialt:0.0A Iaz:0.0A Status:Slewing -> Alt:45.0 Az:270.0 *26
...
Alt:45.0 Az:270.0 Ialt:0.0A Iaz:0.0A Status:Ready *71
```

---

## 12. Troubleshooting

### 12.1 System Does Not Start

| Symptom | Check |
|---------|-------|
| No serial output | USB cable, correct port selected |
| Stuck in homing | Motor connections, encoder connections. `STATUS` is answered during a homing; `STOP` aborts it (then `RESET`, `HOME`). |
| Immediate fault | Motor driver power, fault flag wiring |

### 12.2 Motors Do Not Move

| Symptom | Check |
|---------|-------|
| PWM output but no motion | Motor driver reset pins (should be HIGH) |
| No PWM output | PWM pin connections (pins 8, 10) |
| Motors run backwards | Direction pin wiring (pins 9, 11), `AZ_DIR_INVERT` / `ALT_DIR_INVERT` |

### 12.3 Position Errors

| Symptom | Check |
|---------|-------|
| Wrong position after homing | The homing lines (section 9.2): first-approach counter, `cut`/`edges` in `det[...]`, reed level at rest. `HOMEALT`/`HOMEAZ` should be 0. |
| Position drifts | Encoder debounce (`DEBOUNCE`, `DEBOUNCE_ALT`): read the `[enc: ...]` statistics on the homing limit lines before changing either |
| Overshoots target | `RAMPDOWN` (increase value) |
| Jerky motion | `RAMPUP` (increase value) |

### 12.4 False Overcurrent Faults

| Symptom | Check |
|---------|-------|
| Faults with low actual current | Sensor zero: measured at boot and at each homing (`Homing: current zero re-measured at rest (shift ...)`); `XTRACE` shows the raw readings against a frozen zero |
| Faults only at startup | Motor inrush; the limit is tested on the filtered current |

### 12.5 Serial Communication Issues

| Symptom | Check |
|---------|-------|
| No response to commands | Line ending settings (need CR or LF) |
| Garbled output | Baud rate (must be 115200) |
| ESP32 not receiving | TX/RX swapped (Due 18 -> IO14, Due 19 <- IO4), common ground |
| Controller reports drive targets lost | `ERR UNKNOWN` / `ERR DRIVE` replies and the controller's `drive_ack` counts (section 4.3) |

---

## Appendix A: Quick Reference

### Commands

| Command | Example | Description |
|---------|---------|-------------|
| Go to position | `45 180` or `DRIVE 45 180` | Move to Alt=45, Az=180 |
| Home | `HOME` | Run the homing sequence |
| Stop | `STOP` | Stop both motors; aborts a homing |
| Reset | `RESET` | Clear a fault (does not re-home) |
| Status | `STATUS` | Print the status line |
| Configuration | `CONFIG`, `SET <p> <v>`, `SAVE`, `LOAD`, `DEFAULTS` | Section 6.3 |
| Diagnostics | `XTRACE [s]`, `PROBE [n]`, `TEST2`, `TEST2H` | Section 6.4 |

### Status Output

```
Alt:<deg> Az:<deg> Ialt:<A>A Iaz:<A>A Status:<state> [<fault>] [-> Alt:<deg> Az:<deg>] *HH
```

### Serial1 Replies

```
ACK DRIVE <alt> <az>
ERR DRIVE fault|homing|limits
ERR UNKNOWN <line>
Homing: busy - ignored '<line>'
```

### Pin Summary

```
Motor Control:  8(PWM-Az), 9(DIR-Az), 10(PWM-Alt), 11(DIR-Alt)
Driver Reset:   22(Az), 24(Alt)
Encoders:       12(Az), 13(Alt)
Fault Flags:    4,5(Az), 6,7(Alt)
Current Sense:  A0(Alt), A1(Az)
Serial1:        18(TX), 19(RX)
Calibrator:     26 (active HIGH, drives nothing)
```

---

*Document revision: 1.2*
*Updated: September 2026*
