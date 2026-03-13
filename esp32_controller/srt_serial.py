# srt_serial.py - Serial communication with Arduino Due

from machine import UART
import re


class SRTSerial:
    """Handles serial communication with the SRT drive controller (Arduino Due)

    Due Status Output Format:
        Alt:45.0 Az:180.0 Ialt:0.15A Iaz:0.20A Status:Ready
        Alt:45.0 Az:180.0 Ialt:0.15A Iaz:0.20A Status:Slewing -> Alt:50.0 Az:200.0
        Alt:45.0 Az:180.0 Ialt:0.15A Iaz:0.20A Status:FAULT [Motor stalled]

    Due Command Format:
        <alt> <az>    - Go to position (e.g., "45.0 180.0")
        HOME          - Run homing sequence
        STOP          - Emergency stop
        STATUS        - Request status
        RESET         - Clear fault and re-home
    """

    def __init__(self, tx_pin, rx_pin, baud_rate):
        self.uart = UART(2, baudrate=baud_rate, tx=tx_pin, rx=rx_pin)
        self.last_status = None
        self.current_alt = 0.0
        self.current_az = 0.0
        self.target_alt = 0.0
        self.target_az = 0.0
        self.alt_current_a = 0.0
        self.az_current_a = 0.0
        self.status_str = "UNKNOWN"
        self.fault_str = ""
        self.is_slewing = False

    def send_target(self, alt, az):
        """Send target Alt/Az to the Due"""
        cmd = f"{alt:.1f} {az:.1f}\n"
        self.uart.write(cmd.encode())

    def send_home(self):
        """Send HOME command"""
        self.uart.write(b"HOME\n")

    def send_stop(self):
        """Send STOP command"""
        self.uart.write(b"STOP\n")

    def send_reset(self):
        """Send RESET command to clear fault"""
        self.uart.write(b"RESET\n")

    def request_status(self):
        """Request status update from Due"""
        self.uart.write(b"STATUS\n")

    def read_status(self):
        """Read status line from Due, returns True if new data received"""
        if self.uart.any():
            try:
                line = self.uart.readline()
                if line:
                    line = line.decode().strip()
                    if line.startswith("Alt:"):
                        self._parse_status(line)
                        return True
            except Exception as e:
                print(f"Serial read error: {e}")
        return False

    def _parse_status(self, line):
        """Parse status line from Due

        Example: Alt:45.0 Az:180.0 Ialt:0.15A Iaz:0.20A Status:Slewing -> Alt:50.0 Az:200.0
        """
        try:
            self.last_status = line

            # Extract current position
            alt_match = re.search(r'Alt:([-\d.]+)', line)
            az_match = re.search(r'Az:([-\d.]+)', line)
            if alt_match:
                self.current_alt = float(alt_match.group(1))
            if az_match:
                self.current_az = float(az_match.group(1))

            # Extract currents
            ialt_match = re.search(r'Ialt:([\d.]+)A', line)
            iaz_match = re.search(r'Iaz:([\d.]+)A', line)
            if ialt_match:
                self.alt_current_a = float(ialt_match.group(1))
            if iaz_match:
                self.az_current_a = float(iaz_match.group(1))

            # Extract status
            status_match = re.search(r'Status:(\w+)', line)
            if status_match:
                self.status_str = status_match.group(1)

            # Check for fault message
            fault_match = re.search(r'\[(.+)\]', line)
            if fault_match:
                self.fault_str = fault_match.group(1)
            else:
                self.fault_str = ""

            # Check if slewing with target
            self.is_slewing = " -> " in line
            if self.is_slewing:
                # Extract target position from "-> Alt:50.0 Az:200.0"
                target_match = re.search(r'-> Alt:([-\d.]+) Az:([-\d.]+)', line)
                if target_match:
                    self.target_alt = float(target_match.group(1))
                    self.target_az = float(target_match.group(2))

        except Exception as e:
            print(f"Parse error: {e} - line: {line}")

    def get_status_dict(self):
        """Return current status as a dictionary"""
        return {
            "alt": self.current_alt,
            "az": self.current_az,
            "target_alt": self.target_alt,
            "target_az": self.target_az,
            "alt_current_a": self.alt_current_a,
            "az_current_a": self.az_current_a,
            "status": self.status_str,
            "fault": self.fault_str,
            "is_slewing": self.is_slewing,
            "raw": self.last_status
        }

    def is_ready(self):
        """Check if Due is ready to accept commands"""
        return self.status_str == "Ready"

    def is_fault(self):
        """Check if Due is in fault state"""
        return self.status_str == "FAULT"
