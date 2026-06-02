"""Rich live dashboard for the apply pipeline.

Displays real-time worker status, job progress, and recent events
in a terminal dashboard using the Rich library.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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
        # Show elapsed time as last action when job is running
        action = s.last_action[:30] if s.last_action else ""
        if s.status in ("applying", "starting") and s.start_time and (not action or action == "starting"):
            running = int(time.time() - s.start_time)
            action = f"running {running}s"

        table.add_row(
            str(s.worker_id) if s.status in ("applying", "starting") else "",
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
    """Render the dashboard table plus the recent events panel.

    Returns:
        A Rich Group (table + events panel) or just the table if no events.
    """
    table = render_dashboard()

    with _lock:
        event_lines = list(_events)

    if event_lines:
        event_text = Text.from_markup("\n".join(event_lines))
        events_panel = Panel(
            event_text,
            title="Recent Events",
            border_style="dim",
            height=min(MAX_EVENTS + 2, len(event_lines) + 2),
        )
        return Group(table, events_panel)

    return table


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
