"""Apply orchestration: acquire jobs, spawn Claude Code sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Claude Code for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import json
import logging
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Literal
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.database import get_connection
from applypilot.apply import chrome, dashboard, prompt as prompt_mod
from applypilot.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome,
    reset_worker_dir, cleanup_on_exit, _kill_process_tree,
    BASE_CDP_PORT,
)
from applypilot.apply.dashboard import (
    init_worker, update_state, add_event, get_state,
    add_completed_job, render_full, get_totals,
)

logger = logging.getLogger(__name__)

# ── Background provider prober ──────────────────────────────────────────────────
# Polls the highest-priority provider every 30s. When it recovers, we switch back.
# This runs regardless of whether a job is in progress.
_prober_available: tuple[str, str] | Literal[False] | None = None  # (provider, model) or None if unavailable
_prober_lock = threading.Lock()
_PROBER_INTERVAL = 30

def _prober_thread_fn(chain: list) -> None:
    """Background thread: probes highest-priority provider every 30s."""
    global _prober_available
    import time as _time
    # Initial probe immediately, then every 30s after that
    while True:
        if chain:
            prov, model = chain[0]
            ok = _probe_provider(prov, model)
            with _prober_lock:
                _prober_available = (prov, model) if ok else False
        _time.sleep(_PROBER_INTERVAL)

def _start_prober(chain: list) -> None:
    """Start the background prober daemon thread.

    Runs an initial probe synchronously so the result is available immediately,
    then continues polling every 30s in the background.
    """
    global _prober_available
    # Do the first probe synchronously — no race
    if chain:
        prov, model = chain[0]
        ok = _probe_provider(prov, model)
        _prober_available = (prov, model) if ok else False
    # Background thread for subsequent probes
    t = threading.Thread(target=_prober_thread_fn, args=(chain,), daemon=True)
    t.start()

def _get_best_available():
    """Get the highest-priority provider that's currently available.
    
    Returns:
        (provider, model) if available,
        False if probed and unavailable,
        None if not yet probed (first 30s).
    """
    with _prober_lock:
        return _prober_available

# ── Provider fallback chain ─────────────────────────────────────────────────────
# Patterns in Hermes/LLM output that indicate a provider error (rate-limit,
# outage, auth failure, model unavailable, etc.) — triggers fallback to next
# provider in the chain.
PROVIDER_ERROR_PATTERNS = [
    "rate limit", "rate_limit", "ratelimit",
    "429", "503", "502", "500",
    "too many requests",
    "overloaded",
    "try again later",
    "quota exceeded",
    "please slow down",
    "retry after",
    "retry-after",
    "no response",
    "connection refused",
    "connection reset",
    "timeout",
    "timed out",
    "upstream error",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "model is not supported",
    "model not found",
    "not supported",
    "invalid_api_key",
    "authentication_error",
    "provider_error",
    "api_error",
    "server_error",
    "capacity",
    "currently unavailable",
    "try again",
    "temporarily unavailable",
    "no available provider",
    "all providers failed",
    "error code: 40",
    "error code: 50",
    "non-retryable",
]


def detect_provider_error(output: str) -> bool:
    """Check if Hermes output indicates a provider/model error that should trigger fallback."""
    if not output:
        return True  # empty output = something went wrong
    lower = output.lower()
    return any(p in lower for p in PROVIDER_ERROR_PATTERNS)


def _build_provider_cmd(hermes_path: str, provider: str, model: str,
                        agent_prompt: str,
                        worker_id: int = 0,
                        chain_len: int = 1) -> tuple[list[str], dict]:
    """Build Hermes CLI command + environment for a specific provider/model.

    Always creates a temporary Hermes config that sets ``agent.api_max_retries: 1``
    so Hermes does exactly 1 API attempt per provider — no exponential-backoff
    retries.  The launcher's own fallback chain handles switching providers when
    one fails, so Hermes should fail fast and let us cut to the next provider.

    The temp config is a copy of the real config with only the retry setting
    overridden, so all other Hermes settings (providers, model defaults, etc.)
    are preserved.
    """
    import tempfile as _tempfile
    import yaml as _yaml
    from pathlib import Path

    _tmpdir = _tempfile.mkdtemp(prefix="hermes-applypilot-")

    # Start with a copy of the real config, then override
    _real_cfg_path = Path.home() / ".hermes" / "config.yaml"
    if _real_cfg_path.exists():
        with open(_real_cfg_path) as _fc:
            _cfg: dict = _yaml.safe_load(_fc) or {}
    else:
        _cfg = {}
    # Allow retries for transient errors (429 rate limits, etc.)
    # The launcher's own fallback chain handles permanent provider failures,
    # so Hermes should retry a few times before giving up on a provider.
    # When there's only one provider in the chain, retry effectively forever
    # (999999) since exiting means losing the session and all form progress.
    _cfg.setdefault("agent", {})["api_max_retries"] = 999999 if chain_len <= 1 else 3

    # Restrict tools to only what the apply agent needs — this shrinks the
    # system prompt significantly (fewer tool schema definitions = less context).
    # The agent only needs: browser (MCP Playwright), terminal (scripts),
    # file (reading creds), and vision (CAPTCHA reading).
    _cfg.setdefault("agent", {})["disabled_toolsets"] = [
        "browser", "browser-cdp", "clarify", "code_execution", "computer_use",
        "cronjob", "delegation", "discord", "discord_admin",
        "feishu_doc", "feishu_drive", "homeassistant", "image_gen",
        "kanban", "memory", "messaging", "moa", "session_search",
        "skills", "todo", "tts", "video", "video_gen",
        "vision", "web",
    ]
    # Prevent NixOS read-only filesystem errors — lazy installs try to
    # write to the Nix store via uv pip install.
    _cfg.setdefault("security", {})["allow_lazy_installs"] = False

    cmd = [hermes_path, "chat", "-v", "--pass-session-id"]
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    # Raise API request timeout from 1800s (30 min) default to 3600s (1 hour)
    # to prevent client-side disconnection mid-stream on slow local models.
    env["HERMES_API_TIMEOUT"] = "3600"

    if provider:
        env["LLM_PROVIDER"] = provider

    if provider == "opencode" or provider == "opencode-zen":
        cmd += ["-m", model or "nemotron-3-super-free"]
        env["HERMES_MODE"] = "zen"
    elif provider == "opencode-go":
        cmd += ["-m", model or "deepseek-v4-flash"]
        env["HERMES_MODE"] = "go"
    elif provider == "gemini":
        _gemini_key = os.environ.get("GEMINI_API_KEY", "")
        _cfg["model"] = {
            "provider": "custom",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "default": model or "gemini-2.5-flash",
            "api_key": _gemini_key,
        }
        env.pop("HERMES_MODE", None)
        cmd += ["-m", model or "gemini-2.5-flash"]
    elif provider == "openrouter":
        cmd += ["--provider", "openrouter"]
        cmd += ["-m", model or "openrouter/owl-alpha"]
    elif provider == "local":
        _local_url = os.environ.get("LLM_URL", "http://localhost:11434/v1")
        _local_key = os.environ.get("LLM_API_KEY", "")
        _cfg["model"] = {
            "provider": "custom",
            "base_url": _local_url,
            "default": model or "qwen3.5:4b",
        }
        if _local_key:
            _cfg["model"]["api_key"] = _local_key
        env.pop("HERMES_MODE", None)
        cmd += ["-m", model or "qwen3.5:4b"]
        # Disable streaming for local models — their JSON output can be
        # malformed mid-stream (especially when embedding JS code in tool
        # call args), which kills the session.  Non-streaming gives the
        # model the error message and lets it retry naturally.
        _cfg.setdefault("display", {})["streaming"] = False
        # Set context length based on model size — MI25 has 16GB VRAM.
        # 4B Q4 (~3.4GB) can handle 128K+; 9B Q4 (~5.6GB) fits 128K with Q8_0 KV cache.
        # MoE models (LFM, etc.) have smaller KV cache per active param → full 128K.
        _model_name = (model or "qwen3.5:4b").lower()
        if "35b" in _model_name or "27b" in _model_name:
            _ctx = 16384
        elif "lfm" in _model_name:
            _ctx = 128000   # MoE with 1B active → very small KV, full 128K fits
        elif "9b" in _model_name or "8b" in _model_name:
            _ctx = 64000   # Hermes budget — server has 96K, never returns 400
        else:
            _ctx = 64000
        _cfg.setdefault("model", {}).setdefault("context_length", _ctx)
        _cfg["model"]["context_length"] = _ctx
        # Hermes manages a 64K token budget; llama-server has 96K headroom.
        # Preflight compression fires at 90% of Hermes' budget (~57.6K).
        # This runs BEFORE the API call (conversation_loop.py:430), so
        # the server is idle — compression is the ONLY thing running.
        # Give it a generous timeout so the LLM summary actually finishes
        # instead of leaving a stale task that triggers should_stop.
        _cfg.setdefault("agent", {}).setdefault("context_compressor", {})
        _cfg["agent"]["context_compressor"]["enabled"] = True
        _cfg["agent"]["context_compressor"]["threshold"] = 0.90
        _cfg["compression"] = {
            "enabled": True,
        }
        # Pin all auxiliary models to the same local provider — otherwise they
        # default to 'auto' which tries OpenCode API and fails with 401.
        _aux_cfg = {
            "provider": "custom",
            "base_url": _local_url,
            "model": model or "qwen3.5:9b",
        }
        if _local_key:
            _aux_cfg["api_key"] = _local_key
        for _aux_key in ("vision", "web_extract", "compression", "skills_hub",
                         "approval", "mcp", "title_generation", "triage_specifier",
                         "kanban_decomposer", "profile_describer", "curator"):
            _cfg.setdefault("auxiliary", {}).setdefault(_aux_key, {}).update(_aux_cfg)
        # Compression runs as the ONLY request on the server (preflight).
        # Give it a generous timeout so the LLM summary completes instead
        # of leaving a stale task that triggers should_stop on the next call.
        _cfg["auxiliary"]["compression"]["timeout"] = None  # no timeout — server is idle, only request running
        # Register Playwright MCP server — Hermes manages its lifecycle
        _cfg.setdefault("mcp_servers", {}).setdefault("playwright", {
            "command": "npx",
            "args": ["-y", "@playwright/mcp@latest",
                     "--cdp-endpoint=http://localhost:9515",
                     "--viewport-size=1280x900"],
            "timeout": 300,
            "connect_timeout": 30,
        })
        # Register credential + email tools as a native MCP server
        _mcp_py = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../applypilot_mcp_server.py"))
        if os.path.exists(_mcp_py):
            _cfg.setdefault("mcp_servers", {})["applypilot"] = {
                "command": sys.executable,
                "args": [_mcp_py],
                "timeout": 30,
            }
    else:
        if provider:
            cmd += ["--provider", provider]
        if model:
            cmd += ["-m", model]

    cmd += ["-q", agent_prompt]
    if model:
        env["LLM_MODEL"] = model

    # Write the temp config and point HERMES_HOME to a persistent per-worker dir
    # so sessions survive across retries (--resume works).
    # For opencode-zen, override base_url to Zen API (not Go) without touching
    # the global config, so the user's personal Go sessions are unaffected.
    if provider == "opencode-zen":
        _cfg.setdefault("model", {})["base_url"] = "https://opencode.ai/zen/v1"
    # ── ApplyPilot tweaks to save tokens and avoid hard-stops ─────────
    # Disable holographic memory (fact retrieval/creation wastes tokens)
    _cfg.setdefault("memory", {})["memory_enabled"] = False
    _cfg.setdefault("memory", {})["user_profile_enabled"] = False
    _cfg.setdefault("memory", {})["nudge_interval"] = 0
    # Disable auto-extract in memory plugin (fork+fact_add per turn)
    _cfg.setdefault("plugins", {}).pop("hermes-memory-store", None)
    # Disable tool-loop hard-stop — apply agents need many iterations
    _cfg.setdefault("tool_loop_guardrails", {})["hard_stop_enabled"] = False
    # ──────────────────────────────────────────────────────────────────
    _hermes_home = os.path.join(str(config.APP_DIR), f"hermes-home-{worker_id}")
    os.makedirs(_hermes_home, exist_ok=True)
    _tmp_cfg = os.path.join(_hermes_home, "config.yaml")
    with open(_tmp_cfg, "w") as _f:
        _yaml.dump(_cfg, _f, default_flow_style=False, sort_keys=False)
    env["HERMES_HOME"] = _hermes_home

    return cmd, env


def _probe_provider(provider: str, model: str) -> bool:
    """Quickly check if a provider/model is available by making a minimal API call.
    
    Returns True if the provider responds, False otherwise.
    """
    import urllib.request
    import urllib.error
    import json as _json

    try:
        if provider == "openrouter":
            # Actually probe the specific model with a real chat completion call
            api_key = os.environ.get("OPENROUTER_API_KEY", "")
            if not api_key:
                return False
            body = _json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": "ok"}],
                "max_tokens": 1,
            }).encode()
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": "hermes-agent-probe/1.0",
                },
            )
            resp = urllib.request.urlopen(req, timeout=10)
            return resp.status == 200
        elif provider == "gemini":
            api_key = os.environ.get("GEMINI_API_KEY", "")
            if not api_key:
                return False
            body = _json.dumps({
                "model": model or "gemini-2.5-flash",
                "messages": [{"role": "user", "content": "ok"}],
                "max_tokens": 1,
            }).encode()
            req = urllib.request.Request(
                "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            resp = urllib.request.urlopen(req, timeout=10)
            return resp.status == 200
        elif provider in ("opencode", "opencode-zen", "opencode-go"):
            # OpenCode — hit the models endpoint with the API key
            key = ""
            key_path = os.path.expanduser("~/Code/hermes/opencode-go-key")
            if os.path.isfile(key_path):
                key = open(key_path).read().strip()
            if not key:
                key = os.environ.get("OPENCODE_API_KEY", "")
            if not key:
                return False
            base = "https://opencode.ai/zen/v1"
            req = urllib.request.Request(
                f"{base}/models",
                headers={
                    "Authorization": f"Bearer {key}",
                    "User-Agent": "hermes-agent-probe/1.0",
                },
            )
            resp = urllib.request.urlopen(req, timeout=10)
            return resp.status == 200
        elif provider == "local":
            _local_url = os.environ.get("LLM_URL", "http://localhost:11434/v1").rstrip("/")
            req = urllib.request.Request(
                f"{_local_url}/models",
                method="GET",
            )
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status == 200
        else:
            return False
    except Exception:
        return False


# ── Mid-job provider switch ────────────────────────────────────────────────────

def _mid_job_switch_prober(proc, chain: list, current_idx: int,
                           switch_file: str,
                           worker_id: int = 0) -> None:
    """Background daemon: probes higher-priority providers every 30s during a job.
    
    If a higher-priority provider recovers, writes a switch request to the
    Hermes switch file. Hermes picks it up before the next API call and
    seamlessly swaps model/provider/base_url mid-conversation.
    """
    import json
    
    if current_idx == 0:
        return  # already on the highest-priority provider, nothing to switch to
    
    while proc and proc.poll() is None:
        # Wait 30s between probes
        import time as _time
        _time.sleep(30)
        
        if proc.poll() is not None:
            return  # process already exited
        
        # Check each higher-priority provider (lower index = higher priority)
        for idx in range(current_idx):
            prov, model = chain[idx]
            if _probe_provider(prov, model):
                payload = json.dumps({
                    "provider": prov,
                    "model": model,
                })
                try:
                    with open(switch_file, "w") as f:
                        f.write(payload)
                    add_event(f"[W{worker_id}] {prov}/{model} recovered — queued seamless switch")
                except OSError as e:
                    add_event(f"[W{worker_id}] Failed to write switch file: {e}")
                return

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Track active Claude Code processes for skip (Ctrl+C) handling
_claude_procs: dict[int, subprocess.Popen] = {}
_claude_lock = threading.Lock()

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

def _capture_fields_async(worker_id: int, session_id: str = "") -> None:
    """Extract filled form fields from the agent's state.db and save to field cache.
    
    Queries the Hermes session database for all mcp_playwright_browser_fill_form
    tool calls and extracts the field name→value pairs the agent actually used.
    If session_id is empty, uses the most recent session with fill_form data.
    Stores in the field_cache table of applypilot.db.
    """
    hermes_home = os.path.join(str(config.APP_DIR), f"hermes-home-{worker_id}")
    state_db = os.path.join(hermes_home, "state.db")
    field_db = os.path.join(str(config.APP_DIR), "applypilot.db")
    if not os.path.exists(state_db):
        return

    def _get_session(cur):
        if session_id:
            return session_id
        # Find the most recent session with fill_form or select_option data
        cur.execute("""SELECT session_id FROM messages 
            WHERE role='assistant' AND tool_calls IS NOT NULL
              AND (tool_calls LIKE '%fill_form%' OR tool_calls LIKE '%select_option%')
            ORDER BY id DESC LIMIT 1""")
        row = cur.fetchone()
        return row[0] if row else ""

    def _do_capture():
        time.sleep(2)  # Let agent finish writing to DB
        try:
            import sqlite3, json
            conn = sqlite3.connect(state_db)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            sid = _get_session(cur)
            if not sid:
                conn.close()
                return

            cur.execute("""SELECT tool_calls FROM messages 
                WHERE session_id=? AND role='assistant' AND tool_calls IS NOT NULL 
                  AND (tool_calls LIKE '%fill_form%' OR tool_calls LIKE '%select_option%')
                ORDER BY id""", (sid,))
            rows = cur.fetchall()
            conn.close()

            if not rows:
                return

            import re
            norm_re = re.compile(r"[*\s_\-]+")
            def norm(s):
                return norm_re.sub("", s).strip().lower()

            now = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            new_count = 0
            # Load existing keys from the DB to avoid duplicates
            existing = set()
            dc = None
            try:
                dc = sqlite3.connect(field_db)
                for r2 in dc.execute("SELECT label FROM field_cache"):
                    existing.add(r2[0])
            except Exception:
                pass

            for row in rows:
                try:
                    data = json.loads(row["tool_calls"])
                    for call in data:
                        fn = call.get("function", {})
                        name = fn.get("name", "")
                        args_raw = fn.get("arguments", "{}")
                        if isinstance(args_raw, str):
                            args = json.loads(args_raw)
                        else:
                            args = args_raw
                        if "select_option" in name:
                            # select_option: {"target": "e65", "values": ["Remote"]}
                            vals = args.get("values", [])
                            if vals:
                                key = norm(args.get("target", ""))
                                if key and key not in existing:
                                    dc.execute(
                                        "INSERT OR IGNORE INTO field_cache (label, value, created_at, updated_at, source_session) VALUES (?, ?, ?, ?, ?)",
                                        (key, vals[0].strip(), now, now, sid),
                                    )
                                    if dc.rowcount:
                                        existing.add(key)
                                        new_count += 1
                        else:
                            # fill_form: {"fields": [{"name": "First Name", "value": "Josh"}]}
                            fields = args.get("fields", [])
                            if isinstance(fields, str):
                                fields = json.loads(fields)
                            for f in fields:
                                label = f.get("name") or f.get("element", "")
                                val = f.get("value", "")
                                key = norm(label)
                                if key and val and key not in existing:
                                    try:
                                        dc.execute(
                                            "INSERT OR IGNORE INTO field_cache (label, value, created_at, updated_at, source_session) VALUES (?, ?, ?, ?, ?)",
                                            (key, val.strip(), now, now, sid),
                                        )
                                        if dc.rowcount:
                                            existing.add(key)
                                            new_count += 1
                                    except Exception:
                                        pass
                except (json.JSONDecodeError, KeyError):
                    pass

            if new_count:
                dc.commit()
            if dc:
                dc.close()
        except Exception:
            pass  # best-effort

    t = threading.Thread(target=_do_capture, daemon=True)
    t.start()

def _make_mcp_config(cdp_port: int) -> dict:
    """Build MCP config dict for a specific CDP port."""
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    "@playwright/mcp@latest",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            },
            "gmail": {
                "command": "npx",
                "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
            },
        }
    }


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def acquire_job(target_url: str | None = None, min_score: int = 7,
                worker_id: int = 0, strategy: str | None = None) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).
        strategy: Only acquire jobs from this discovery strategy (bigtech, jobspy, workday_api).

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        if target_url:
            like = f"%{target_url.split('?')[0].rstrip('/')}%"
            row = conn.execute("""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
                  AND (apply_status IS NULL OR apply_status != 'in_progress')
                LIMIT 1
            """, (target_url, target_url, like, like)).fetchone()
        else:
            blocked_sites, blocked_patterns = _load_blocked()
            # Build parameterized filters to avoid SQL injection
            params: list = []
            site_clause = ""
            if blocked_sites:
                placeholders = ",".join("?" * len(blocked_sites))
                site_clause = f"AND site NOT IN ({placeholders})"
                params.extend(blocked_sites)
            url_clauses = ""
            if blocked_patterns:
                url_clauses = " ".join(f"AND COALESCE(application_url, url) NOT LIKE ?" for _ in blocked_patterns)
                params.extend(blocked_patterns)
            # Also block board-domain URLs at the SQL level so they're never picked up
            url_clauses += " AND COALESCE(application_url, url) NOT LIKE '%linkedin.com/jobs/%'"
            url_clauses += " AND COALESCE(application_url, url) NOT LIKE '%indeed.com%'"
            url_clauses += " AND COALESCE(application_url, url) NOT LIKE '%glassdoor.com%'"
            url_clauses += " AND COALESCE(application_url, url) NOT LIKE '%ziprecruiter.com%'"

            strategy_clause = ""
            if strategy:
                strategy_clause = "AND strategy = ? "
                params.append(strategy)

            row = conn.execute(f"""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path,
                       last_session_id
                FROM jobs
                WHERE (apply_status IS NULL OR apply_status = 'failed')
                  AND (apply_attempts IS NULL OR apply_attempts < ?)
                  AND (fit_score >= ? OR fit_score IS NULL)
                  {site_clause}
                  {url_clauses}
                  {strategy_clause}
                ORDER BY fit_score DESC NULLS LAST,
                  -- Round-robin by site: pick the site least recently attempted
                  (SELECT COALESCE(MAX(j2.last_attempted_at), '1970-01-01')
                   FROM jobs j2 WHERE j2.site = jobs.site) ASC,
                  RANDOM()
                LIMIT 1
            """, [config.DEFAULTS["max_apply_attempts"], min_score] + params).fetchone()

        if not row:
            conn.rollback()
            return None

        # Skip manual ATS sites (unsolvable CAPTCHAs)
        from applypilot.config import is_manual_ats
        apply_url = row["application_url"] or row["url"]
        if is_manual_ats(apply_url):
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                (row["url"],),
            )
            conn.commit()
            logger.info("Skipping manual ATS: %s", row["url"][:80])
            return None

        # Safety net: skip if the effective apply URL is on a blocked board domain
        # This catches edge cases where site derivation didn't redirect away
        _BLOCKED_APPLY_DOMAINS = {"indeed.com", "linkedin.com", "ziprecruiter.com", "glassdoor.com"}
        if apply_url:
            from urllib.parse import urlparse
            parsed = urlparse(apply_url)
            apply_domain = parsed.netloc.lower().removeprefix("www.")
            for blocked_domain in _BLOCKED_APPLY_DOMAINS:
                if apply_domain == blocked_domain or apply_domain.endswith("." + blocked_domain):
                    conn.execute(
                        "UPDATE jobs SET apply_status = 'manual', apply_error = 'blocked_board_domain' WHERE url = ?",
                        (row["url"],),
                    )
                    conn.commit()
                    logger.info("Skipping blocked board domain: %s", apply_url[:80])
                    return None

        # Skip jobs where the default resume PDF doesn't exist (can't apply without a resume)
        default_resume = config.RESUME_PDF_PATH
        if not default_resume.exists():
            fallback = Path(os.path.expanduser("~/Code/JobBot_Zip/JoshWard_Resume.pdf"))
            if not fallback.exists():
                conn.execute(
                    "UPDATE jobs SET apply_status = 'failed', apply_error = 'no_resume_pdf' WHERE url = ?",
                    (row["url"],),
                )
                conn.commit()
                logger.warning("No resume PDF found at %s or fallback", default_resume)
                return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?
            WHERE url = ?
        """, (f"worker-{worker_id}", now, row["url"]))
        conn.commit()

        return dict(row)
    except Exception:
        conn.rollback()
        raise


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (now, duration_ms, task_id, url))
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, task_id, url))
    conn.commit()


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 7,
               model: str = "sonnet", worker_id: int = 0) -> Path | None:
    """Generate a prompt file and print the Claude CLI command for manual debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id,
                      strategy=None)
    if not job:
        return None

    # Read resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text)

    # Release the lock so the job stays available
    release_lock(job["url"])

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{job['title'][:30].replace(' ', '_')}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = 9515 + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL
            WHERE url = ?
        """, (now, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL
            WHERE url = ?
        """, (reason or "manual", url))
    conn.commit()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status != 'applied'
              AND apply_status != 'in_progress')
    """)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def _kill_orphan_apply_agents() -> None:
    """Kill any Hermes agents from previous run_apply instances.

    Uses --pass-session-id as the marker — this flag is only present in
    Hermes agents launched by applypilot, never in user-initiated sessions.
    """
    try:
        _out = subprocess.check_output(
            ["pgrep", "-f", "--", "--pass-session-id"],
            timeout=5, text=True, stderr=subprocess.DEVNULL,
        ).strip()
        if _out:
            for _pid in _out.split("\n"):
                _pid = _pid.strip()
                if _pid and _pid.isdigit():
                    try:
                        os.kill(int(_pid), signal.SIGTERM)
                    except (OSError, ValueError):
                        pass
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError,
            FileNotFoundError):
        pass


