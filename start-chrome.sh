#!/usr/bin/env bash
set -euo pipefail

# start-chrome.sh — Launch Chrome for ApplyPilot with remote debugging on port 9515.
# Run this FIRST, then run ./applypilot.sh to start applying.
#
# Usage:
#   ./start-chrome.sh              # launch with existing profile
#   ./start-chrome.sh --clean      # launch with a fresh profile (re-login needed)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT=9515
USER_DATA_DIR="${HOME}/.config/chromium-applypilot"
CLEAN="${1:-}"

if [ "$CLEAN" = "--clean" ]; then
  USER_DATA_DIR="/tmp/applypilot-chromium-$$"
  echo "[start-chrome] Using CLEAN profile: $USER_DATA_DIR"
fi

# Kill any existing chromium on our port to avoid conflicts
pkill -f "remote-debugging-port=${PORT}" 2>/dev/null || true
sleep 1

# Find chromium binary
CHROMIUM=""
for candidate in \
  /nix/store/*-chromium-*/bin/chromium \
  /nix/store/*-chromium-*/bin/chromium-browser \
  /usr/bin/chromium \
  /usr/bin/chromium-browser \
  /snap/bin/chromium \
  /etc/profiles/per-user/jward/bin/google-chrome-stable \
  /etc/profiles/per-user/jward/bin/chromium; do
  if [ -x "$candidate" ]; then
    CHROMIUM="$candidate"
    break
  fi
done

if [ -z "$CHROMIUM" ]; then
  CHROMIUM="$(command -v google-chrome-stable 2>/dev/null || command -v chromium-browser 2>/dev/null || command -v chromium 2>/dev/null || true)"
fi

if [ -z "$CHROMIUM" ]; then
  echo "[start-chrome] ERROR: No Chrome/Chromium binary found"
  exit 1
fi

echo "[start-chrome] Binary: $CHROMIUM"
echo "[start-chrome] Port:   $PORT"
echo "[start-chrome] Profile: $USER_DATA_DIR"
echo "[start-chrome]"
echo "[start-chrome] Chrome is starting with remote debugging on port $PORT."
echo "[start-chrome] Run ./applypilot.sh in another terminal to start applying."
echo ""

# Clean up Singleton* locks that can prevent startup
for f in SingletonLock SingletonSocket SingletonCookie; do
  lock="${USER_DATA_DIR}/${f}"
  [ -f "$lock" ] && rm -f "$lock" 2>/dev/null && echo "[start-chrome] Removed stale: $lock"
done

RESTART_COUNTER_FILE="/tmp/chromium-restart-count"
MAX_RESTARTS=10
RESTART_DELAY=2

# Loop to auto-restart if Chrome crashes
while true; do
  # Track restarts to prevent rapid crash loops
  if [ -f "$RESTART_COUNTER_FILE" ]; then
    count=$(cat "$RESTART_COUNTER_FILE" 2>/dev/null || echo 0)
    if [ "$count" -ge "$MAX_RESTARTS" ]; then
      echo "[start-chrome] Too many restarts ($count). Giving up."
      exit 1
    fi
  else
    count=0
  fi
  echo $((count + 1)) > "$RESTART_COUNTER_FILE"

  echo "[start-chrome] Starting Chromium (attempt $((count + 1)))..."
  "$CHROMIUM" \
    --remote-debugging-port="${PORT}" \
    --user-data-dir="${USER_DATA_DIR}" \
    --no-first-run \
    --no-default-browser-check \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-extensions \
    --disable-popup-blocking \
    --disable-setuid-sandbox \
    --disable-crashpad \
    --crash-dumps-dir=/tmp/chromium-crashes \
    --disable-background-networking \
    --disable-sync \
    --metrics-recording-only \
    --window-size=1920,1080 \
    --disable-blink-features=AutomationControlled \
    --disable-renderer-backgrounding \
    --disable-component-update \
    --disable-session-crashed-bubble \
    --disable-restore-session-state \
    --disable-gpu \
    --disable-features=ChromeWhatsNew,ChromeToaster,SearchEngineChoiceScreen,ChromeLabs \
    --hide-crash-restore-bubble \
    --remote-allow-origins="*" \
    "https://www.google.com" &
  CHROME_PID=$!
  echo "[start-chrome] PID: $CHROME_PID"

  # Wait for Chrome to be listening on the port before declaring success
  for i in $(seq 1 10); do
    if ss -tlnp 2>/dev/null | grep -q ":${PORT} "; then
      echo "[start-chrome] Ready on port ${PORT}."
      break
    fi
    sleep 1
  done

  # Wait for Chrome to exit, then auto-restart
  wait $CHROME_PID 2>/dev/null
  EXIT_CODE=$?
  echo "[start-chrome] Chromium exited (code: $EXIT_CODE). Restarting in ${RESTART_DELAY}s..."
  sleep "$RESTART_DELAY"
done
