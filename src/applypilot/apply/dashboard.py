"""Rich live dashboard for the apply pipeline.

Displays real-time worker status, job progress, recent events,
and llama-server KV prompt cache stats in a terminal dashboard
using the Rich library.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen, Request

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

logger = logging.getLogger(__name__)


@dataclass
class WorkerState:
    """Tracks the current state of the apply worker."""

    worker_id: int = 0
    status: str = "starting"  # starting, applying, applied, failed, expired, captcha, idle, done
    job_title: str = ""
    company: str = ""
    score: int = 0
    start_time: float = 0.0
    actions: int = 0
    last_action: str = ""
    jobs_applied: int = 0
    jobs_failed: int = 0
    jobs_done: int = 0
    total_cost: float = 0.0
    log_file: Path | None = None


@dataclass
class CompletedJob:
    """Record of a finished job for the history log."""
    title: str = ""
    company: str = ""
    score: int | None = None
    status: str = ""
    elapsed: str = ""


# Module-level state (thread-safe via _lock)
_worker_states: dict[int, WorkerState] = {}
_completed_jobs: list[CompletedJob] = []
_events: list[str] = []
_lock = threading.Lock()
MAX_EVENTS = 8
MAX_HISTORY = 15

# ── KV Cache Monitor ───────────────────────────────────────────────────────
# Polls llama-server /slots endpoint to display real-time prompt cache stats.
_DEFAULT_LLAMA_PORT = 11434
_cache_data: dict = {"slots": [], "error": None, "last_update": 0.0}
_cache_lock = threading.Lock()


def _poll_cache(port: int = _DEFAULT_LLAMA_PORT) -> None:
    """Background thread: polls llama-server /slots every 2s."""
    global _cache_data
    url = f"http://127.0.0.1:{port}/slots"
    while True:
        try:
            req = Request(url, method="GET")
            resp = urlopen(req, timeout=3)
            data = json.loads(resp.read().decode())
            total_tok = 0
            cached_tok = 0
            for slot in data:
                pt = slot.get("n_prompt_tokens", 0) or 0
                pc = slot.get("n_prompt_tokens_cache", 0) or 0
                total_tok += pt
                cached_tok += pc
            with _cache_lock:
                _cache_data = {
                    "slots": data,
                    "total_tokens": total_tok,
                    "cached_tokens": cached_tok,
                    "error": None,
                    "last_update": time.time(),
                }
        except Exception as e:
            with _cache_lock:
                _cache_data["error"] = str(e)[:60]
                _cache_data["last_update"] = time.time()
        time.sleep(2)


def start_cache_monitor(port: int = _DEFAULT_LLAMA_PORT) -> None:
    """Start the background cache poller daemon thread."""
    t = threading.Thread(target=_poll_cache, args=(port,), daemon=True)
    t.start()


def get_agent_output_panel(worker_id: int = 0, max_lines: int = 20) -> Panel | None:
    """Build a Rich Panel showing last N lines of Hermes agent output."""
    log_dir = os.path.join(str(Path.home()), ".applypilot", "logs")
    log_path = os.path.join(log_dir, f"worker-{worker_id}.log")
    if not os.path.exists(log_path):
        return None

    try:
        with open(log_path, "rb") as f:
            # Read last 64KB for line sampling
            f.seek(0, 2)
            size = f.tell()
            seek_back = min(size, 65536)
            f.seek(size - seek_back)
            raw = f.read(seek_back).decode("utf-8", errors="replace")
    except Exception:
        return None

    # Split into lines, take last N*4, filter
    all_lines = raw.split("\n")
    sample = all_lines[-max_lines * 4:]  # generous buffer

    kept = []
    for line in sample:
        stripped = line.strip()
        if not stripped:
            continue
        # Skip noisy output: huge JSON blobs, DOM dumps, full prompts
        if len(stripped) > 250:
            continue
        if stripped.startswith("{\"result\":") or stripped.startswith("{\"error\":"):
            continue
        if "You are an autonomous" in stripped or "== JOB ==" in stripped:
            continue
        if "RESUME TEXT" in stripped or "APPLICANT PROFILE" in stripped:
            continue
        if stripped.startswith("```") or stripped.startswith("│") or stripped.startswith("┊"):
            continue
        # Keep lines that look like agent output
        if any(s in stripped for s in ("🤖", "📞", "✅", "❌", "⏱️", "🔄", "💾", "🔧",
                                        "RESULT:", "API call", "Tool ", "Model:",
                                        "Session:", "Captured reasoning",
                                        "Starting", "APPLYING", "FAILED", "STOPPING",
                                        "Skipping", "Ctrl+C", "applied=")):
            kept.append(stripped[:120])
        elif kept and not stripped.startswith("━") and not stripped.startswith("╔") \
             and not stripped.startswith("┏") and not stripped.startswith("┃"):
            # Also keep contextual lines that follow a kept line (e.g., error details)
            kept.append(stripped[:120])

    # Take last max_lines
    display = kept[-max_lines:]
    if not display:
        return None

    text = "\n".join(display)
    return Panel(
        text,
        title="[bold]Hermes Output[/bold]",
        border_style="green",
        height=min(max_lines + 2, len(display) + 2),
    )


def get_cache_panel() -> Panel | None:
    """Build a Rich Panel showing llama-server KV cache stats.

    Returns None if llama-server isn't reachable (not running).
    """
    with _cache_lock:
        data = dict(_cache_data)

    if data.get("error") and not data.get("slots"):
        return None  # Server not reachable — skip panel

    slots = data.get("slots", [])
    if not slots:
        return None

    lines = []
    total_prompt = 0
    total_cache = 0
    for slot in slots:
        sid = slot.get("id", 0)
        n_ctx = slot.get("n_ctx", 0)
        pt = slot.get("n_prompt_tokens", 0) or 0
        pp = slot.get("n_prompt_tokens_processed", 0) or 0
        pc = slot.get("n_prompt_tokens_cache", 0) or 0
        busy = slot.get("is_processing", False)
        total_prompt += pt
        total_cache += pc

        status = "[yellow]▸[/]" if busy else "[dim]·[/]"
        if pt > 0:
            pct = pc * 100 // pt if pt else 0
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            lines.append(
                f"  {status} slot {sid}: {pp}/{pt} tok | cache {bar} {pct}%"
            )
        else:
            lines.append(f"  {status} slot {sid}: idle")

    # Aggregate cache info
    total_pct = total_cache * 100 // total_prompt if total_prompt else 0
    ctx_gb = slots[0].get("n_ctx", 0) * 4 / (1024**3) if slots else 0
    header = (
        f"[bold]KV Prompt Cache[/bold]  "
        f"(ctx: {ctx_gb:.1f}G tok | "
        f"hit: {total_cache}/{total_prompt} = {total_pct}%)"
    )

    text = "\n".join(lines)
    return Panel(
        text,
        title=header,
        border_style="blue",
        height=len(lines) + 2,
    )


# ---------------------------------------------------------------------------
# State mutation helpers
# ---------------------------------------------------------------------------

def init_worker(worker_id: int = 0) -> None:
    """Register the worker in the dashboard state."""
    with _lock:
        _worker_states[worker_id] = WorkerState(worker_id=worker_id)


def update_state(worker_id: int = 0, **kwargs) -> None:
    """Update the worker's state fields.

    Args:
        worker_id: Which worker to update.
        **kwargs: Field names and values to set on WorkerState.
    """
    with _lock:
        state = _worker_states.get(worker_id)
        if state is not None:
            for key, value in kwargs.items():
                setattr(state, key, value)


def get_state(worker_id: int = 0) -> WorkerState | None:
    """Read the worker's current state."""
    with _lock:
        return _worker_states.get(worker_id)


