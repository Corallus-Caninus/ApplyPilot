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
                        agent_prompt: str) -> tuple[list[str], dict]:
    """Build Hermes CLI command + environment for a specific provider/model."""
    cmd = [hermes_path, "chat", "-v"]
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    if provider:
        env["LLM_PROVIDER"] = provider

    if provider == "opencode" or provider == "opencode-zen":
        cmd += ["-m", model or "deepseek-v4-flash-free"]
        env["HERMES_MODE"] = "zen"
        env["REASONING_EFFORT"] = "low"
    elif provider == "opencode-go":
        cmd += ["-m", model or "deepseek-v4-flash"]
        env["HERMES_MODE"] = "go"
        env["REASONING_EFFORT"] = "low"
    elif provider == "openrouter":
        cmd += ["--provider", "openrouter"]
        cmd += ["-m", model or "openrouter/owl-alpha"]
        env["REASONING_EFFORT"] = "low"
    else:
        if provider:
            cmd += ["--provider", provider]
        if model:
            cmd += ["-m", model]
        env["REASONING_EFFORT"] = "low"

    cmd += ["-q", agent_prompt]
    if model:
        env["LLM_MODEL"] = model

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
        elif provider in ("opencode", "opencode-zen", "opencode-go"):
            # OpenCode — hit the models endpoint with the API key
            key_path = os.path.expanduser("~/Code/hermes/opencode-go-key")
            if os.path.isfile(key_path):
                key = open(key_path).read().strip()
                base = "https://opencode.ai/zen/go/v1" if provider == "opencode-go" else "https://opencode.ai/zen/v1"
                req = urllib.request.Request(
                    f"{base}/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
                resp = urllib.request.urlopen(req, timeout=10)
                return resp.status == 200
            return False
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
                worker_id: int = 0) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).

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
                  AND apply_status != 'in_progress'
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
                url_clauses = " ".join(f"AND url NOT LIKE ?" for _ in blocked_patterns)
                params.extend(blocked_patterns)
            row = conn.execute(f"""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE (apply_status IS NULL OR apply_status = 'failed')
                  AND (apply_attempts IS NULL OR apply_attempts < ?)
                  AND (fit_score >= ? OR fit_score IS NULL)
                  {site_clause}
                  {url_clauses}
                ORDER BY fit_score DESC, RANDOM()
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
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)
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
    port = BASE_CDP_PORT + worker_id
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

    # Read tailored resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    # Build the prompt
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
    )

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

    for chain_idx, (provider, model) in enumerate(provider_chain):
        # Use the background prober's latest result to decide which provider to use.
        # The prober tests the highest-priority provider every 30s.
        best = _get_best_available()
        if best is False and chain_idx == 0 and chain_len > 1:
            # Prober says highest-priority provider is down — skip it
            add_event(f"[W{worker_id}] {provider}/{model} unavailable (probed) — skipping")
            continue
        elif best and best is not False and chain_idx > 0:
            # Prober says a higher-priority provider recovered — jump to it
            provider, model = best
            chain_idx = 0
            add_event(f"[W{worker_id}] Prober detected recovery — switching to {provider}/{model}")

        last_provider = provider
        last_model = model
        label = f"{provider}/{model}" if provider else "default"
        attempt_label = f" (attempt {chain_idx + 1}/{chain_len})" if chain_len > 1 else ""

        add_event(f"[W{worker_id}] Using {label}{attempt_label}")
        start = time.time()
        proc = None

        cmd, env = _build_provider_cmd(hermes_path, provider, model, agent_prompt)
        env["LLM_PROVIDER"] = provider

        try:
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
            _switch_file = os.path.expanduser("~/.hermes/apply-provider-switch.json")
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

                proc.wait(timeout=900)
                reader_thread.join(timeout=5)

            returncode = proc.returncode
            proc = None

            if returncode and returncode < 0:
                return "skipped", int((time.time() - overall_start) * 1000)

            output = "\n".join(stdout_lines)
            elapsed = int(time.time() - start)
            duration_ms = int((time.time() - overall_start) * 1000)

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

                # Check if the result is actually a provider error disguised as RESULT:FAILED
                if status_key.startswith("failed:") and detect_provider_error(output):
                    if chain_idx < chain_len - 1:
                        add_event(f"[W{worker_id}] {label} provider error (fallback #{chain_idx + 2})")
                        continue  # try next provider
                    # Last provider — return the original result
                    add_event(f"[W{worker_id}] {display_status.upper()} ({elapsed}s): {job['title'][:30]}")
                    update_state(worker_id, status="failed",
                                 last_action=f"{display_status.upper()} ({elapsed}s)")
                    return status_key, duration_ms

                if status_key == "applied":
                    add_event(f"[W{worker_id}] APPLIED via {label} ({elapsed}s): {job['title'][:30]}")
                    update_state(worker_id, status="applied",
                                 last_action=f"APPLIED via {label} ({elapsed}s)")
                    return "applied", duration_ms

                PROMOTE_TO_STATUS = {"captcha", "expired", "login_issue"}
                if display_status in PROMOTE_TO_STATUS:
                    add_event(f"[W{worker_id}] {display_status.upper()} ({elapsed}s): {job['title'][:30]}")
                    update_state(worker_id, status=display_status,
                                 last_action=f"{display_status.upper()} ({elapsed}s)")
                    return display_status, duration_ms

                add_event(f"[W{worker_id}] FAILED ({elapsed}s): {display_status[:30]}")
                update_state(worker_id, status="failed",
                             last_action=f"FAILED: {display_status[:25]}")
                return status_key, duration_ms

            # No RESULT line found — check for provider errors
            if detect_provider_error(output):
                if chain_idx < chain_len - 1:
                    add_event(f"[W{worker_id}] {label} no result (provider error, fallback #{chain_idx + 2})")
                    continue
                add_event(f"[W{worker_id}] {label} no result (last provider)")
                update_state(worker_id, status="failed",
                             last_action=f"no result ({elapsed}s)")
                return "failed:provider_error", duration_ms

            add_event(f"[W{worker_id}] NO RESULT ({elapsed}s)")
            update_state(worker_id, status="failed", last_action=f"no result ({elapsed}s)")
            return "failed:no_result_line", duration_ms

        except subprocess.TimeoutExpired:
            elapsed = int(time.time() - start)
            add_event(f"[W{worker_id}] {label} TIMEOUT ({elapsed}s)")
            update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
            if proc is not None:
                _kill_process_tree(proc.pid)
            if chain_idx < chain_len - 1:
                add_event(f"[W{worker_id}] Timeout may be provider issue — fallback #{chain_idx + 2}")
                continue
            return "failed:timeout", int((time.time() - overall_start) * 1000)

        except Exception as e:
            duration_ms = int((time.time() - overall_start) * 1000)
            err_msg = str(e)[:100]
            add_event(f"[W{worker_id}] {label} ERROR: {err_msg[:40]}")
            update_state(worker_id, status="failed", last_action=f"ERROR: {err_msg[:25]}")
            if chain_idx < chain_len - 1:
                add_event(f"[W{worker_id}] Error may be transient — fallback #{chain_idx + 2}")
                continue
            return f"failed:{err_msg}", duration_ms

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
                dry_run: bool = False) -> tuple[int, int]:
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
    port = BASE_CDP_PORT + worker_id

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
                          worker_id=worker_id)
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


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 7, headless: bool = False, model: str = "sonnet",
         provider: str = "",
         provider_chain: list | None = None,
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1) -> None:
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
    console = Console()

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
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    # ── Start background provider prober ──────────────────────────────────────────
    # Polls the highest-priority provider every 30s so we know immediately when
    # it recovers and can switch back even mid-job.
    if provider_chain:
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
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                # Single worker — run directly in main thread
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
                )
            else:
                # Multi-worker — distribute limit across workers
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

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

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