def _save_session_id(url: str, session_id: str | None) -> None:
    """Persist the last Hermes session ID for a job so retries can pull history."""
    if not session_id:
        return
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE jobs SET last_session_id = ? WHERE url = ?",
            (session_id, url),
        )
        conn.commit()
    except Exception:
        pass  # best-effort save


def _get_recent_history(session_id: str, worker_id: int = 0,
                        max_turns: int = 5) -> str:
    """Read last N user+assistant turns from a Hermes session.

    Only user and assistant roles are included (tool messages — browser
    DOM dumps — are excluded as noise). Each message is capped at 600
    chars to keep the block compact (~3-5K tokens total).
    """
    state_db = os.path.join(str(config.APP_DIR), f"hermes-home-{worker_id}", "state.db")
    if not os.path.exists(state_db):
        return ""
    try:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(state_db)
        rows = conn.execute("""
            SELECT role, content FROM messages
            WHERE session_id = ?
              AND role IN ('user', 'assistant')
            ORDER BY id DESC
            LIMIT ?
        """, (session_id, max_turns * 2)).fetchall()
        conn.close()
    except Exception:
        return ""

    if not rows:
        return ""

    rows.reverse()
    lines = ["--- PRIOR SESSION (recent turns) ---"]
    for role, content in rows:
        label = "User" if role == "user" else "Assistant"
        excerpt = content[:600]
        lines.append(f"{label}: {excerpt}")
    lines.append("--- END PRIOR SESSION ---")
    return "\n\n".join(lines)


