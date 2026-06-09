#!/usr/bin/env bash
# Start Playwright MCP server on a specific port with auto-restart loop.
# Matches the same pattern as start-chrome.sh.
# Usage: start-mcp.sh [port]
# Default port: 9515

PORT="${1:-9515}"
PID_FILE="/tmp/start-mcp-${PORT}.pid"

# ── PID file lock — prevent duplicate instances for the same port ────────
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        exit 0
    fi
    rm -f "$PID_FILE"
fi

echo $$ > "$PID_FILE"

cd "$(dirname "$0")" || exit 1

echo "Starting Playwright MCP server on port ${PORT}..."

# Foreground loop — PID file stays valid
while true; do
  npx -y @playwright/mcp@latest \
    --cdp-endpoint="http://localhost:${PORT}" \
    --viewport-size=1280x900
  echo "Playwright MCP (port ${PORT}) exited, restarting..."
  sleep 1
done
