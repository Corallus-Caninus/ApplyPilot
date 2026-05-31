#!/usr/bin/env bash
set -euo pipefail

# ApplyPilot — one command to start applying to jobs
# Uses belt_fed + Hermes with the apply prompt
#
# Prerequisites:
#   ./start-chrome.sh  (in another terminal)
#
# Usage:
#   ./applypilot.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check Chrome is running on port 9515
if ! curl -s http://127.0.0.1:9515/json/version >/dev/null 2>&1; then
  echo "[applypilot] Chrome not running. Start it first:"
  echo "  cd ~/Code/applypilot && ./start-chrome.sh"
  exit 1
fi

echo "[applypilot] Chrome is running on port 9515."
echo "[applypilot] Starting apply loop (Ctrl+C to stop)..."
echo ""

# Run belt_fed in --link mode with the apply prompt
# This keeps the same session across restarts (--continue)
cat "$SCRIPT_DIR/applypilot_prompt.txt" | "$SCRIPT_DIR/../hermes/belt_fed.sh" --link
