# state.py - Shared global state for SRT controller

# WiFi manager instance (set by main.py after boot delay)
wifi = None

# Tracking state
current_ra = 0.0       # hours
current_dec = 0.0      # degrees
target_alt = 0.0       # degrees
target_az = 0.0        # degrees
tracking_enabled = False
target_name = None     # "Sun", "Moon", "Gal l=x b=y", or None for manual RA/Dec
waiting_for_wrap = False  # True when target is outside az limits
waiting_for_rise = False  # True when target is below horizon

# Time state
time_synced = False    # True if time has been set (NTP or browser)
time_source = None     # "NTP", "browser", or None
