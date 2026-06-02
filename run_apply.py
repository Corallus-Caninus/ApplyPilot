#!/usr/bin/env python3
"""ApplyPilot launcher — starts Chrome, applies to jobs, optional workers flag.
   Usage: python3 run_apply.py [--workers N]"""
import sys, os, subprocess, time, signal
sys.path.insert(0, os.path.expanduser("~/Code/applypilot/.venv/lib/python3.11/site-packages"))

from applypilot.config import load_env
load_env()

# Parse --workers flag
workers = 1
if "--workers" in sys.argv:
    idx = sys.argv.index("--workers")
    try:
        workers = int(sys.argv[idx + 1])
        sys.argv.pop(idx + 1)
        sys.argv.pop(idx)
    except (IndexError, ValueError):
        pass

BASE_CDP_PORT = 9515
CHROME_SCRIPT = os.path.expanduser("~/Code/applypilot/start-chrome.sh")

# Start Chrome for each worker
chrome_procs = []
for i in range(workers):
    port = BASE_CDP_PORT + i
    print(f"Starting Chrome worker {i} on port {port}...")
    proc = subprocess.Popen(
        ["bash", CHROME_SCRIPT, str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    chrome_procs.append(proc)
    time.sleep(2)

# Kill Chrome on exit
def _cleanup():
    for p in chrome_procs:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, AttributeError):
            pass
import atexit
atexit.register(_cleanup)

from applypilot.apply.launcher import main

sys.exit(main(
    min_score=0,
    limit=0,
    workers=workers,
    model="haiku",
    dry_run=False,
    headless=False,
    continuous=True,
    poll_interval=5,
    target_url=None,
))
