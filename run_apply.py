#!/usr/bin/env python3
"""ApplyPilot launcher — starts Chrome + MCP, applies to jobs.
   Usage: python3 run_apply.py [--workers N] [--provider P] [--model M] [--no-fallback]

   Starts Chrome and Playwright MCP server with auto-restart wrappers,
   then runs the apply pipeline. Chrome and MCP are killed on exit.

   For the full stack (llama-server + Chrome + MCP + apply), use:
     ./apply_local_llama.sh --model 9b

   That script handles llama-server startup, VRAM checks, and prompt caching.
   This script (run_apply.py) assumes llama-server is already running.

   Default provider priority chain (auto-fallback on failure):
     1. OpenCode Zen (nemotron-3-super-free)              — 12B active MoE, via Zen (no 429 caps)
     2. OpenRouter  (z-ai/glm-4.5-air:free)             — compact MoE fallback
     3. OpenRouter  (nousresearch/hermes-3-llama-3.1-405b:free) — 405B dense, best agentic
     4. OpenRouter  (qwen/qwen3-coder:free)                 — 35B active MoE, excellent tool use, 1M ctx
     5. OpenRouter  (meta-llama/llama-3.3-70b-instruct:free) — 70B dense, strong
     6. OpenRouter  (moonshotai/kimi-k2.6:free)           — MoE, strong long-context
     7. OpenRouter  (google/gemma-4-31b-it:free)        — 31B dense, good (rate-limited)
     8. OpenRouter  (openai/gpt-oss-120b:free)          — 120B dense (was down)
     9. OpenRouter  (nvidia/nemotron-3-super-120b-a12b:free) — fallback via OpenRouter

   Use --provider and/or --model to pin to a single provider (no fallback).

   Session preservation: on provider error (429, timeout, etc.), the session
   is preserved and retried with the next model in the chain. Once all models
   are exhausted, it wraps around to the first and keeps trying until the
   model itself makes a decision (applied, failed:*, expired, captcha, etc.)."""
import sys, os, subprocess, time, signal, atexit
sys.path.insert(0, os.path.expanduser("~/Code/ApplyPilot/src"))
sys.path.insert(0, os.path.expanduser("~/Code/applypilot/.venv/lib/python3.11/site-packages"))

from applypilot.config import load_env
load_env()

from applypilot.config import DEFAULT_PROVIDER_CHAIN

# Parse flags
workers = 1
provider = None
model = None
no_fallback = False
target_url = None
strategy = None

if "--url" in sys.argv:
    idx = sys.argv.index("--url")
    try:
        target_url = sys.argv[idx + 1]
        sys.argv.pop(idx + 1)
        sys.argv.pop(idx)
    except IndexError:
        pass
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
    except (IndexError, ValueError):
        pass
if "--no-fallback" in sys.argv:
    no_fallback = True
    sys.argv.pop(sys.argv.index("--no-fallback"))

if "--strategy" in sys.argv:
    idx = sys.argv.index("--strategy")
    try:
        strategy = sys.argv[idx + 1]
        sys.argv.pop(idx + 1)
        sys.argv.pop(idx)
    except IndexError:
        pass

# Override LLM_URL for local provider (load_env() may have set it from .env)
if provider == "local":
    os.environ["LLM_URL"] = "http://127.0.0.1:11434/v1"
    if model:
        os.environ["LLM_MODEL"] = model

BASE_CDP_PORT = 9516  # use 9516 to avoid conflicting with user's personal Hermes on 9515
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CHROME_SCRIPT = os.path.join(SCRIPT_DIR, "start-chrome.sh")
MCP_SCRIPT = os.path.join(SCRIPT_DIR, "start-mcp.sh")

