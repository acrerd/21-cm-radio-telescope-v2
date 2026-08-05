#!/usr/bin/env python3
"""Arduino Due drive-firmware emulator for bench-testing the ESP32 controller.

Emulates the Due end of the ESP32 <-> Due UART protocol (line-oriented ASCII,
115200 baud) so the ESP32 controller can be exercised on the bench with no
telescope attached: slews, homing, faults, calibrator state, and the 1 Hz
status stream all behave like the real drive firmware (src/main.cpp).

=============================================================================
HARDWARE SETUP
=============================================================================
You need a 3.3 V USB-to-UART adapter (FTDI, CP2102, CH340, ...).

  !! The ESP32 is NOT 5 V tolerant. Set the adapter jumper to 3.3 V. !!

Wire the adapter to the ESP32 with TX and RX crossed, plus common ground:

  WT32-ETH01 (the deployed board, default build, -e wt32-eth01):
      WT32 IO4     (its TX) --> adapter RX
      WT32 IO14    (its RX) <-- adapter TX
      WT32 GND     <-------->  adapter GND

  ESP32-S3 Super Mini (legacy, no longer deployed, -e esp32s3):
      ESP32 GPIO5  (its TX) --> adapter RX
      ESP32 GPIO6  (its RX) <-- adapter TX
      ESP32 GND    <-------->  adapter GND

(Pin assignments from esp32_controller_arduino/src/config.h. Do NOT use the
board's TX0/RX0 pins - those are the programming/console port.)

Power the ESP32 from its own USB cable as usual. Find the adapter's serial
port name (Windows: Device Manager -> Ports, e.g. COM7; Linux:
/dev/ttyUSB0), then run:

    pip install pyserial          # once
    python tools/due_emulator.py --port COM7

The console shows all traffic, timestamped. Type 'help' at any time for the
fault-injection commands.

=============================================================================
WHAT IT EMULATES (matching src/main.cpp)
=============================================================================
- Unsolicited status line once per second (the real Due always sends the
  full line to Serial1), exact positional format:
    Alt:%.1f Az:%.1f Ialt:%.1fA Iaz:%.1fA Status:<state> [<fault>] \
        -> Alt:%.1f Az:%.1f Cal:ON|OFF
  ("[<fault>]" only in FAULT state; "-> ..." only while driving.)
- Commands: "<alt> <az>", DRIVE, HOME, STOP, RESET, CAL [ON|OFF], STATUS
  (immediate reply), anything else -> error text, like the real firmware.
- Motion: constant-rate slew toward the target with positions quantised to
  0.5 deg (PULSES_PER_DEGREE = 2), alt clamped 0..90, az 0..355.
- Homing: a few seconds in "Homing" then position (0, 0).
- Motor currents: small idle noise, ~1 A while driving.

Console injection commands (type into the emulator, not the ESP32):
    fault <text>   enter FAULT state with the given fault message
    clear          clear the fault (like a power cycle)
    pos <alt> <az> teleport the reported position
    quiet on|off   stop/resume the 1 Hz status stream (test ESP32 timeout UI)
    garbage        send one malformed line (test parser robustness)
    split          send the next status line in two chunks with a delay
    help           this list

The --capture option appends every raw received chunk to a file with
timestamps, exactly as it arrived. Use it to detect interleaved/spliced
command lines from the ESP32 (review finding C2): grep the capture for lines
that are not a clean command.
"""

import argparse
import random
import sys
import threading
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial is required:  pip install pyserial")

ALT_MIN, ALT_MAX = 0.0, 90.0
AZ_MIN, AZ_MAX = 0.0, 355.0
QUANT = 0.5                 # PULSES_PER_DEGREE = 2 -> 0.5 deg steps


def quantise(x):
    return round(x / QUANT) * QUANT


