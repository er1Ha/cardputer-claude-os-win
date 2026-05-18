"""Push-to-Claude device config — copy to ``config.py`` and fill in.

The Push-to-Claude app loads ``WORKER_BASE`` and ``DEVICE_SECRET``
from this module at import time. ``config.py`` is gitignored so your
secret never leaves the device. See ``worker/README.md`` for how to
deploy your own Cloudflare Worker relay and where to get these
values.
"""

# Base URL of YOUR deployed Cloudflare Worker, e.g.
#   "https://push-to-claude.<your-subdomain>.workers.dev"
#
# Alternatively, point at the local_relay running on your PC so chat
# consumes your Claude Pro/Max + ChatGPT subscription quota instead
# of pay-as-you-go API credit:
#   "http://192.168.1.10:8787"          # your PC's LAN IP, port 8787
# See local_relay/README.md.
#
# No trailing slash; the app appends "/ask", "/ask-text", "/reset",
# "/codex", "/usage".
WORKER_BASE = ""

# Shared secret between this device and the Worker. Must match the
# DEVICE_SECRET you set on the Worker via:
#   wrangler secret put DEVICE_SECRET
# Generate one with: ``openssl rand -base64 32`` (or any random 32+ char string).
DEVICE_SECRET = ""
