#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Kill llama-server-wrapper.sh and its child processes.
# Usage:  ./kill-llama-server-wrapper.sh [--force|-9]
# ──────────────────────────────────────────────────────────────────────────────

SIGNAL="-15"  # SIGTERM
if [ "$1" = "--force" ] || [ "$1" = "-9" ]; then
    SIGNAL="-9"
fi

# Find wrapper PIDs (exclude grep and this script itself)
PIDS=$(pgrep -f "llama-server-wrapper" | grep -v $$ 2>/dev/null)

if [ -z "$PIDS" ]; then
    echo "llama-server-wrapper is not running."
    exit 0
fi

echo "Killing llama-server-wrapper (PID(s): $PIDS)..."
for PID in $PIDS; do
    # Kill the child processes first (llama-server, env, etc.)
    CHILDREN=$(pgrep -P "$PID" 2>/dev/null)
    if [ -n "$CHILDREN" ]; then
        kill $SIGNAL $CHILDREN 2>/dev/null
    fi
    # Then kill the wrapper itself
    kill $SIGNAL $PID 2>/dev/null
done

sleep 1

# Check if any survived
REMAINING=$(pgrep -f "llama-server-wrapper" | grep -v $$ 2>/dev/null)
if [ -n "$REMAINING" ]; then
    echo "Some processes survived — forcing kill..."
    kill -9 $REMAINING 2>/dev/null
    kill -9 $(pgrep -P $REMAINING 2>/dev/null) 2>/dev/null
fi

echo "Done."
