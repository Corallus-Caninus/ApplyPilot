#!/usr/bin/env bash
# Start Chrome on a specific port for the apply pipeline.
# Usage: start-chrome.sh [port]
# Default port: 9515

PORT="${1:-9515}"

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

echo "Starting Chrome on port $PORT via $CHROME_BIN..."

while true; do
  "$CHROME_BIN" \
    --remote-debugging-port="$PORT" \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-beforeunload \
    about:blank
  echo "Chrome (port $PORT) exited, restarting..."
  sleep 1
done
