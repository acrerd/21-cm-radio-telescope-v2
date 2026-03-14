# SRT Drive Controller - Operations Manual

**Version 1.1**
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
9. [Startup Sequence](#9-startup-sequence)
10. [Configuration](#10-configuration)
11. [Simulation Mode](#11-simulation-mode)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Overview

The SRT Drive Controller manages the alt-azimuth drive system for the Small Radio Telescope. It provides:

- Automatic homing on startup
- Position tracking via reed switch encoders (0.5 degree resolution)
- Smooth motion with acceleration/deceleration ramps
- Overcurrent and fault protection
- Serial interface for control and monitoring
- Simulation mode for testing without hardware

### Specifications

| Parameter | Value |
|-----------|-------|
| Microcontroller | Arduino Due (ARM Cortex-M3) |
| Position Resolution | 0.5 degrees (2 pulses per degree) |
| Altitude Range | 0 to 90 degrees |
| Azimuth Range | 0 to 355 degrees |
| Home Position | Alt: 0, Az: 180 degrees |
| Current Limit | 5 Amps (configurable) |
| Serial Baud Rate | 115200 |

---

## 2. Building and Uploading

The project uses [PlatformIO](https://platformio.org/) for building and uploading. Two build environments are provided:

| Environment | Purpose | Build Flag |
|-------------|---------|------------|
| `due` | Real hardware (default) | None |
| `simulation` | Software-only testing | `-DSIMULATION_MODE` |

### 2.1 Prerequisites

- [PlatformIO CLI](https://docs.platformio.org/en/latest/core/installation.html) or [VS Code with PlatformIO extension](https://platformio.org/install/ide?install=vscode)
- USB cable (Programming Port on the Arduino Due)

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
| Az Direction | 9 | Yellow | LOW=West, HIGH=East |
| Alt PWM | 10 | Green | Speed control (inverted) |
| Alt Direction | 11 | Yellow | LOW=Down, HIGH=Up |
| **Driver Reset** |
| Az Reset | 22 | Black | HIGH=enabled |
| Alt Reset | 24 | White | HIGH=enabled |
| **Position Encoders** |
| Az Pulses | 12 | Yellow | Reed switch, falling edge |
| Alt Pulses | 13 | Blue | Reed switch, falling edge |
| **Current Sensors** |
| Az Current | A1 | Black | Analog input |
| Alt Current | A0 | White | Analog input |

### 3.2 USB Ports

The Arduino Due has two USB ports:

| Port | Location | Use |
|------|----------|-----|
| **Programming Port** | Near DC jack | Programming + serial terminal |
| **Native USB Port** | Near reset button | Not used |

**Use the Programming Port** for both uploading firmware and serial communication.

### 3.3 ESP32 Interface (Serial1)

For external microcontroller connections (e.g., ESP32), use the hardware UART:

| Due Pin | Function | Connect to ESP32 |
|---------|----------|------------------|
| 18 | TX1 (transmit) | RX (via level shifter) |
| 19 | RX1 (receive) | TX (via level shifter) |
| GND | Ground | GND |

**Important:** The Due operates at 3.3V logic. If your ESP32 is also 3.3V, direct connection is possible. Otherwise, use a level shifter.

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

The controller outputs a status line whenever position, state, or fault changes:

```
Alt:45.0 Az:180.0 Ialt:0.52A Iaz:0.38A Status:Ready
```

**Fields:**

| Field | Description | Units |
|-------|-------------|-------|
| `Alt` | Current altitude position | Degrees |
| `Az` | Current azimuth position | Degrees |
| `Ialt` | Altitude motor current | Amps |
| `Iaz` | Azimuth motor current | Amps |
| `Status` | System state | See section 7 |

**Additional fields when slewing:**

```
Alt:30.0 Az:150.0 Ialt:2.10A Iaz:1.85A Status:Slewing -> Alt:45.0 Az:180.0
```

The `-> Alt:xx Az:xx` shows the target position.

**Additional fields in fault state:**

```
Alt:30.0 Az:150.0 Ialt:5.20A Iaz:0.00A Status:FAULT [Altitude motor overcurrent]
```

The fault description appears in square brackets.

---

## 5. Status Messages

### 5.1 System Status Values

| Status | Description |
|--------|-------------|
| `Initializing` | System is starting up |
| `Homing` | Finding limit switches and moving to home position |
| `Ready` | Idle, waiting for commands |
| `Slewing` | Moving to target position |
| `Reversing` | Decelerating before changing direction |
| `FAULT` | Error condition - motors stopped |

### 5.2 Startup Messages

On power-up, you will see:

```
=================================
SRT Drive Controller v1.1
Acre Road Observatory, Glasgow
=================================

Starting homing sequence...
Homing: Driving to limit switches...
Homing: Altitude limit switch reached
Homing: Azimuth limit switch reached
Homing: At limit switches, moving to home position...

Homing complete. Position: Alt=0.0 Az=180.0
Ready. Type HELP for commands.
```

In simulation mode, an additional banner is shown:

```
=================================
SRT Drive Controller v1.1
Acre Road Observatory, Glasgow
*** SIMULATION MODE ***
=================================
```

### 5.3 Command Acknowledgments

When a valid command is received:

```
Slewing to Alt:45.0 Az:200.0
```

### 5.4 Error Messages

| Message | Cause |
|---------|-------|
| `ERROR: Cannot slew while in FAULT state. Power cycle to reset.` | Fault has occurred |
| `ERROR: Cannot slew while homing in progress.` | Command sent during homing |
| `ERROR: Altitude xx.x is out of range. Valid: 0 to 90 degrees` | Alt out of bounds |
| `ERROR: Azimuth xx.x is out of range. Valid: 0 to 355 degrees` | Az out of bounds |
| `ERROR: Invalid command '...'. Send: <altitude> <azimuth>` | Parse error |

---

## 6. Commands

All commands are case-insensitive.

### 6.1 Motion Commands

| Command | Description |
|---------|-------------|
| `<alt> <az>` | Slew to position (e.g., `45.0 180.0`) |
| `DRIVE <alt> <az>` | Slew to position (explicit form) |
| `HOME` | Run homing sequence |
| `STOP` | Emergency stop both motors |
| `RESET` | Clear fault and re-home |

**Examples:**
```
45 180           Slew to Alt=45, Az=180
DRIVE 45.5 200   Slew to Alt=45.5, Az=200
HOME             Re-run homing sequence
STOP             Immediate stop
RESET            Clear fault, then re-home
```

### 6.2 Information Commands

| Command | Description |
|---------|-------------|
| `STATUS` | Show current position and status |
| `CONFIG` | Show all configuration parameters |
| `HELP` | Show command help |

### 6.3 Configuration Commands

| Command | Description |
|---------|-------------|
| `SET <param> <value>` | Set a configuration parameter |
| `SAVE` | Save configuration to flash memory |
| `LOAD` | Load configuration from flash memory |
| `DEFAULTS` | Reset to factory defaults |

**SET Parameters:**

*Hardware Limits (physical limit switches):*

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
| `HOMEALT` | Home altitude position | 0 | degrees |
| `HOMEAZ` | Home azimuth position | 180 | degrees |
| `RAMPUP` | Acceleration time | 500 | ms |
| `RAMPDOWN` | Deceleration distance | 7 | degrees |
| `STOPRAMP` | Reversal deceleration time | 300 | ms |
| `CURRENT` | Overcurrent threshold | 5.0 | Amps |
| `STALL` | Stall detection timeout | 2000 | ms |

**Examples:**
```
SET AZMAX 350        Set azimuth upper software limit to 350 degrees
SET CURRENT 4.5      Set current limit to 4.5 Amps
SET HOMEAZ 175       Set home azimuth to 175 degrees
SAVE                 Save changes to flash
CONFIG               Verify settings
```

### 6.4 Motion Profile

The system uses smooth motion profiles to prevent structural oscillation:

- **Acceleration:** Linear ramp over configured time (default 500ms)
- **Cruise:** Full speed
- **Deceleration:** Quadratic ramp over configured distance (default 7 degrees)
- **Reversal:** If direction change needed, decelerates over configured time (default 300ms), then reverses

Commands can be sent at any time, even while moving. This enables smooth tracking of celestial sources with continuous position updates.

### 6.5 Position Limits (Two-Tier System)

The controller uses a two-tier limit system for safe operation:

**Hardware Limits** (physical limit switches):

| Axis | Minimum | Maximum |
|------|---------|---------|
| Altitude | 0 degrees (horizon) | 90 degrees (zenith) |
| Azimuth | 0 degrees (North) | 355 degrees |

These are absolute limits where physical limit switches stop the motor. The system cannot drive beyond these.

**Software Limits** (operational, inside hardware limits):

| Axis | Minimum | Maximum |
|------|---------|---------|
| Altitude | 0 degrees | 90 degrees |
| Azimuth | 2 degrees | 353 degrees |

Normal operation stays within software limits. They provide a 2-degree safety margin from hardware stops.

**Software Limit Tolerance** (4 degrees):

The system allows brief excursions up to 4 degrees beyond software limits, but never beyond hardware limits. Commands that would exceed software limits + tolerance are rejected.

---

## 7. System States

```
                    +--------+
                    |  INIT  |
                    +---+----+
                        |
                        v
                    +--------+
                    | HOMING |-------> FAULT
                    +---+----+
                        |
                        v
          +-------> +-------+ <-------+
          |         | READY |         |
          |         +---+---+         |
          |             |             |
          |   command   |             |
          |             v             |
          |         +--------+        |
          +---------|SLEWING |--------+
             done   +---+----+
                        |
                        v fault
                    +-------+
                    | FAULT |
                    +-------+
```

### State Descriptions

| State | Description | LED Pattern |
|-------|-------------|-------------|
| **INIT** | Power-on initialization | N/A |
| **HOMING** | Finding limits and moving to home | Motors running |
| **READY** | Idle, accepting commands | Motors stopped |
| **SLEWING** | Moving to commanded position | Motors running |
| **FAULT** | Error condition, motors disabled | Stopped |

---

## 8. Fault Conditions

When a fault occurs, the system immediately stops both motors and enters the FAULT state. The fault is reported in the status output.

### 8.1 Motor Driver Faults

These are detected via the motor driver's fault flag pins:

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

This is a sanity check: if the counted position exceeds what is physically possible, something is wrong and the system stops immediately.

### 8.5 Recovery from Faults

**Option A: Use the RESET command**

1. Investigate and resolve the fault cause
2. Send `RESET` via serial
3. The system will clear the fault and re-home

**Option B: Power cycle**

1. Turn off the controller
2. Investigate and resolve the fault cause
3. Turn on the controller
4. The system will re-home automatically

---

## 9. Startup Sequence

On power-up, the system performs the following sequence:

1. **Initialize hardware**
   - Configure all GPIO pins
   - Enable motor drivers
   - Set motors to stopped state

2. **Initialize serial ports**
   - Programming port (USB) at 115200 baud
   - Serial1 (pins 18/19) at 115200 baud (if enabled)

3. **Homing sequence**
   - Drive both motors toward limit switches (Az West, Alt Down)
   - Wait for each motor to stop (2 second timeout = at limit)
   - Reset position counters to 0
   - Drive to home position (Az: 180 degrees)
   - Set position to home coordinates

4. **Enter READY state**
   - Output status on position/state changes
   - Accept commands

**Note:** The homing sequence takes approximately 30-60 seconds depending on the starting position.

---

## 10. Configuration

All configurable parameters are in `include/config.h`. The most commonly adjusted parameters:

### 10.1 Position Limits

```c
#define ALT_MIN_DEG         0.0     // Altitude lower limit
#define ALT_MAX_DEG         90.0    // Altitude upper limit
#define AZ_MIN_DEG          0.0     // Azimuth lower limit
#define AZ_MAX_DEG          355.0   // Azimuth upper limit
```

### 10.2 Home Position

```c
#define HOME_ALT_DEG        0.0     // Altitude home position
#define HOME_AZ_DEG         180.0   // Azimuth home position

// Pulses to drive from limits to home
#define HOME_ALT_OFFSET_PULSES  0       // (0 - 0) * 2 = 0
#define HOME_AZ_OFFSET_PULSES   360     // (180 - 0) * 2 = 360
```

### 10.3 Safety Limits

```c
#define CURRENT_LIMIT_AMPS  5.0     // Stop if current exceeds this
#define STALL_TIMEOUT_MS    2000    // No pulses = stalled
```

### 10.4 Motion Profile

```c
#define RAMP_UP_TIME_MS     500     // Acceleration time
#define RAMP_DOWN_PULSES    14      // Start decelerating at this distance
```

### 10.5 Serial1 Enable

```c
#define ENABLE_SERIAL1      1       // Set to 0 to disable Serial1
```

After changing any configuration, rebuild and upload:

```bash
pio run --target upload
```

---

## 11. Simulation Mode

Simulation mode allows you to test the full control system - state machine, motion profiles, serial commands, homing sequence, position tracking, and safety logic - without any drive hardware connected. It is enabled at compile time via the `simulation` PlatformIO environment.

### 11.1 How It Works

When built with `-DSIMULATION_MODE`, the firmware replaces all hardware I/O with software stubs using preprocessor macros. The core control logic (state machine, motion profiles, serial commands) runs unmodified - only the lowest-level hardware interface is swapped out.

#### Position Feedback (Pulse Simulation)

On real hardware, reed switch encoders generate interrupt-driven pulses as the motors turn (2 pulses per degree, FALLING edge). The ISRs read the motor direction pin to determine whether to increment or decrement the position counter.

In simulation mode, a `simulatePulses()` function replaces the interrupt-driven feedback. It runs every loop iteration (10ms) and:

1. Reads the **shadow PWM value** to determine motor speed. The PWM uses inverted logic (255 = stopped, 0 = full speed), so the speed fraction is calculated as:

   ```
   speed = (255 - pwm) / 255
   ```

2. Reads the **shadow direction state** to determine whether to increment or decrement position.

3. **Accumulates fractional pulses** over real elapsed time using the configured maximum speed (`SIM_MAX_SPEED_DEG_S`, default 6 deg/s):

   ```
   pulses_this_tick = speed * max_pulse_rate * dt
   ```

   A floating-point accumulator tracks sub-pulse fractions. When the accumulator reaches 1.0, a whole pulse is generated and the position counter is updated, exactly as the real ISR would.

4. **Updates the `lastPulse` timestamp** with each generated pulse, which keeps the stall detection logic happy (no false stall faults while the simulated motor is running).

5. **Simulates physical hard stops** at the configured position limits (`cfg.azMin`, `cfg.azMax`, `cfg.altMin`, `cfg.altMax`). When the simulated position reaches a limit, pulse generation stops. This naturally causes the stall detection timeout to fire - exactly as a real motor stalling against a limit switch would. This is what makes the homing sequence work in simulation: the motors "drive" toward the limits, "stall" when they arrive, and the homing logic detects it normally.

The `simulatePulses()` function is called in three places:
- The main `loop()`, after `updateMotion()`
- Both blocking while-loops inside `performHoming()` (phase 1: finding limits, phase 2: driving to home)

#### Current Sensing

The ACS712 hall-effect current sensors are read via `analogRead()`. In simulation mode, `analogRead()` is overridden to always return the ADC value corresponding to 0 Amps (the 2.5V zero-current offset of the ACS712). This means:

- The status output will show `Ialt:0.00A Iaz:0.00A`
- Overcurrent faults will never trigger

#### Fault Flag Pins

The motor driver fault flags (FF1/FF2 for each axis) are read via `digitalRead()`. In simulation mode, `digitalRead()` always returns `LOW` for all pins. Since both flags LOW indicates normal operation, no driver faults will be detected.

#### GPIO and Interrupts

All `pinMode()`, `attachInterrupt()`, and `analogReadResolution()` calls become no-ops. Motor control writes (`analogWrite` for PWM, `digitalWrite` for direction) are intercepted and stored in shadow variables instead of touching real GPIO registers.

### 11.2 Simulation Parameters

These are defined in `include/config.h` under the `SIMULATION_MODE` guard:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SIM_MAX_SPEED_DEG_S` | 18.0 | Maximum simulated motor speed in degrees/second at full PWM |
| `SIM_INITIAL_AZ_DEG` | 180.0 | Simulated starting azimuth before homing (degrees) |
| `SIM_INITIAL_ALT_DEG` | 45.0 | Simulated starting altitude before homing (degrees) |

The initial position values determine how far the simulated telescope must "drive" during the homing sequence before hitting the limits. With the defaults (Az=180, Alt=45), homing takes about 10 seconds at the default speed of 18 deg/s. Setting them closer to 0 will make homing faster; setting them further away gives a longer, more realistic homing test.

### 11.3 What Is Tested vs. What Is Not

| Tested in simulation | Not tested |
|----------------------|------------|
| State machine transitions (INIT, HOMING, IDLE, DRIVING, FAULT) | Real EMI/noise on reed switch signals |
| Motion profiles (acceleration, cruise, deceleration, reversal) | Actual motor driver behaviour |
| Homing sequence (limit detection, home offset drive) | Real current draw and inrush |
| Serial command parsing and dispatch | Mechanical stall characteristics |
| Position limit enforcement | Interrupt timing edge cases |
| Stall detection logic (triggers naturally at simulated limits) | Motor driver fault flag hardware |
| Configuration management (SET/SAVE/LOAD/DEFAULTS) | Flash storage wear |

### 11.4 Example Session

```
=================================
SRT Drive Controller v1.1
Acre Road Observatory, Glasgow
*** SIMULATION MODE ***
=================================

Type HELP for commands.

Starting homing sequence...
Homing: Driving to limit switches...
Homing: Altitude limit switch reached
Homing: Azimuth limit switch reached
Homing: At limit switches, moving to home position...

Homing complete. Position: Alt=0.0 Az=180.0
Ready. Type HELP for commands.

Alt:0.0 Az:180.0 Ialt:0.00A Iaz:0.00A Status:Ready
> 45 270
Slewing to Alt:45.0 Az:270.0
Alt:5.5 Az:188.5 Ialt:0.00A Iaz:0.00A Status:Slewing -> Alt:45.0 Az:270.0
Alt:11.2 Az:197.1 Ialt:0.00A Iaz:0.00A Status:Slewing -> Alt:45.0 Az:270.0
...
Alt:45.0 Az:270.0 Ialt:0.00A Iaz:0.00A Status:Ready
```

---

## 12. Troubleshooting

### 12.1 System Does Not Start

| Symptom | Check |
|---------|-------|
| No serial output | USB cable, correct port selected |
| Stuck in homing | Motor connections, encoder connections |
| Immediate fault | Motor driver power, fault flag wiring |

### 12.2 Motors Do Not Move

| Symptom | Check |
|---------|-------|
| PWM output but no motion | Motor driver reset pins (should be HIGH) |
| No PWM output | PWM pin connections (pins 8, 10) |
| Motors run backwards | Direction pin wiring (pins 9, 11) |

### 12.3 Position Errors

| Symptom | Check |
|---------|-------|
| Wrong position after homing | HOME_AZ_OFFSET_PULSES, HOME_ALT_OFFSET_PULSES |
| Position drifts | Encoder debounce (increase DEBOUNCE_MS) |
| Overshoots target | RAMP_DOWN_PULSES (increase value) |
| Jerky motion | RAMP_UP_TIME_MS (increase value) |

### 12.4 False Overcurrent Faults

| Symptom | Check |
|---------|-------|
| Faults with low actual current | Current sensor calibration in config.h |
| Faults only at startup | Normal - motor inrush current |

### 12.5 Serial Communication Issues

| Symptom | Check |
|---------|-------|
| No response to commands | Line ending settings (need CR or LF) |
| Garbled output | Baud rate (must be 115200) |
| ESP32 not receiving | Level shifter, TX/RX swapped |

---

## Appendix A: Quick Reference

### Commands

| Command | Example | Description |
|---------|---------|-------------|
| Go to position | `45 180` | Move to Alt=45, Az=180 |

### Status Output

```
Alt:<deg> Az:<deg> Ialt:<A>A Iaz:<A>A Status:<state> [<fault>] [-> Alt:<deg> Az:<deg>]
```

### Pin Summary

```
Motor Control:  8(PWM-Az), 9(DIR-Az), 10(PWM-Alt), 11(DIR-Alt)
Driver Reset:   22(Az), 24(Alt)
Encoders:       12(Az), 13(Alt)
Fault Flags:    4,5(Az), 6,7(Alt)
Current Sense:  A0(Alt), A1(Az)
Serial1:        18(TX), 19(RX)
```

---

*Document revision: 1.1*
*Generated: March 2026*