class DueEmulator:
    def __init__(self, port, baud, alt_rate, az_rate, status_period,
                 start_alt, start_az, homing_secs, capture_path):
        self.ser = serial.Serial(port, baud, timeout=0.05)
        self.alt_rate = alt_rate
        self.az_rate = az_rate
        self.status_period = status_period
        self.homing_secs = homing_secs

        self.alt = start_alt
        self.az = start_az
        self.target_alt = start_alt
        self.target_az = start_az
        self.state = "Ready"        # Ready | Slewing | Homing | FAULT
        self.fault = ""
        self.cal_on = False
        self.homing_until = 0.0
        self.quiet = False
        self.split_next = False

        self.lock = threading.Lock()
        self.capture = open(capture_path, "a") if capture_path else None
        self.rx_buffer = ""

    # ------------------------------------------------------------------ I/O

    def log(self, direction, text):
        print(f"{time.strftime('%H:%M:%S')} {direction} {text}")

    def send(self, line):
        self.log("TX", line)
        self.ser.write((line + "\r\n").encode())

    def send_status(self):
        with self.lock:
            line = f"Alt:{self.alt:.1f} Az:{self.az:.1f}"
            if self.state == "Slewing":
                ialt = 1.0 + random.uniform(-0.1, 0.3)
                iaz = 1.2 + random.uniform(-0.1, 0.3)
            else:
                ialt = random.uniform(0.0, 0.1)
                iaz = random.uniform(0.0, 0.1)
            line += f" Ialt:{ialt:.1f}A Iaz:{iaz:.1f}A"
            line += f" Status:{self.state}"
            if self.state == "FAULT":
                line += f" [{self.fault}]"
            if self.state == "Slewing":
                line += f" -> Alt:{self.target_alt:.1f} Az:{self.target_az:.1f}"
            line += f" Cal:{'ON' if self.cal_on else 'OFF'}"
            split = self.split_next
            self.split_next = False

        if split:
            cut = len(line) // 2
            self.log("TX", line + "   (sent as two chunks)")
            self.ser.write(line[:cut].encode())
            time.sleep(0.3)
            self.ser.write((line[cut:] + "\r\n").encode())
        else:
            self.log("TX", line)
            self.ser.write((line + "\r\n").encode())

    # ------------------------------------------------------------ commands

    def execute_drive(self, alt, az):
        alt = min(max(alt, ALT_MIN), ALT_MAX)
        az = min(max(az, AZ_MIN), AZ_MAX)
        with self.lock:
            if self.state == "FAULT":
                return
            self.target_alt = alt
            self.target_az = az
            if abs(alt - self.alt) >= QUANT or abs(az - self.az) >= QUANT:
                self.state = "Slewing"

    def handle_command(self, cmdline):
        self.log("RX", cmdline)
        parts = cmdline.split()
        if not parts:
            return

        # Bare "<alt> <az>" drive command (first token starts numerically)
        if cmdline[0].isdigit() or cmdline[0] in "-.":
            try:
                alt, az = float(parts[0]), float(parts[1])
                self.execute_drive(alt, az)
                return
            except (ValueError, IndexError):
                pass

        cmd = parts[0].upper()
        if cmd == "DRIVE" and len(parts) >= 3:
            try:
                self.execute_drive(float(parts[1]), float(parts[2]))
            except ValueError:
                self.send("Usage: DRIVE <altitude> <azimuth>")
        elif cmd == "HOME":
            with self.lock:
                if self.state == "FAULT":
                    self.send("ERROR: Cannot home while in FAULT state. "
                              "Power cycle to reset.")
                elif self.state == "Homing":
                    self.send("Already homing.")
                else:
                    self.send("Starting homing sequence...")
                    self.state = "Homing"
                    self.homing_until = time.time() + self.homing_secs
        elif cmd == "STOP":
            with self.lock:
                if self.state == "Slewing":
                    self.target_alt = self.alt
                    self.target_az = self.az
                    self.state = "Ready"
            self.send("STOPPED")
        elif cmd == "RESET":
            with self.lock:
                if self.state != "FAULT":
                    self.send("No fault to reset.")
                else:
                    self.send("Fault cleared. Use HOME to re-home.")
                    self.fault = ""
                    self.state = "Ready"
        elif cmd == "CAL":
            with self.lock:
                if len(parts) > 1 and parts[1].upper() in ("ON", "1"):
                    self.cal_on = True
                elif len(parts) > 1 and parts[1].upper() in ("OFF", "0"):
                    self.cal_on = False
                elif len(parts) > 1:
                    self.send("Usage: CAL [ON|OFF]")
                    return
                else:
                    self.cal_on = not self.cal_on
            self.send(f"Calibrator: {'ON' if self.cal_on else 'OFF'}")
        elif cmd == "STATUS":
            self.send_status()
        else:
            self.send(f"ERROR: Unknown command '{parts[0]}'")

    # ------------------------------------------------------------- threads

    def reader_loop(self):
        while True:
            data = self.ser.read(256)
            if not data:
                continue
            if self.capture:
                self.capture.write(
                    f"{time.time():.3f} {data!r}\n")
                self.capture.flush()
            self.rx_buffer += data.decode(errors="replace")
            while "\n" in self.rx_buffer:
                line, self.rx_buffer = self.rx_buffer.split("\n", 1)
                line = line.strip()
                if line:
                    self.handle_command(line)

    def motion_loop(self):
        dt = 0.1
        while True:
            time.sleep(dt)
            with self.lock:
                if self.state == "Homing":
                    if time.time() >= self.homing_until:
                        self.alt = self.az = 0.0
                        self.target_alt = self.target_az = 0.0
                        self.state = "Ready"
                elif self.state == "Slewing":
                    def step(pos, target, rate):
                        delta = target - pos
                        move = min(abs(delta), rate * dt)
                        return pos + move * (1 if delta > 0 else -1)
                    self.alt = step(self.alt, self.target_alt, self.alt_rate)
                    self.az = step(self.az, self.target_az, self.az_rate)
                    if (abs(self.alt - self.target_alt) < QUANT / 2 and
                            abs(self.az - self.target_az) < QUANT / 2):
                        self.alt = quantise(self.target_alt)
                        self.az = quantise(self.target_az)
                        self.state = "Ready"

    def status_loop(self):
        while True:
            time.sleep(self.status_period)
            if not self.quiet:
                self.send_status()

    def console_loop(self):
        while True:
            try:
                cmd = input().strip()
            except EOFError:
                return
            parts = cmd.split()
            if not parts:
                continue
            op = parts[0].lower()
            if op == "fault":
                with self.lock:
                    self.fault = " ".join(parts[1:]) or "Azimuth motor stalled"
                    self.state = "FAULT"
                print(f"** FAULT injected: {self.fault}")
            elif op == "clear":
                with self.lock:
                    self.fault = ""
                    self.state = "Ready"
                print("** fault cleared")
            elif op == "pos" and len(parts) == 3:
                with self.lock:
                    self.alt = quantise(float(parts[1]))
                    self.az = quantise(float(parts[2]))
                    self.target_alt, self.target_az = self.alt, self.az
                    self.state = "Ready"
                print(f"** position set to Alt:{self.alt} Az:{self.az}")
            elif op == "quiet" and len(parts) == 2:
                self.quiet = parts[1].lower() == "on"
                print(f"** status stream {'paused' if self.quiet else 'resumed'}")
            elif op == "garbage":
                self.send("Alt:xx GARBAGE Iaz:")
                print("** malformed line sent")
            elif op == "split":
                self.split_next = True
                print("** next status line will be sent in two chunks")
            elif op == "help":
                print("  fault <text> | clear | pos <alt> <az> | "
                      "quiet on|off | garbage | split | help")
            else:
                print("** unknown console command, type 'help'")

    def run(self):
        for fn in (self.reader_loop, self.motion_loop, self.status_loop):
            threading.Thread(target=fn, daemon=True).start()
        print(f"Due emulator running on {self.ser.port} @ {self.ser.baudrate} "
              f"baud. Type 'help' for injection commands, Ctrl+C to quit.")
        self.console_loop()


def main():
    ap = argparse.ArgumentParser(
        description="Emulate the Arduino Due drive firmware over a serial "
                    "port (see module docstring for wiring).")
    ap.add_argument("--port", required=True,
                    help="serial port of the USB-UART adapter, e.g. COM7 "
                         "or /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--alt-rate", type=float, default=1.5,
                    help="altitude slew rate, deg/s (default 1.5)")
    ap.add_argument("--az-rate", type=float, default=2.5,
                    help="azimuth slew rate, deg/s (default 2.5)")
    ap.add_argument("--status-period", type=float, default=1.0,
                    help="seconds between unsolicited status lines")
    ap.add_argument("--start-alt", type=float, default=0.0)
    ap.add_argument("--start-az", type=float, default=0.0)
    ap.add_argument("--homing-secs", type=float, default=8.0,
                    help="simulated homing duration")
    ap.add_argument("--capture", metavar="FILE",
                    help="append raw received chunks (timestamped, repr'd) "
                         "to FILE for interleaving analysis")
    args = ap.parse_args()

    emu = DueEmulator(args.port, args.baud, args.alt_rate, args.az_rate,
                      args.status_period, args.start_alt, args.start_az,
                      args.homing_secs, args.capture)
    try:
        emu.run()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
