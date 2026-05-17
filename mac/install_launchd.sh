#!/usr/bin/env bash
# Install claude-pull as a launchd agent that runs every 60 seconds.
#
# Idempotent — safe to re-run on upgrade. Bootstraps a per-user
# launchd job, no sudo required.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${HERE}/claude-pull"
PLIST_SRC="${HERE}/com.claude.pager.pull.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/com.claude.pager.pull.plist"
LABEL="com.claude.pager.pull"
CONFIG_DIR="${HOME}/.config/claude-pager"
CONFIG_PATH="${CONFIG_DIR}/config.json"

if [[ ! -x "${SCRIPT}" ]]; then
  chmod +x "${SCRIPT}"
fi

# 1) Make sure config exists; if not, write a stub and exit so the
#    user fills it in before launchd starts spamming the worker.
if [[ ! -f "${CONFIG_PATH}" ]]; then
  mkdir -p "${CONFIG_DIR}"
  cat > "${CONFIG_PATH}" <<'JSON'
{
  "worker_base": "https://REPLACE-ME.workers.dev",
  "device_secret": "REPLACE_ME",
  "out_dir": "~/ClaudeRuns",
  "notify": true
}
JSON
  echo
  echo "Wrote stub config to ${CONFIG_PATH}"
  echo "Edit it (set worker_base + device_secret), then re-run this script."
  exit 0
fi

# Sanity-check the config has been edited.
if grep -q "REPLACE" "${CONFIG_PATH}"; then
  echo "Refusing to install: ${CONFIG_PATH} still has REPLACE-ME placeholders."
  echo "Edit it first."
  exit 1
fi

# 2) Render the plist with the real script path.
mkdir -p "$(dirname "${PLIST_DST}")"
sed "s|__CLAUDE_PULL_PATH__|${SCRIPT}|g" "${PLIST_SRC}" > "${PLIST_DST}"

# 3) (Re)bootstrap the job. `bootout` is permissive — it returns
#    non-zero if the job wasn't loaded, which is fine on first run.
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${PLIST_DST}"

# 4) Kick it once so the user sees results immediately, not 60s from now.
launchctl kickstart -k "gui/$(id -u)/${LABEL}"

echo
echo "Installed launchd agent: ${LABEL}"
echo "  plist:    ${PLIST_DST}"
echo "  script:   ${SCRIPT}"
echo "  config:   ${CONFIG_PATH}"
echo "  out dir:  $(python3 -c 'import json,os;print(os.path.expanduser(json.load(open("'"${CONFIG_PATH}"'"))["out_dir"]))' 2>/dev/null || echo "~/ClaudeRuns")"
echo
echo "Logs:"
echo "  tail -f /tmp/claude-pull.out.log /tmp/claude-pull.err.log"
echo
echo "Run it manually anytime:"
echo "  ${SCRIPT} -v"
echo
echo "Stop / uninstall:"
echo "  launchctl bootout gui/\$(id -u)/${LABEL}"
echo "  rm ${PLIST_DST}"
