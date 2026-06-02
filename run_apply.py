#!/usr/bin/env python3
"""ApplyPilot launcher — launches Chrome, applies to all jobs, with optional worker count."""
import sys, os, subprocess, time
sys.path.insert(0, os.path.expanduser("~/Code/applypilot/.venv/lib/python3.11/site-packages"))

from applypilot.config import load_env
load_env()

# Parse --workers flag
workers = 1
if "--workers" in sys.argv:
    idx = sys.argv.index("--workers")
    try:
        workers = int(sys.argv[idx + 1])
        # Remove both args so they don't get passed to main()
        sys.argv.pop(idx + 1)
        sys.argv.pop(idx)
    except (IndexError, ValueError):
        pass

# Launch Chrome on each worker port
from applypilot.apply.chrome import BASE_CDP_PORT, launch_chrome
chrome_procs = []
for i in range(workers):
    port = BASE_CDP_PORT + i
    print(f"Starting Chrome worker {i} on port {port}...")
    proc = launch_chrome(worker_id=i, headless=False)
    chrome_procs.append(proc)
    time.sleep(2)

from applypilot.apply.launcher import main

sys.exit(main(
    min_score=0,        # don't require scoring
    limit=0,            # unlimited
    workers=workers,
    model="haiku",      # ignored — using Hermes
    dry_run=False,
    headless=False,
    continuous=True,
    poll_interval=5,
    target_url=None,
))
