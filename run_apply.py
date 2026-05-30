#!/usr/bin/env python3
"""ApplyPilot launcher — connects to your existing Chrome on port 9515 and applies to all jobs."""
import sys, os
sys.path.insert(0, os.path.expanduser("~/Code/applypilot/.venv/lib/python3.11/site-packages"))

from applypilot.config import load_env
load_env()

from applypilot.apply.launcher import main

sys.exit(main(
    min_score=0,        # don't require scoring
    limit=0,            # unlimited
    workers=1,
    model="haiku",      # ignored — using Hermes
    dry_run=False,
    headless=False,
    continuous=True,
    poll_interval=5,    # check for new jobs every 5s
    target_url=None,
))