def run_job(job: dict, port: int, worker_id: int = 0,
            provider_chain: list | None = None,
            dry_run: bool = False) -> tuple[str, int]:
    """Try providers in priority order until one succeeds or all fail.

    provider_chain: list of (provider_name, model_name) tuples, highest priority first.
        Each provider is tried; if it errors (rate-limit, 401, 5xx, timeout, etc.)
        the next provider is attempted. After success, higher-priority providers
        are probed for the next job.

    Returns:
        Tuple of (status_string, duration_ms). Status is one of:
        'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', 'failed:all_providers_exhausted', or 'skipped'.
    """
    if not provider_chain:
        provider_chain = [("", "")]

    # Pre-flight: wait until at least one provider is available.
    # Don't skip the job — loop until the prober finds something.
    # NOTE: Do NOT filter or reassign provider_chain here — chain_len must
    # stay intact so _build_provider_cmd sets api_max_retries correctly (3
    # for multi-provider chains, not 999999 which would cause infinite retry).
    while True:
        any_available = False
        for prov, mod in provider_chain:
            if not prov:
                any_available = True
                break
            if prov == "openrouter":
                if _probe_provider(prov, mod):
                    any_available = True
                    break
            else:
                any_available = True
                break
        if any_available:
            break
        # All providers down — wait and retry
        if worker_id == 0:
            add_event(f"[W{worker_id}] All providers unavailable — waiting 30s...")
        import time as _time
        _time.sleep(30)

    # Read resume text — use default honest resume
    resume_path = job.get("tailored_resume_path")
    if resume_path:
        txt_path = Path(resume_path).with_suffix(".txt")
        resume_text = txt_path.read_text(encoding="utf-8") if txt_path.exists() else ""
    else:
        # Fall back to default resume text
        default_txt = config.RESUME_PDF_PATH.with_suffix(".txt")
        resume_text = default_txt.read_text(encoding="utf-8") if default_txt.exists() else ""

    # Build the prompt — append continuation history if this is a retry
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
    )
    # Re-add continuation context from prior session (last 3 turns only)
    # This lets the model pick up where it left off without restarting.
    last_session_id = job.get("last_session_id")
    if last_session_id:
        history = _get_recent_history(last_session_id, worker_id, max_turns=3)
        if history:
            agent_prompt += (
                "\n\n== CONTINUATION (prior session lost) ==\n"
                "A prior session for this job reached the context limit and was "
                "interrupted. Stay on the current page. Do NOT navigate away or "
                "re-upload the resume. Read the page to see what's already filled "
                "and continue where the prior session left off.\n\n"
            )
            agent_prompt += history + "\n"

    # Write per-worker MCP config
    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_config_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    hermes_path = os.path.expanduser("~/Code/hermes/fully_automatic_holographic")
    if not os.path.exists(hermes_path):
        return "failed:hermes_not_found", 0

    worker_dir = reset_worker_dir(worker_id)

    update_state(worker_id, status="applying", job_title=job['title'],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    overall_start = time.time()
    chain_len = len(provider_chain)
    last_provider = ""
    last_model = ""
    chain_idx = 0
    session_id: str | None = None  # captured from output for session resume
    _last_error_time: float = 0.0  # cooldown to prevent prober bounce
    while chain_idx < chain_len:
        provider, model = provider_chain[chain_idx]
        # Use the background prober's latest result to decide which provider to use.
        # The prober tests the highest-priority provider every 30s.
        best = _get_best_available()
        if best is False and chain_idx == 0 and chain_len > 1:
            # Prober says highest-priority provider is down — skip it
            add_event(f"[W{worker_id}] {provider}/{model} unavailable (probed) — skipping")
            chain_idx += 1
            continue
        elif best and best is not False and chain_idx > 0:
            # Prober says a higher-priority provider recovered — only switch back
            # if enough time has passed since the last error (prevent infinite bounce)
            _cooldown_remaining = 60 - (time.time() - _last_error_time)
            if _cooldown_remaining <= 0:
                provider, model = best
                chain_idx = 0
                add_event(f"[W{worker_id}] Prober detected recovery — switching to {provider}/{model}")
            else:
                add_event(f"[W{worker_id}] Prober detected recovery — waiting {_cooldown_remaining:.0f}s cooldown")

        last_provider = provider
        last_model = model
        label = f"{provider}/{model}" if provider else "default"
        attempt_label = f" (attempt {chain_idx + 1}/{chain_len})" if chain_len > 1 else ""

        add_event(f"[W{worker_id}] Using {label}{attempt_label}")
        start = time.time()
        proc = None

        cmd, env = _build_provider_cmd(hermes_path, provider, model, agent_prompt, worker_id=worker_id, chain_len=chain_len)
        # Only resume sessions for multi-provider chains (provider switching).
        # For single-provider (local model), start fresh every retry so context
        # doesn't bloat with stale history from dead-end sessions.
        if session_id and chain_len > 1:
            cmd += ["--resume", session_id]
        env["LLM_PROVIDER"] = provider

        try:
            # Kill any orphaned Hermes agents from previous run_apply.py instances.
            # They have --pass-session-id in their args (normal Hermes doesn't).
            _kill_orphan_apply_agents()
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=str(worker_dir),
                start_new_session=True,
            )
            with _claude_lock:
                _claude_procs[worker_id] = proc

            # ── Start mid-job prober ────────────────────────────────────────
            # Checks higher-priority providers every 30s; writes switch file
            # so Hermes can seamlessly swap provider/model mid-conversation.
            _hermes_home = env.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
            _switch_file = os.path.join(_hermes_home, "apply-provider-switch.json")
            _prober = Thread(
                target=_mid_job_switch_prober,
                args=(proc, provider_chain, chain_idx, _switch_file, worker_id),
                daemon=True,
            )
            _prober.start()

            stdout_lines: list[str] = []
            with open(worker_log, "a", encoding="utf-8") as lf:
                lf.write(log_header)

                def _reader():
                    for raw_line in proc.stdout:
                        line = raw_line.strip()
                        if not line:
                            continue
                        stdout_lines.append(line)
                        lf.write(line + "\n")

                reader_thread = Thread(target=_reader, daemon=True)
                reader_thread.start()

                proc.wait(timeout=None)
                reader_thread.join(timeout=5)

            returncode = proc.returncode
            proc = None

            output = "\n".join(stdout_lines)
            elapsed = int(time.time() - start)
            duration_ms = int((time.time() - overall_start) * 1000)

            # Capture session ID from output for potential resume on retry
            if not session_id:
                for _line in stdout_lines:
                    _m = __import__('re').search(r'session=(\S+)', _line)
                    if _m:
                        session_id = _m.group(1)
                        break

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            job_log = config.LOG_DIR / f"claude_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
            job_log.write_text(output, encoding="utf-8")

            def _clean_reason(s: str) -> str:
                return re.sub(r'[*`\\\"]+$', '', s).strip()

            # Scan output lines in REVERSE for agent's final RESULT
            output_lines = output.split("\n")
            result_line = None
            for i in range(len(output_lines) - 1, -1, -1):
                line = output_lines[i].strip()
                if "RESULT:APPLIED" in line:
                    result_line = ("applied", "applied")
                    break
                elif "RESULT:FAILED" in line:
                    reason = (
                        line.split("RESULT:FAILED:")[-1].strip()
                        if ":FAILED:" in line
                        else "unknown"
                    )
                    reason = _clean_reason(reason)
                    result_line = (f"failed:{reason}", reason)
                    break
                elif "RESULT:EXPIRED" in line:
                    result_line = ("expired", "expired")
                    break
                elif "RESULT:CAPTCHA" in line:
                    result_line = ("captcha", "captcha")
                    break
                elif "RESULT:LOGIN_ISSUE" in line:
                    result_line = ("login_issue", "login_issue")
                    break

            if result_line:
                status_key, display_status = result_line

                # URL verification for APPLIED — reject if no confirmation URL follows
                if status_key == "applied":
                    _url_line = output_lines[i + 1].strip() if i + 1 < len(output_lines) else ""
                    if not _url_line.startswith("http"):
                        add_event(f"[W{worker_id}] APPLIED claimed without confirmation URL — rejecting")
                        status_key = "failed:no_confirmation_url"
                        display_status = "no_confirmation_url"
                        result_line = (status_key, display_status)

                # Check if the result is actually a provider error disguised as RESULT:FAILED.
                # Only override when the agent gave a generic reason (not a specific eligibility
                # decision like not_eligible_location / not_eligible_role / not_eligible_work_auth).
                # The post-processing phase (title gen, fact extraction) may produce auxiliary
                # error messages (e.g. OpenRouter 401 fallback) that are NOT job failures.
                _AGENT_DECISIONS = {"not_eligible_location", "not_eligible_role",
                                    "not_eligible_work_auth", "not_eligible_salary",
                                    "already_applied", "not_a_job_application",
                                    "sso_required", "account_required",
                                    "page_error", "site_blocked"}
                _reason = status_key.split(":", 1)[-1] if ":" in status_key else ""
                # Single-provider (local model): always respect the agent's
                # decision.  There's no fallback to switch to, and tool-level
                # errors like Playwright timeouts contain strings ("timeout")
                # that trigger detect_provider_error.  Skip the override.
                if (chain_len > 1
                    and status_key.startswith("failed:")
                    and _reason not in _AGENT_DECISIONS
                    and detect_provider_error(output)):
                    _last_error_time = time.time()
                    if chain_idx < chain_len - 1:
                        add_event(f"[W{worker_id}] {label} provider error (fallback #{chain_idx + 2})")
                        chain_idx += 1
                        continue  # try next provider
                    elif chain_len <= 1:
                        # Single-provider: never give up — keep retrying forever
                        _last_error_time = time.time()
                        add_event(f"[W{worker_id}] {label} provider error — retrying same provider in 10s")
                        chain_idx = 0
                        time.sleep(10)
                        continue
                    # All providers exhausted — wrap around to first and retry
                    chain_idx = 0
                    add_event(f"[W{worker_id}] All providers exhausted — wrapping to first in 10s")
                    update_state(worker_id, last_action="all providers exhausted, retrying (10s)")
                    time.sleep(10)
                    continue

                if status_key == "applied":
                    add_event(f"[W{worker_id}] APPLIED via {label} ({elapsed}s): {job['title'][:30]}")
                    # ── Save the confirmation page in the DB ───────────────────────
                    _port = 9515 + worker_id
                    try:
                        import urllib.request, json as _j
                        _targets = _j.loads(urllib.request.urlopen(
                            f"http://127.0.0.1:{_port}/json", timeout=5).read())
                        _ws = None
                        for _t in _targets:
                            if _t.get("type") == "page":
                                _ws = _t.get("webSocketDebuggerUrl")
                                break
                        if _ws:
                            import websocket
                            _wsc = websocket.create_connection(_ws, timeout=10)
                            _cmd = _j.dumps({"id":1,"method":"Runtime.evaluate",
                                "params":{"expression":
                                    "document.body.innerText.substring(0,20000)"}})
                            _wsc.send(_cmd)
                            _resp = _j.loads(_wsc.recv())
                            _wsc.close()
                            _page_text = (_resp.get("result",{}).get("result",{})
                                          .get("value",""))
                            from applypilot.database import get_connection
                            _conn = get_connection()
                            _conn.execute("ALTER TABLE jobs ADD COLUMN confirmation_page TEXT")
                            _conn.execute("UPDATE jobs SET confirmation_page = ? WHERE url = ?",
                                          (_page_text, job["url"]))
                            _conn.commit()
                    except Exception:
                        pass  # Non-critical — best-effort capture

                    update_state(worker_id, status="applied",
                                 last_action=f"APPLIED via {label} ({elapsed}s)")
                    _capture_fields_async(worker_id, session_id or "")
                    _save_session_id(job["url"], session_id)
                    return "applied", duration_ms

                PROMOTE_TO_STATUS = {"captcha", "expired", "login_issue"}
                if display_status in PROMOTE_TO_STATUS:
                    add_event(f"[W{worker_id}] {display_status.upper()} ({elapsed}s): {job['title'][:30]}")
                    update_state(worker_id, status=display_status,
                                 last_action=f"{display_status.upper()} ({elapsed}s)")
                    _save_session_id(job["url"], session_id)
                    return display_status, duration_ms

                add_event(f"[W{worker_id}] FAILED ({elapsed}s): {display_status[:30]}")
                update_state(worker_id, status="failed",
                             last_action=f"FAILED: {display_status[:25]}")
                _save_session_id(job["url"], session_id)
                return status_key, duration_ms

            # No RESULT line found — check for provider errors
            # Single-provider (local): skip provider error detection.
            # Tool-level errors (Playwright timeouts) contain strings like
            # "timeout" that would falsely trigger it.  Just fail cleanly.
            if chain_len > 1 and detect_provider_error(output):
                _last_error_time = time.time()
                if chain_idx < chain_len - 1:
                    add_event(f"[W{worker_id}] {label} no result (provider error, fallback #{chain_idx + 2})")
                    chain_idx += 1
                    continue
                elif chain_len <= 1:
                    # Single-provider: never give up — keep retrying forever
                    _last_error_time = time.time()
                    add_event(f"[W{worker_id}] {label} no result (provider error) — retrying same provider in 10s")
                    chain_idx = 0
                    time.sleep(10)
                    continue
                # All providers exhausted — wrap around to first and retry
                chain_idx = 0
                add_event(f"[W{worker_id}] All providers exhausted — wrapping to first in 10s")
                update_state(worker_id, last_action="all providers exhausted, retrying (10s)")
                time.sleep(10)
                continue

            add_event(f"[W{worker_id}] NO RESULT ({elapsed}s)")
            _save_session_id(job["url"], session_id)
            _capture_fields_async(worker_id, session_id or "")
            update_state(worker_id, status="failed", last_action=f"no result ({elapsed}s)")
            return "failed:no_result_line", duration_ms

        except subprocess.TimeoutExpired:
            elapsed = int(time.time() - start)
            _last_error_time = time.time()
            add_event(f"[W{worker_id}] {label} TIMEOUT ({elapsed}s)")
            update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
            if proc is not None:
                _kill_process_tree(proc.pid)
            if chain_idx < chain_len - 1:
                add_event(f"[W{worker_id}] Timeout may be provider issue — fallback #{chain_idx + 2}")
                chain_idx += 1
                continue
            elif chain_len <= 1:
                # Single-provider: never give up — keep retrying forever
                _last_error_time = time.time()
                add_event(f"[W{worker_id}] {label} TIMEOUT — retrying same provider in 10s")
                chain_idx = 0
                time.sleep(10)
                continue
            # All providers exhausted — wrap around to first and retry
            chain_idx = 0
            add_event(f"[W{worker_id}] All providers exhausted — wrapping to first in 10s")
            update_state(worker_id, last_action="all providers exhausted, retrying (10s)")
            time.sleep(10)
            continue

        except Exception as e:
            duration_ms = int((time.time() - overall_start) * 1000)
            _last_error_time = time.time()
            err_msg = str(e)[:100]
            add_event(f"[W{worker_id}] {label} ERROR: {err_msg[:40]}")
            update_state(worker_id, status="failed", last_action=f"ERROR: {err_msg[:25]}")
            if chain_idx < chain_len - 1:
                add_event(f"[W{worker_id}] Error may be transient — fallback #{chain_idx + 2}")
                chain_idx += 1
                continue
            # All providers exhausted — wrap around to first and retry
            chain_idx = 0
            add_event(f"[W{worker_id}] All providers exhausted — wrapping to first in 10s")
            update_state(worker_id, last_action="all providers exhausted, retrying (10s)")
            time.sleep(10)
            continue

        finally:
            with _claude_lock:
                _claude_procs.pop(worker_id, None)
            if proc is not None and proc.poll() is None:
                _kill_process_tree(proc.pid)

    # All providers exhausted
    return "failed:all_providers_exhausted", int((time.time() - overall_start) * 1000)


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired", "captcha", "login_issue",
    "not_eligible_location", "not_eligible_salary",
    "not_eligible_role",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "sso_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
    "unsupported_requirement",
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, headless: bool = False,
                model: str = "sonnet", provider: str = "",
                provider_chain: list | None = None,
                dry_run: bool = False,
                strategy: str | None = None) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Uses provider_chain (list of (provider, model) tuples, highest priority first)
    for automatic fallback. If provider_chain is None, falls back to the legacy
    single (provider, model).

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Model name for the apply agent (legacy, used if no chain).
        provider: LLM provider override (legacy, used if no chain).
        provider_chain: List of (provider, model) tuples for fallback.
        dry_run: Don't click Submit.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = 9515 + worker_id  # match run_apply.py's base port for Chrome started via start-chrome.sh

    # Build the effective provider chain
    if provider_chain:
        active_chain = list(provider_chain)
    elif provider:
        active_chain = [(provider, model or "")]
    else:
        active_chain = [("", "")]

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id, strategy=strategy)
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0

        chrome_proc = None
        try:
            # Use user's already-running Chrome on port 9515 instead of launching a new instance
            add_event(f"[W{worker_id}] Connecting to Chrome on port 9515...")

            result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                            provider_chain=active_chain,
                                            dry_run=dry_run)

            if result == "skipped":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                continue
            elif result == "applied":
                mark_result(job["url"], "applied", duration_ms=duration_ms)
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
                add_completed_job(
                    job['title'], job.get('site', ''),
                    job.get('fit_score'), 'applied',
                    f"{duration_ms // 60000}m{duration_ms // 1000 % 60}s" if duration_ms else "",
                )
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms)
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)
                add_completed_job(
                    job['title'], job.get('site', ''),
                    job.get('fit_score'), reason,
                    f"{duration_ms // 60000}m{duration_ms // 1000 % 60}s" if duration_ms else "",
                )

            # ── Probe higher-priority providers after each job ──────────────
            # If a higher-priority provider recovers, move it back to the front
            # of the chain for the next job.
            if len(active_chain) > 1 and result == "applied":
                for probe_idx in range(1, len(active_chain)):
                    probe_prov, probe_model = active_chain[probe_idx]
                    # Don't probe last-used since it just worked
                    if _probe_provider(probe_prov, probe_model):
                        add_event(f"[W{worker_id}] {probe_prov}/{probe_model} recovered — promoting to top priority")
                        # Move probed entry to front
                        entry = active_chain.pop(probe_idx)
                        active_chain.insert(0, entry)
                        break

        except KeyboardInterrupt:
            release_lock(job["url"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            release_lock(job["url"])
            failed += 1
            update_state(worker_id, jobs_failed=failed)
        finally:
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


def _run_multi_worker(workers, effective_limit, target_url,
                      min_score, headless, model, provider,
                      provider_chain, dry_run, strategy=None):
    """Run multiple worker loops in parallel using ThreadPoolExecutor."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    if effective_limit:
        base = effective_limit // workers
        extra = effective_limit % workers
        limits = [base + (1 if i < extra else 0)
                  for i in range(workers)]
    else:
        limits = [0] * workers  # continuous mode

    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="apply-worker") as executor:
        futures = {
            executor.submit(
                worker_loop,
                worker_id=i,
                limit=limits[i],
                target_url=target_url,
                min_score=min_score,
                headless=headless,
                model=model,
                provider=provider,
                provider_chain=provider_chain,
                dry_run=dry_run,
                strategy=strategy,
            ): i
            for i in range(workers)
        }

        results: list[tuple[int, int]] = []
        for future in as_completed(futures):
            wid = futures[future]
            try:
                results.append(future.result())
            except Exception:
                logger.exception("Worker %d crashed", wid)
                results.append((0, 0))

    return sum(r[0] for r in results), sum(r[1] for r in results)


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 7, headless: bool = False, model: str = "sonnet",
         provider: str = "",
         provider_chain: list | None = None,
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1,
         strategy: str | None = None) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Model name (legacy, used if no provider_chain).
        provider: LLM provider override (legacy, used if no provider_chain).
        provider_chain: List of (provider_name, model_name) tuples for fallback.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    config.ensure_dirs()
    _is_tty = sys.stdout.isatty()
    console = Console(stderr=True) if not _is_tty else Console()

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    if _is_tty:
        console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    # ── Start background provider prober ──────────────────────────────────────────
    # Polls the highest-priority provider every 30s so we know immediately when
    # it recovers and can switch back even mid-job.
    # Only needed for multi-provider chains — local/single-provider doesn't need probing.
    if provider_chain and len(provider_chain) > 1:
        _start_prober(provider_chain)
        add_event("Background provider prober started (every 30s)")

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Kill all active Claude processes to skip current jobs
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        if _is_tty:
            # Start the KV cache monitor (polls llama-server /slots if running)
            from applypilot.apply.dashboard import start_cache_monitor
            start_cache_monitor(port=int(os.environ.get("LLAMA_PORT", "11434")))

            # TTY: full dashboard with ANSI cursor-overwrite
            _dashboard_running = True
            _dash_output = ""  # last full output for height calculation

            def _render_str() -> str:
                from io import StringIO
                from rich.console import Console as RichConsole
                buf = StringIO()
                rc = RichConsole(file=buf, width=120, color_system=None)
                rc.print(render_full())
                return buf.getvalue()

            def _get_height(text: str) -> int:
                return text.count("\n") + 1

            # Initial print
            from applypilot.apply.dashboard import render_full
            out = _render_str()
            print(out, end="")
            _dash_output = out

            def _refresh():
                nonlocal _dash_output
                while _dashboard_running:
                    time.sleep(2)
                    out = _render_str()
                    if out == _dash_output:
                        continue
                    h = _get_height(_dash_output)
                    sys.stdout.write(f"\033[{h}A\033[J")
                    print(out, end="")
                    _dash_output = out

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    provider=provider,
                    provider_chain=provider_chain,
                    dry_run=dry_run,
                    strategy=strategy,
                )
            else:
                total_applied, total_failed = _run_multi_worker(
                    workers, effective_limit, target_url,
                    min_score, headless, model, provider,
                    provider_chain, dry_run,
                    strategy=strategy,
                )

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            sys.stdout.write(f"\033[{_get_height(_dash_output)}A\033[J")
            print(_render_str(), end="")
        else:
            # Non-TTY: fall back to simple console logging
            console.print("Running in non-TTY mode (dashboard disabled)...")
            if workers == 1:
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    provider=provider,
                    provider_chain=provider_chain,
                    dry_run=dry_run,
                    strategy=strategy,
                )
            else:
                total_applied, total_failed = _run_multi_worker(
                    workers, effective_limit, target_url,
                    min_score, headless, model, provider,
                    provider_chain, dry_run,
                    strategy=strategy,
                )

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