def add_event(msg: str) -> None:
    """Add a timestamped event to the scrolling event log.

    Args:
        msg: Rich markup string describing the event.
    """
    ts = datetime.now().strftime("%H:%M:%S")
    with _lock:
        _events.append(f"[dim]{ts}[/dim] {msg}")
        if len(_events) > MAX_EVENTS:
            _events.pop(0)


def add_completed_job(title: str, company: str, score: int | None, status: str, elapsed: str) -> None:
    """Record a finished job in the history log.

    Args:
        title: Job title.
        company: Company/site name.
        score: Fit score (may be None).
        status: Result status string.
        elapsed: Human-readable elapsed time.
    """
    with _lock:
        _completed_jobs.append(CompletedJob(
            title=title[:40], company=company[:16],
            score=score, status=status, elapsed=elapsed,
        ))
        if len(_completed_jobs) > MAX_HISTORY:
            _completed_jobs.pop(0)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

# Status -> Rich style mapping
_STATUS_STYLES: dict[str, str] = {
    "starting": "dim",
    "idle": "dim",
    "applying": "yellow",
    "applied": "bold green",
    "failed": "red",
    "expired": "dim red",
    "captcha": "magenta",
    "login_issue": "red",
    "done": "bold",
}


def render_dashboard() -> Table:
    """Build the Rich table showing worker status and completed jobs.

    Returns:
        A Rich Table object ready for display.
    """
    # Fetch progress stats
    try:
        from applypilot.config import load_blocked_sites
        from applypilot.database import get_connection
        conn = get_connection()
        blocked_sites, _ = load_blocked_sites()
        blocked = tuple(blocked_sites)
        ph = ','.join('?' * len(blocked))
        total_jobs = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE site NOT IN ({ph})", list(blocked)).fetchone()[0]
        resolved = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE site NOT IN ({ph}) AND (apply_status = 'applied' OR (apply_status = 'failed' AND apply_attempts >= 99))", list(blocked)).fetchone()[0]
        # Show the current in-progress job, not the next queued one
        current = conn.execute(f"SELECT title, site, fit_score FROM jobs WHERE apply_status = 'in_progress' AND site NOT IN ({ph}) ORDER BY ROWID LIMIT 1", list(blocked)).fetchone()
        if current:
            subtitle = f"{resolved}/{total_jobs} done | {current[0][:40]} @ {current[1]} (score={current[2] or '-'})"
        else:
            subtitle = f"{resolved}/{total_jobs} done"
    except Exception:
        subtitle = ""

    table = Table(title=f"ApplyPilot Dashboard  [{subtitle}]" if subtitle else "ApplyPilot Dashboard",
                  expand=True, show_lines=False)
    table.add_column("#", style="bold", width=3, justify="center")
    table.add_column("Job", min_width=22, max_width=35, no_wrap=True)
    table.add_column("S", width=3, justify="center")
    table.add_column("Status", width=10, justify="center")
    table.add_column("Time", width=6, justify="right")
    table.add_column("Acts", width=3, justify="right")
    table.add_column("Last Action", min_width=14, max_width=25, no_wrap=True)

    with _lock:
        states = sorted(_worker_states.values(), key=lambda s: s.worker_id)
        completed = list(_completed_jobs)

    row_idx = 0

    # Current worker state
    for s in states:
        row_idx += 1
        elapsed = ""
        if s.start_time and s.status == "applying":
            elapsed = f"{int(time.time() - s.start_time)}s"

        style = _STATUS_STYLES.get(s.status, "")
        status_text = Text(s.status.upper(), style=style)
        job_text = f"{s.job_title[:28]} @ {s.company[:16]}" if s.job_title else ""
        score_text = str(s.score) if s.score else ""

        # Try to read the latest action from the worker log
        action = s.last_action[:30] if s.last_action else ""
        if s.status in ("applying", "starting") and s.log_file and s.log_file.exists():
            try:
                lines = s.log_file.read_text(encoding="utf-8", errors="replace").strip().split("\n")
                # Find the most recent interesting line (tool call, action, etc.)
                for line in reversed(lines):
                    line = line.strip()
                    if any(kw in line.lower() for kw in ["tool call", "click", "fill", "submit", "navigate", "type", "select", "upload", "scroll", "wait", "choose", "check", "apply", "next", "continue"]):
                        action = line.strip()[:35]
                        break
            except Exception:
                pass

        table.add_row(
            str(s.worker_id) if s.status in ("applying", "starting", "idle") else "",
            job_text,
            score_text,
            status_text,
            elapsed,
            str(s.actions) if s.actions else "",
            action,
        )

    # Completed jobs history
    for cj in reversed(completed):
        row_idx += 1
        style = _STATUS_STYLES.get(cj.status, "dim")
        status_text = Text(cj.status.upper(), style=style)
        job_text = f"{cj.title[:28]} @ {cj.company[:16]}" if cj.title else ""
        score_text = str(cj.score) if cj.score else ""
        table.add_row("", job_text, score_text, status_text, cj.elapsed, "", "")

    # Totals
    total_applied = sum(s.jobs_applied for s in states)
    total_failed = sum(s.jobs_failed for s in states)
    table.add_section()
    table.add_row("", "", "", "", "", "", f"OK={total_applied} FAIL={total_failed}", style="bold")

    return table