# ── Start Chrome (with auto-restart via start-chrome.sh PID lock) ────────
chrome_procs = []
for i in range(workers):
    port = BASE_CDP_PORT + i
    if os.path.exists(CHROME_SCRIPT):
        proc = subprocess.Popen(
            ["bash", CHROME_SCRIPT, str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        chrome_procs.append(proc)
        time.sleep(2)

# ── Inject autofill script into Chrome via CDP ──────────────────────────
# Fires on every new document/page load — fills cached fields automatically.
_INJECT_SCRIPT = os.path.join(SCRIPT_DIR, "inject_autofill.py")
if os.path.exists(_INJECT_SCRIPT):
    for i in range(workers):
        port = BASE_CDP_PORT + i
        for attempt in range(10):
            try:
                import requests
                r = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=3)
                if r.status_code == 200:
                    subprocess.Popen(
                        [sys.executable, _INJECT_SCRIPT, str(port)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    break
            except Exception:
                time.sleep(1)

# ── Llama-server health monitor (GPU-aware, auto-restart on crash) ────
# Polls both HTTP endpoint AND GPU VRAM. On GPU fault (VRAM plummets),
# resets the GPU before restarting. Detached process group prevents
# signal propagation from the crashed server.
import threading as _t, urllib.request as _ur, re as _re

def _get_vram_mb() -> float:
    """Return VRAM used in MB by parsing rocm-smi output."""
    try:
        out = subprocess.check_output(
            ["rocm-smi", "--showmeminfo", "vram"],
            timeout=10, stderr=subprocess.DEVNULL,
        ).decode()
        m = _re.search(r"VRAM Total Used Memory \(B\):\s*(\d+)", out)
        if m:
            return int(m.group(1)) / (1024 * 1024)
    except Exception:
        pass
    return 0.0

def _reset_gpu() -> bool:
    """Reset the GPU via rocm-smi. Returns True if reset appeared to work."""
    try:
        subprocess.run(
            ["sudo", "rocm-smi", "--gpureset", "-d", "0"],
            timeout=30, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        )
        time.sleep(8)  # Wait for reset to complete
        vram = _get_vram_mb()
        return vram > 0
    except Exception:
        return False

def _llama_health():
    _script = os.path.join(SCRIPT_DIR, "apply_local_llama.sh")
    _server = None
    # Expected VRAM with model loaded: ~15-16 GB of 17.2 GB
    _VRAM_LOADED_THRESHOLD = 4000  # MB — anything below means GPU fault or model not loaded
    _CONSECUTIVE_FAILURES = 0

    while True:
        # 1. Check HTTP endpoint
        http_alive = False
        try:
            _ur.urlopen("http://127.0.0.1:11434/v1/models", timeout=5)
            http_alive = True
        except Exception:
            pass

        # 2. Check GPU VRAM
        vram = _get_vram_mb()
        gpu_fault = vram > 0 and vram < _VRAM_LOADED_THRESHOLD

        # 3. Decide if restart needed
        needs_restart = False
        if not http_alive:
            _CONSECUTIVE_FAILURES += 1
            if _CONSECUTIVE_FAILURES >= 2:
                needs_restart = True
        else:
            _CONSECUTIVE_FAILURES = 0

        # Even if HTTP is alive, if VRAM dropped to fault level, restart
        if gpu_fault:
            needs_restart = True

        if needs_restart:
            # GPU fault detected or server confirmed dead
            if gpu_fault or vram < 100:
                print("[llama-monitor] GPU fault detected — resetting GPU...", flush=True)
                _reset_gpu()
            elif not http_alive:
                # HTTP down but VRAM looks OK — maybe the process crashed
                # but GPU is still healthy. Still reset to be safe.
                print("[llama-monitor] llama-server not responding — resetting GPU...", flush=True)
                _reset_gpu()

            # Kill leftover processes on the port
            subprocess.run(
                ["fuser", "-k", "11434/tcp"],
                timeout=5, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            )
            time.sleep(1)

            # Start new server — detached process group so GPU faults
            # don't propagate back to this monitor or run_apply.py
            _flag = model or "9b"
            print(f"[llama-monitor] Starting llama-server (--model {_flag})...", flush=True)
            _server = subprocess.Popen(
                ["bash", _script, "--model", _flag, "--server-only"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,  # KEY: detach from our process group
            )

            # Wait for server to come online (up to 5 min for model load)
            _start = time.time()
            _online = False
            while time.time() - _start < 300:
                try:
                    _ur.urlopen("http://127.0.0.1:11434/v1/models", timeout=3)
                    _online = True
                    break
                except Exception:
                    time.sleep(2)
                    # If process died, bail fast
                    if _server.poll() is not None:
                        print(f"[llama-monitor] New server died (exit={_server.poll()}) — will retry", flush=True)
                        break

            if _online:
                print(f"[llama-monitor] Server back online after {time.time()-_start:.0f}s", flush=True)
                _CONSECUTIVE_FAILURES = 0
            else:
                print(f"[llama-monitor] Server failed to come online", flush=True)

        time.sleep(15)

_t.Thread(target=_llama_health, daemon=True).start()

# ── Cleanup on exit ────────────────────────────────────────────────────
def _cleanup():
    for p in chrome_procs:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, AttributeError):
            pass
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
    continuous=True if not target_url else False,
    poll_interval=5,
    target_url=target_url,
    strategy=strategy,
))
