#!/usr/bin/env python3
"""ApplyPilot launcher — starts Chrome, applies to jobs, optional workers flag.
   Usage: python3 run_apply.py [--workers N] [--provider P] [--model M] [--no-fallback]

   Default provider priority chain (auto-fallback on failure):
     1. OpenRouter  (meta-llama/llama-3.3-70b-instruct:free) — free, dense
     2. OpenRouter  (openai/gpt-oss-120b:free)          — free, dense
     3. OpenRouter  (nvidia/nemotron-3-super-120b-a12b:free) — free, MoE
     4. OpenCode Go  (deepseek-v4-flash)                — $10/mo, unlimited

   Use --provider and/or --model to pin to a single provider (no fallback)."""
import sys, os, subprocess, time, signal
sys.path.insert(0, os.path.expanduser("~/Code/applypilot/.venv/lib/python3.11/site-packages"))

from applypilot.config import load_env
load_env()

# Default priority chain: highest priority first
DEFAULT_PROVIDER_CHAIN = [
    ("openrouter", "meta-llama/llama-3.3-70b-instruct:free"),
    ("openrouter", "openai/gpt-oss-120b:free"),
    ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free"),
    ("opencode-go", "deepseek-v4-flash"),
]

# Parse flags
workers = 1
provider = None
model = None
no_fallback = False

if "--workers" in sys.argv:
    idx = sys.argv.index("--workers")
    try:
        workers = int(sys.argv[idx + 1])
        sys.argv.pop(idx + 1)
        sys.argv.pop(idx)
    except (IndexError, ValueError):
        pass
if "--provider" in sys.argv:
    idx = sys.argv.index("--provider")
    try:
        provider = sys.argv[idx + 1]
        sys.argv.pop(idx + 1)
        sys.argv.pop(idx)
    except IndexError:
        pass
if "--model" in sys.argv:
    idx = sys.argv.index("--model")
    try:
        model = sys.argv[idx + 1]
        sys.argv.pop(idx + 1)
        sys.argv.pop(idx)
    except IndexError:
        pass
if "--no-fallback" in sys.argv:
    no_fallback = True
    sys.argv.pop(sys.argv.index("--no-fallback"))

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

# Build provider chain:
#   --provider specified → single-entry chain (preserves existing behavior)
#   --no-fallback       → single-entry chain from --provider/--model or default
#   otherwise           → full priority chain
if provider or no_fallback:
    provider_chain = [(provider or DEFAULT_PROVIDER_CHAIN[0][0],
                       model or DEFAULT_PROVIDER_CHAIN[0][1])]
else:
    provider_chain = list(DEFAULT_PROVIDER_CHAIN)
    if model:
        # Override the model on the first entry (highest priority), keep fallbacks
        provider_chain[0] = (provider_chain[0][0], model)

sys.exit(main(
    min_score=0,
    limit=0,
    workers=workers,
    provider_chain=provider_chain,
    dry_run=False,
    headless=False,
    continuous=True,
    poll_interval=5,
    target_url=None,
))
