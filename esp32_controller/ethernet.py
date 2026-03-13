# ethernet.py - W5500 Ethernet management

import network
from machine import Pin, SPI
import time

from config import (
    ETH_SPI_ID, ETH_SCK, ETH_MOSI, ETH_MISO, ETH_CS, ETH_RST,
    ETH_USE_DHCP, ETH_STATIC_IP, ETH_STATIC_MASK,
    ETH_STATIC_GW, ETH_STATIC_DNS
)


class EthernetManager:
    """Manages W5500 Ethernet connection"""

    def __init__(self):
        self.nic = None
        self.connected = False

    def init(self):
        """Initialize the W5500 hardware"""
        try:
            # Configure SPI
            spi = SPI(
                ETH_SPI_ID,
                baudrate=10_000_000,
                polarity=0,
                phase=0,
                sck=Pin(ETH_SCK),
                mosi=Pin(ETH_MOSI),
                miso=Pin(ETH_MISO)
            )

            # Chip select pin
            cs = Pin(ETH_CS, Pin.OUT)

            # Reset pin (optional)
            if ETH_RST is not None:
                rst = Pin(ETH_RST, Pin.OUT)
                rst.value(0)
                time.sleep(0.1)
                rst.value(1)
                time.sleep(0.1)

            # Initialize WIZNET5K
            self.nic = network.WIZNET5K(spi, cs)
            self.nic.active(True)

            print("W5500 initialized")
            return True

        except Exception as e:
            print(f"W5500 init failed: {e}")
            return False

    def connect(self, timeout=10):
        """Connect to network (DHCP or static IP)"""
        if self.nic is None:
            if not self.init():
                return False

        try:
            if ETH_USE_DHCP:
                # Use DHCP
                print("Ethernet: requesting DHCP...")
                self.nic.ifconfig('dhcp')
            else:
                # Use static IP
                print(f"Ethernet: using static IP {ETH_STATIC_IP}")
                self.nic.ifconfig((
                    ETH_STATIC_IP,
                    ETH_STATIC_MASK,
                    ETH_STATIC_GW,
                    ETH_STATIC_DNS
                ))

            # Wait for link
            start = time.time()
            while not self.nic.isconnected():
                if time.time() - start > timeout:
                    print("Ethernet: connection timeout")
                    return False
                time.sleep(0.5)

            self.connected = True
            ip = self.nic.ifconfig()[0]
            print(f"Ethernet connected: {ip}")
            return True

        except Exception as e:
            print(f"Ethernet connect failed: {e}")
            return False

    def is_connected(self):
        """Check if Ethernet is connected"""
        if self.nic is None:
            return False
        return self.nic.isconnected()

    def get_ip(self):
        """Get current IP address"""
        if self.nic and self.nic.isconnected():
            return self.nic.ifconfig()[0]
        return None

    def get_status(self):
        """Return Ethernet status dict"""
        connected = self.is_connected()
        return {
            "connected": connected,
            "ip": self.get_ip() if connected else None,
            "mac": ':'.join('%02x' % b for b in self.nic.config('mac')) if self.nic else None
        }


# Global instance
ethernet = EthernetManager()