def render_full() -> Table | Group:
    """Render the dashboard table plus the recent events and cache panels.

    Returns:
        A Rich Group (table + events panel + cache panel) or just the table.
    """
    table = render_dashboard()

    with _lock:
        event_lines = list(_events)

    panels = [table]

    if event_lines:
        event_text = Text.from_markup("\n".join(event_lines))
        events_panel = Panel(
            event_text,
            title="Recent Events",
            border_style="dim",
            height=min(MAX_EVENTS + 2, len(event_lines) + 2),
        )
        panels.append(events_panel)

    # Show KV cache panel if llama-server is reachable
    cache_panel = get_cache_panel()
    if cache_panel:
        panels.append(cache_panel)

    # Show agent output panel
    agent_panel = get_agent_output_panel()
    if agent_panel:
        panels.append(agent_panel)

    return Group(*panels) if len(panels) > 1 else table


def get_totals() -> dict[str, int | float]:
    """Compute aggregate totals across all workers.

    Returns:
        Dict with keys: applied, failed, cost.
    """
    with _lock:
        applied = sum(s.jobs_applied for s in _worker_states.values())
        failed = sum(s.jobs_failed for s in _worker_states.values())
        cost = sum(s.total_cost for s in _worker_states.values())
    return {"applied": applied, "failed": failed, "cost": cost}
