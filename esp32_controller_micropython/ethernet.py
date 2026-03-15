# ethernet.py - W5500 Ethernet management

import time
from machine import Pin, SPI

from config import (
    ETH_SPI_ID, ETH_SCK, ETH_MOSI, ETH_MISO, ETH_CS, ETH_RST,
    ETH_USE_DHCP, ETH_STATIC_IP, ETH_STATIC_MASK,
    ETH_STATIC_GW, ETH_STATIC_DNS
)

# Check if WIZNET5K support is available in this firmware
try:
    import network
    WIZNET_AVAILABLE = hasattr(network, 'WIZNET5K')
except ImportError:
    WIZNET_AVAILABLE = False


class EthernetManager:
    """Manages W5500 Ethernet connection"""

    def __init__(self):
        self.nic = None
        self.connected = False
        self.available = WIZNET_AVAILABLE

    def init(self):
        """Initialize the W5500 hardware"""
        if not WIZNET_AVAILABLE:
            print("W5500: WIZNET5K not available in this firmware")
            return False

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
                time.sleep(0.2)

            # Initialize WIZNET5K with timeout
            self.nic = network.WIZNET5K(spi, cs)

            # Check if chip responds (read MAC - will be all zeros or FF if no chip)
            mac = self.nic.config('mac')
            if mac == b'\x00\x00\x00\x00\x00\x00' or mac == b'\xff\xff\xff\xff\xff\xff':
                print("W5500: No chip detected (invalid MAC)")
                self.nic = None
                return False

            self.nic.active(True)
            print(f"W5500 initialized, MAC: {self._format_mac(mac)}")
            return True

        except OSError as e:
            print(f"W5500 hardware error: {e}")
            self.nic = None
            return False
        except Exception as e:
            print(f"W5500 init failed: {e}")
            self.nic = None
            return False

    def _format_mac(self, mac):
        """Format MAC address as string"""
        return ':'.join('%02x' % b for b in mac)

    def connect(self, timeout=10):
        """Connect to network (DHCP or static IP)"""
        if self.nic is None:
            if not self.init():
                return False

        try:
            if ETH_USE_DHCP:
                print("Ethernet: requesting DHCP...")
                # DHCP can block, so we set a reasonable timeout
                self.nic.ifconfig('dhcp')
            else:
                print(f"Ethernet: using static IP {ETH_STATIC_IP}")
                self.nic.ifconfig((
                    ETH_STATIC_IP,
                    ETH_STATIC_MASK,
                    ETH_STATIC_GW,
                    ETH_STATIC_DNS
                ))

            # Wait for link with timeout
            start = time.time()
            while not self.nic.isconnected():
                if time.time() - start > timeout:
                    print("Ethernet: connection timeout (no cable?)")
                    return False
                time.sleep(0.5)

            self.connected = True
            ip = self.nic.ifconfig()[0]
            print(f"Ethernet connected: {ip}")
            return True

        except OSError as e:
            # Common when no cable connected
            print(f"Ethernet: network error: {e}")
            return False
        except Exception as e:
            print(f"Ethernet connect failed: {e}")
            return False

    def is_connected(self):
        """Check if Ethernet is connected"""
        if self.nic is None:
            return False
        try:
            return self.nic.isconnected()
        except Exception:
            return False

    def get_ip(self):
        """Get current IP address"""
        try:
            if self.nic and self.nic.isconnected():
                return self.nic.ifconfig()[0]
        except Exception:
            pass
        return None

    def get_status(self):
        """Return Ethernet status dict"""
        if not WIZNET_AVAILABLE:
            return {
                "available": False,
                "connected": False,
                "ip": None,
                "mac": None
            }

        connected = self.is_connected()
        mac = None
        if self.nic:
            try:
                mac = self._format_mac(self.nic.config('mac'))
            except Exception:
                pass

        return {
            "available": True,
            "connected": connected,
            "ip": self.get_ip() if connected else None,
            "mac": mac
        }


# Global instance
ethernet = EthernetManager()
