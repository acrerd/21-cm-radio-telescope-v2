# SRT Drive Controller - Operations Manual

**Version 1.0**
**Acre Road Observatory, Glasgow**

---

## Table of Contents

1. [Overview](#1-overview)
2. [Hardware Connections](#2-hardware-connections)
3. [Serial Communication](#3-serial-communication)
4. [Status Messages](#4-status-messages)
5. [Commands](#5-commands)
6. [System States](#6-system-states)
7. [Fault Conditions](#7-fault-conditions)
8. [Startup Sequence](#8-startup-sequence)
9. [Configuration](#9-configuration)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Overview

The SRT Drive Controller manages the alt-azimuth drive system for the Small Radio Telescope. It provides:

- Automatic homing on startup
- Position tracking via reed switch encoders (0.5 degree resolution)
- Smooth motion with acceleration/deceleration ramps
- Overcurrent and fault protection
- Serial interface for control and monitoring

### Specifications

| Parameter | Value |
|-----------|-------|
| Microcontroller | Arduino Due (ARM Cortex-M3) |
| Position Resolution | 0.5 degrees (2 pulses per degree) |
| Altitude Range | 0 to 90 degrees |
| Azimuth Range | 0 to 355 degrees |
| Home Position | Alt: 0, Az: 180 degrees |
| Current Limit | 5 Amps (configurable) |
| Serial Baud Rate | 9600 |

---

## 2. Hardware Connections

### 2.1 Arduino Due Pinout

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
| Az Pulses | 12 | Blue | Reed switch, falling edge |
| Alt Pulses | 13 | Orange | Reed switch, falling edge |
| **Current Sensors** |
| Az Current | A1 | Black | Analog input |
| Alt Current | A0 | White | Analog input |

### 2.2 USB Ports

The Arduino Due has two USB ports:

| Port | Location | Use |
|------|----------|-----|
| **Programming Port** | Near DC jack | Programming + serial terminal |
| **Native USB Port** | Near reset button | Not used |

**Use the Programming Port** for both uploading firmware and serial communication.

### 2.3 ESP32 Interface (Serial1)

For external microcontroller connections (e.g., ESP32), use the hardware UART:

| Due Pin | Function | Connect to ESP32 |
|---------|----------|------------------|
| 18 | TX1 (transmit) | RX (via level shifter) |
| 19 | RX1 (receive) | TX (via level shifter) |
| GND | Ground | GND |

**Important:** The Due operates at 3.3V logic. If your ESP32 is also 3.3V, direct connection is possible. Otherwise, use a level shifter.

---

## 3. Serial Communication

### 3.1 Connection Settings

| Parameter | Value |
|-----------|-------|
| Baud Rate | 9600 |
| Data Bits | 8 |
| Stop Bits | 1 |
| Parity | None |
| Line Ending | CR or LF or CRLF |

### 3.2 Output Format

The controller outputs a status line once per second:

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
| `Status` | System state | See section 6 |

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

## 4. Status Messages

### 4.1 System Status Values

| Status | Description |
|--------|-------------|
| `Initializing` | System is starting up |
| `Homing` | Finding limit switches and moving to home position |
| `Ready` | Idle, waiting for commands |
| `Slewing` | Moving to target position |
| `Reversing` | Decelerating before changing direction |
| `FAULT` | Error condition - motors stopped |

### 4.2 Startup Messages

On power-up, you will see:

```
=================================
SRT Drive Controller v1.0
Acre Road Observatory, Glasgow
=================================

Starting homing sequence...
Homing: Driving to limit switches...
Homing: Altitude limit switch reached
Homing: Azimuth limit switch reached
Homing: At limit switches, moving to home position...

Homing complete. Position: Alt=0.0 Az=180.0
Ready to receive commands. Send: <altitude> <azimuth>
```

### 4.3 Command Acknowledgments

When a valid command is received:

```
Slewing to Alt:45.0 Az:200.0
```

### 4.4 Error Messages

| Message | Cause |
|---------|-------|
| `ERROR: Cannot slew while in FAULT state. Power cycle to reset.` | Fault has occurred |
| `ERROR: Cannot slew while homing in progress.` | Command sent during homing |
| `ERROR: Altitude xx.x is out of range. Valid: 0 to 90 degrees` | Alt out of bounds |
| `ERROR: Azimuth xx.x is out of range. Valid: 0 to 355 degrees` | Az out of bounds |
| `ERROR: Invalid command '...'. Send: <altitude> <azimuth>` | Parse error |

---

## 5. Commands

All commands are case-insensitive.

### 5.1 Motion Commands

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

### 5.2 Information Commands

| Command | Description |
|---------|-------------|
| `STATUS` | Show current position and status |
| `CONFIG` | Show all configuration parameters |
| `HELP` | Show command help |

### 5.3 Configuration Commands

| Command | Description |
|---------|-------------|
| `SET <param> <value>` | Set a configuration parameter |
| `SAVE` | Save configuration to flash memory |
| `LOAD` | Load configuration from flash memory |
| `DEFAULTS` | Reset to factory defaults |

**SET Parameters:**

| Parameter | Description | Default | Units |
|-----------|-------------|---------|-------|
| `ALTMIN` | Altitude minimum limit | 0 | degrees |
| `ALTMAX` | Altitude maximum limit | 90 | degrees |
| `AZMIN` | Azimuth minimum limit | 0 | degrees |
| `AZMAX` | Azimuth maximum limit | 355 | degrees |
| `HOMEALT` | Home altitude position | 0 | degrees |
| `HOMEAZ` | Home azimuth position | 180 | degrees |
| `RAMPUP` | Acceleration time | 500 | ms |
| `RAMPDOWN` | Deceleration distance | 7 | degrees |
| `STOPRAMP` | Reversal deceleration time | 300 | ms |
| `CURRENT` | Overcurrent threshold | 5.0 | Amps |
| `STALL` | Stall detection timeout | 2000 | ms |

**Examples:**
```
SET AZMAX 350        Set azimuth upper limit to 350 degrees
SET CURRENT 4.5      Set current limit to 4.5 Amps
SET HOMEAZ 175       Set home azimuth to 175 degrees
SAVE                 Save changes to flash
CONFIG               Verify settings
```

### 5.4 Motion Profile

The system uses smooth motion profiles to prevent structural oscillation:

- **Acceleration:** Linear ramp over configured time (default 500ms)
- **Cruise:** Full speed
- **Deceleration:** Quadratic ramp over configured distance (default 7 degrees)
- **Reversal:** If direction change needed, decelerates over configured time (default 300ms), then reverses

Commands can be sent at any time, even while moving. This enables smooth tracking of celestial sources with continuous position updates.

### 5.5 Position Limits

Default limits (adjustable via SET commands):

| Axis | Minimum | Maximum |
|------|---------|---------|
| Altitude | 0 degrees (horizon) | 90 degrees (zenith) |
| Azimuth | 0 degrees (North) | 355 degrees |

---

## 6. System States

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

## 7. Fault Conditions

When a fault occurs, the system immediately stops both motors and enters the FAULT state. The fault is reported in the status output.

### 7.1 Motor Driver Faults

These are detected via the motor driver's fault flag pins:

| Fault | Description | Likely Cause |
|-------|-------------|--------------|
| `Azimuth motor short circuit` | FF1=LOW, FF2=HIGH | Wiring short, motor failure |
| `Altitude motor short circuit` | FF1=LOW, FF2=HIGH | Wiring short, motor failure |
| `Azimuth motor overheating` | FF1=HIGH, FF2=LOW | Prolonged high-current operation |
| `Altitude motor overheating` | FF1=HIGH, FF2=LOW | Prolonged high-current operation |
| `Azimuth motor undervoltage` | FF1=HIGH, FF2=HIGH | Power supply issue |
| `Altitude motor undervoltage` | FF1=HIGH, FF2=HIGH | Power supply issue |

### 7.2 Current Faults

| Fault | Description | Threshold |
|-------|-------------|-----------|
| `Azimuth motor overcurrent` | Motor drawing too much current | > 5.0 Amps |
| `Altitude motor overcurrent` | Motor drawing too much current | > 5.0 Amps |

**Possible causes:**
- Mechanical obstruction
- Binding in gears or drive train
- Motor failure
- Overloaded drive

### 7.3 Stall Faults

| Fault | Description | Detection |
|-------|-------------|-----------|
| `Azimuth motor stalled` | No encoder pulses while driving | 2 seconds timeout |
| `Altitude motor stalled` | No encoder pulses while driving | 2 seconds timeout |

**Possible causes:**
- Motor not turning
- Encoder failure
- Belt/coupling slipped
- Mechanical jam

### 7.4 Recovery from Faults

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

## 8. Startup Sequence

On power-up, the system performs the following sequence:

1. **Initialize hardware**
   - Configure all GPIO pins
   - Enable motor drivers
   - Set motors to stopped state

2. **Initialize serial ports**
   - Programming port (USB) at 9600 baud
   - Serial1 (pins 18/19) at 9600 baud (if enabled)

3. **Homing sequence**
   - Drive both motors toward limit switches (Az West, Alt Down)
   - Wait for each motor to stop (2 second timeout = at limit)
   - Reset position counters to 0
   - Drive to home position (Az: 180 degrees)
   - Set position to home coordinates

4. **Enter READY state**
   - Begin 1Hz status output
   - Accept commands

**Note:** The homing sequence takes approximately 30-60 seconds depending on the starting position.

---

## 9. Configuration

All configurable parameters are in `include/config.h`. The most commonly adjusted parameters:

### 9.1 Position Limits

```c
#define ALT_MIN_DEG         0.0     // Altitude lower limit
#define ALT_MAX_DEG         90.0    // Altitude upper limit
#define AZ_MIN_DEG          0.0     // Azimuth lower limit
#define AZ_MAX_DEG          355.0   // Azimuth upper limit
```

### 9.2 Home Position

```c
#define HOME_ALT_DEG        0.0     // Altitude home position
#define HOME_AZ_DEG         180.0   // Azimuth home position

// Pulses to drive from limits to home
#define HOME_ALT_OFFSET_PULSES  0       // (0 - 0) * 2 = 0
#define HOME_AZ_OFFSET_PULSES   360     // (180 - 0) * 2 = 360
```

### 9.3 Safety Limits

```c
#define CURRENT_LIMIT_AMPS  5.0     // Stop if current exceeds this
#define STALL_TIMEOUT_MS    2000    // No pulses = stalled
```

### 9.4 Motion Profile

```c
#define RAMP_UP_TIME_MS     500     // Acceleration time
#define RAMP_DOWN_PULSES    14      // Start decelerating at this distance
```

### 9.5 Serial1 Enable

```c
#define ENABLE_SERIAL1      1       // Set to 0 to disable Serial1
```

After changing any configuration, rebuild and upload:

```bash
pio run --target upload
```

---

## 10. Troubleshooting

### 10.1 System Does Not Start

| Symptom | Check |
|---------|-------|
| No serial output | USB cable, correct port selected |
| Stuck in homing | Motor connections, encoder connections |
| Immediate fault | Motor driver power, fault flag wiring |

### 10.2 Motors Do Not Move

| Symptom | Check |
|---------|-------|
| PWM output but no motion | Motor driver reset pins (should be HIGH) |
| No PWM output | PWM pin connections (pins 8, 10) |
| Motors run backwards | Direction pin wiring (pins 9, 11) |

### 10.3 Position Errors

| Symptom | Check |
|---------|-------|
| Wrong position after homing | HOME_AZ_OFFSET_PULSES, HOME_ALT_OFFSET_PULSES |
| Position drifts | Encoder debounce (increase DEBOUNCE_MS) |
| Overshoots target | RAMP_DOWN_PULSES (increase value) |
| Jerky motion | RAMP_UP_TIME_MS (increase value) |

### 10.4 False Overcurrent Faults

| Symptom | Check |
|---------|-------|
| Faults with low actual current | Current sensor calibration in config.h |
| Faults only at startup | Normal - motor inrush current |

### 10.5 Serial Communication Issues

| Symptom | Check |
|---------|-------|
| No response to commands | Line ending settings (need CR or LF) |
| Garbled output | Baud rate (must be 9600) |
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

*Document revision: 1.0*
*Generated: March 2026*
