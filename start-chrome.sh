#!/usr/bin/env bash
# Start Chrome on a specific port with an isolated profile for the apply pipeline.
# Uses a PID file lock so only one instance runs per port.
# Usage: start-chrome.sh [port]
# Default port: 9515

PORT="${1:-9515}"
DATA_DIR="/tmp/chrome-worker-${PORT}"
PID_FILE="/tmp/start-chrome-${PORT}.pid"

# ── PID file lock — prevent duplicate instances for the same port ────────
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        # Another instance is already running for this port
        exit 0
    fi
    # Stale PID — remove it
    rm -f "$PID_FILE"
fi

# Write our PID — do NOT clean up on EXIT. The PID file is the lock;
# without it, a second instance starts and wreaks havoc (duplicate Chrome
# processes opening tabs). Stale PID files are cleaned up by the next
# invocation's pid check above.
echo $$ > "$PID_FILE"

if [ -n "$CHROME_PATH" ]; then
  CHROME_BIN="$CHROME_PATH"
elif command -v chromium &>/dev/null; then
  CHROME_BIN=chromium
elif command -v google-chrome &>/dev/null; then
  CHROME_BIN=google-chrome
elif [ -f "/usr/lib/chromium/chromium" ]; then
  CHROME_BIN=/usr/lib/chromium/chromium
else
  echo "ERROR: No Chrome/Chromium found. Install chromium or set CHROME_PATH."
  exit 1
fi

mkdir -p "$DATA_DIR"

echo "Starting Chrome on port $PORT (profile: $DATA_DIR)..."

# Inject field-cache prefill into all pages (non-blocking, best-effort)
(.venv/bin/python3 "$(dirname "$0")/field_prefill.py" "$PORT" 2>/dev/null || true) &

# Main loop — foreground, so PID file stays valid as long as this runs
while true; do
  "$CHROME_BIN" \
    --remote-debugging-port="$PORT" \
    --user-data-dir="$DATA_DIR" \
    --no-first-run \
    --no-default-browser-check \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-beforeunload \
    --remote-allow-origins=* \
    about:blank
  echo "Chrome (port $PORT) exited, restarting..."
  sleep 1
done
