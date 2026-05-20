# Copy to config.py and fill in your values.
# config.py is gitignored so secrets stay local.

WIFI_SSID = "your-network"
WIFI_PASS = "your-password"

# Static IP of the computer running host/server.py.
# Include http:// and the port.
SERVER_URL = "http://192.168.1.50:8765/api/heartbeat"

# How often to poll (seconds).
POLL_INTERVAL_S = 30

# Optional. If your server is behind a token, append it to the URL:
#   SERVER_URL = "http://192.168.1.50:8765/api/heartbeat?token=YOURTOKEN"
