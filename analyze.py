#!/usr/bin/env python3
"""ApplyPilot Analytics CLI — parse logs, run queries, track prompt improvements.

Usage:
    ./analyze.sh parse           # Parse all job logs into DB
    ./analyze.sh report          # Full analytics report
    ./analyze.sh report --json  # JSON output
    ./analyze.sh query <name>   # Single query
    ./analyze.sh list-queries   # Show available queries
"""

import sys
from pathlib import Path

# Add src/ to path so we can import applypilot modules
PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT / "src"))

from applypilot.analytics.__init__ import main

if __name__ == "__main__":
    main()
