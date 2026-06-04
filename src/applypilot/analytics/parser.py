"""Parse claude_*.txt job logs into the analytics database.

Extracts per-job, per-API-call, and per-tool-call telemetry from the raw
Hermes conversation transcripts. Idempotent — skips logs already parsed.
"""

import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from applypilot.analytics.schema import DB_PATH, init_db

logger = logging.getLogger(__name__)

# ── ATS detection from job URLs ──────────────────────────────────────────────

ATS_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Workday",       re.compile(r"\.myworkdayjobs\.|\.wd\d?\.myworkday|myworkdayjobs\.|workday\.com")),
    ("Greenhouse",    re.compile(r"boards\.greenhouse\.|greenhouse\.io")),
    ("Lever",         re.compile(r"jobs\.lever\.|lever\.co")),
    ("Taleo",         re.compile(r"taleo\.net|oracle\.com.*taleo|cloud\.oracle.*recruitment")),
    ("Ashby",         re.compile(r"jobs\.ashbyhq|ashbyhq\.com")),
    ("BambooHR",      re.compile(r"bamboohr\.com/jobs")),
    ("Breezy",        re.compile(r"breezy\.hr")),
    ("Pinpoint",      re.compile(r"pinpointhq\.com")),
    ("SmartRecruiters", re.compile(r"smartrecruiters\.com")),
    ("iCIMS",         re.compile(r"icims\.com")),
    ("Jobvite",       re.compile(r"jobvite\.com")),
    ("LinkedIn",      re.compile(r"linkedin\.com/jobs")),
    ("Indeed",        re.compile(r"indeed\.com")),
    ("Dayforce",      re.compile(r"dayforcehcm|dayforce\.com")),
    ("SAP SuccessFactors", re.compile(r"successfactors\.com|sap\.com/careers")),
    ("Paycom",        re.compile(r"paycomonline\.com")),
]


def detect_ats(url: str | None) -> str | None:
    """Detect ATS platform from a job URL."""
    if not url:
        return None
    url_lower = url.lower()
    for name, pattern in ATS_PATTERNS:
        if pattern.search(url_lower):
            return name
    return None


# ── Regex patterns for log parsing ───────────────────────────────────────────

RE_TIMESTAMP = re.compile(r"^(\d{2}:\d{2}:\d{2})", re.MULTILINE)

# OpenAI client creation reveals provider/model/base_url
RE_CLIENT_CREATED = re.compile(
    r"OpenAI client created.*?provider=(\S+)\s+base_url=(\S+)\s+model=(\S+)"
)

# API call summary: API call #N: model=X provider=Y in=Z out=W total=V latency=Us
# Also handles cache info: cache=hit/miss (P%)
RE_API_CALL = re.compile(
    r"API call #(\d+):\s+model=(\S+)\s+provider=(\S+)"
    r"\s+in=(\d+)\s+out=(\d+)\s+total=(\d+)\s+latency=([\d.]+)s"
    r"(?:\s+cache=(\d+)/\d+\s*\((\d+)%\))?"
)

# Tool call start: "Tool call: name with args: {...}"
RE_TOOL_CALL = re.compile(
    r"Tool call:\s+(\S+)\s+with args:\s+(\{.+?\})"
)

# Tool completion (success): "- tool name completed (0.05s, 214 chars)"
RE_TOOL_DONE = re.compile(
    r"- tool\s+(\S+)\s+completed\s+\(([\d.]+)s,\s*(\d+)\s+chars\)"
)

# Tool error (WARNING): "- Tool name returned error (0.00s): ..."
RE_TOOL_ERROR = re.compile(
    r"- Tool\s+(\S+)\s+returned error\s+\(([\d.]+)s\)"
)

# Tool result error content
RE_TOOL_RESULT_ERROR = re.compile(
    r'"error":\s*"(.+?)"'
)

# Result line (agent's final output)
RE_RESULT = re.compile(
    r"^(?:🤖\s*Assistant:\s*)?RESULT:(APPLIED|FAILED|EXPIRED|CAPTCHA|LOGIN_ISSUE)"
    r"(?::(.+))?",
    re.MULTILINE,
)

# Session id
RE_SESSION = re.compile(r"session=(\S+)")

# URL from job header
RE_JOB_URL = re.compile(r"^URL:\s*(\S+)", re.MULTILINE)
RE_JOB_TITLE = re.compile(r"^Title:\s*(.+)", re.MULTILINE)
RE_JOB_COMPANY = re.compile(r"^Company:\s*(.+)", re.MULTILINE)


# ── Tool error classification ───────────────────────────────────────────────

ERROR_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("parameter_error", re.compile(r"Invalid input.*expected string|Unsupported token|expected 'target'|expected '.*?'")),
    ("css_selector",    re.compile(r"while parsing css selector|strict mode violation|multiple elements")),
    ("timeout",         re.compile(r"timeout|timed out|Timeout")),
    ("navigation",      re.compile(r"navigation|page\.goto|net::ERR_")),
    ("runtime",         re.compile(r"RuntimeError|TypeError|ValueError|AttributeError")),
    ("permission",      re.compile(r"permission|denied|blocked")),
    ("network",         re.compile(r"ETIMEDOUT|ECONNREFUSED|ECONNRESET|socket hang up")),
    ("auth",            re.compile(r"401|403|unauthorized|forbidden")),
    ("rate_limit",      re.compile(r"429|rate limit|too many requests")),
]


def classify_error(error_text: str) -> str:
    """Classify an error string into a category."""
    if not error_text:
        return "unknown"
    for category, pattern in ERROR_PATTERNS:
        if pattern.search(error_text):
            return category
    return "other"


# ── Parser ───────────────────────────────────────────────────────────────────

def parse_log_file(log_path: Path) -> dict | None:
    """Parse a single claude_*.txt log file into structured data.

    Returns a dict with keys for job_attempts, api_calls[], tool_calls[],
    or None if the file is unparseable (e.g. crashed on startup).
    """
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("Cannot read %s: %s", log_path, e)
        return None

    if not text.strip():
        return None

    # ── Job metadata from prompt header ──
    url_m = RE_JOB_URL.search(text)
    title_m = RE_JOB_TITLE.search(text)
    company_m = RE_JOB_COMPANY.search(text)

    job_url = url_m.group(1) if url_m else None
    job_title = title_m.group(1).strip() if title_m else None
    company = company_m.group(1).strip() if company_m else None
    ats = detect_ats(job_url)

    # ── Session ID ──
    session_m = RE_SESSION.search(text)
    session_id = session_m.group(1) if session_m else None

    # ── Provider / Model (from first client creation) ──
    provider = None
    model = None
    base_url = None
    for m in RE_CLIENT_CREATED.finditer(text):
        provider = m.group(1)
        base_url = m.group(2)
        model = m.group(3)
        break  # first one is the primary

    # ── Attempt date from filename ──
    fname = log_path.name  # claude_20260603_174105_w0_NVIDIA.txt
    date_m = re.match(r"claude_(\d{8})_", fname)
    attempt_date = date_m.group(1) if date_m else None
    if attempt_date:
        attempt_date = f"{attempt_date[:4]}-{attempt_date[4:6]}-{attempt_date[6:8]}"

    # ── Timeline (first and last timestamps) ──
    timestamps = [m.group(1) for m in RE_TIMESTAMP.finditer(text)]
    attempt_start = f"{attempt_date}T{timestamps[0]}" if timestamps and attempt_date else None
    attempt_end = f"{attempt_date}T{timestamps[-1]}" if timestamps and attempt_date else None

    # ── API calls ──
    api_calls = []
    for m in RE_API_CALL.finditer(text):
        api_calls.append({
            "call_number": int(m.group(1)),
            "model": m.group(2),
            "prompt_tokens": int(m.group(4)),
            "completion_tokens": int(m.group(5)),
            "total_tokens": int(m.group(6)),
            "latency_ms": int(float(m.group(7)) * 1000),
            "cached_tokens": int(m.group(8)) if m.group(8) else 0,
            "cache_hit_pct": float(m.group(9)) if m.group(9) else 0.0,
        })

    # ── Tool calls ──
    # Build a map of call_number -> tool name from Tool call: lines
    tool_name_map: dict[int, str] = {}
    tool_call_errors: dict[int, str] = {}
    for m in RE_TOOL_CALL.finditer(text):
        # We need to correlate call events with their position in the stream.
        # The tool calls are numbered in order of appearance in the log.
        pass

    # Better approach: iterate through tool completion/error events which
    # are sequential and have precise timing
    tool_calls = []
    # Collect all tool events in order
    events: list[dict] = []
    tool_call_count = 0

    for m in RE_TOOL_DONE.finditer(text):
        tool_call_count += 1
        events.append({
            "type": "done",
            "tool_name": m.group(1),
            "duration_ms": int(float(m.group(2)) * 1000),
            "result_chars": int(m.group(3)),
            "success": True,
        })

    for m in RE_TOOL_ERROR.finditer(text):
        tool_call_count += 1
        events.append({
            "type": "error",
            "tool_name": m.group(1),
            "duration_ms": int(float(m.group(2)) * 1000),
            "result_chars": 0,
            "success": False,
        })

    # Sort events by position in file (they are in order already)
    for i, evt in enumerate(events, 1):
        error_type = None
        error_snippet = None

        # For errors, try to find the error text nearby in the log
        if not evt["success"]:
            # Search for error text around this position
            error_type = "tool_error"

        tool_calls.append({
            "call_number": i,
            "tool_name": evt["tool_name"],
            "success": 1 if evt["success"] else 0,
            "duration_ms": evt["duration_ms"],
            "result_size_chars": evt["result_chars"],
            "error_type": error_type,
            "error_snippet": None,
        })

    # ── Result ──
    result = None
    result_reason = None
    for m in RE_RESULT.finditer(text):
        line = m.group(0)
        # Skip lines that are part of the prompt (contain "RESULT:APPLIED -- submitted"
        # or other help text). Actual agent output is singular.
        if "--" in line and "RESULT:APPLIED" in line and len(line) > 30:
            continue  # prompt documentation line
        if "output RESULT:" in line.lower():
            continue  # instruction, not actual output
        result_code = m.group(1).lower()
        if result_code == "applied":
            result = "applied"
        elif result_code == "expired":
            result = "expired"
        elif result_code == "captcha":
            result = "captcha"
        elif result_code == "login_issue":
            result = "login_issue"
        else:  # FAILED
            reason = (m.group(2) or "unknown").strip()
            # Clean the reason
            reason = re.sub(r'[\s\-–—]+', '_', reason)
            reason = reason.rstrip(".,;:!?\"'- ")
            result = f"failed:{reason[:60]}"
            result_reason = (m.group(2) or "unknown").strip()[:200]

    # ── Rollups ──
    total_api = len(api_calls)
    total_prompt = sum(c["prompt_tokens"] for c in api_calls)
    total_completion = sum(c["completion_tokens"] for c in api_calls)
    total_cached = sum(c["cached_tokens"] for c in api_calls)
    total_latency = sum(c["latency_ms"] for c in api_calls)
    avg_latency = total_latency / total_api if total_api else 0

    total_tool = len(tool_calls)
    total_tool_errs = sum(1 for t in tool_calls if not t["success"])
    total_tool_dur = sum(t["duration_ms"] for t in tool_calls)

    # ── Duration from file timestamps as fallback ──
    duration_ms = None
    if len(timestamps) >= 2:
        try:
            t0 = datetime.strptime(timestamps[0], "%H:%M:%S")
            t1 = datetime.strptime(timestamps[-1], "%H:%M:%S")
            # Handle day rollover
            if t1 < t0:
                from datetime import timedelta
                t1 += timedelta(days=1)
            duration_ms = int((t1 - t0).total_seconds() * 1000)
        except ValueError:
            pass

    return {
        "job_url": job_url,
        "job_title": job_title,
        "company": company,
        "ats_type": ats,
        "attempt_date": attempt_date,
        "attempt_start": attempt_start,
        "attempt_end": attempt_end,
        "duration_ms": duration_ms,
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "result": result,
        "result_reason": result_reason,
        "session_id": session_id,
        "log_file_path": str(log_path),
        "total_api_calls": total_api,
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_cached_tokens": total_cached,
        "total_latency_ms": total_latency,
        "avg_latency_ms": round(avg_latency, 1),
        "total_tool_calls": total_tool,
        "total_tool_errors": total_tool_errs,
        "total_tool_duration_ms": total_tool_dur,
        "_api_calls": api_calls,
        "_tool_calls": tool_calls,
    }


def insert_attempt(conn: sqlite3.Connection, data: dict) -> int | None:
    """Insert a parsed attempt and its children into the DB.

    Returns the inserted job_attempts.id, or None if already exists.
    """
    if not data:
        return None

    cur = conn.execute(
        "SELECT id FROM job_attempts WHERE log_file_path = ?",
        (data["log_file_path"],),
    )
    existing = cur.fetchone()
    if existing:
        return existing[0]  # already parsed

    cur = conn.execute(
        """INSERT INTO job_attempts (
            job_url, job_title, company, ats_type,
            attempt_date, attempt_start, attempt_end, duration_ms,
            provider, model, base_url,
            result, result_reason, session_id, log_file_path,
            total_api_calls, total_prompt_tokens, total_completion_tokens,
            total_cached_tokens, total_latency_ms, avg_latency_ms,
            total_tool_calls, total_tool_errors, total_tool_duration_ms
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            data["job_url"], data["job_title"], data["company"], data["ats_type"],
            data["attempt_date"], data["attempt_start"], data["attempt_end"], data["duration_ms"],
            data["provider"], data["model"], data["base_url"],
            data["result"], data["result_reason"], data["session_id"], data["log_file_path"],
            data["total_api_calls"], data["total_prompt_tokens"], data["total_completion_tokens"],
            data["total_cached_tokens"], data["total_latency_ms"], data["avg_latency_ms"],
            data["total_tool_calls"], data["total_tool_errors"], data["total_tool_duration_ms"],
        ),
    )
    attempt_id = cur.lastrowid

    # Insert api_calls
    for ac in data.get("_api_calls", []):
        conn.execute(
            """INSERT INTO api_calls
               (attempt_id, call_number, model, prompt_tokens, completion_tokens,
                total_tokens, cached_tokens, latency_ms, cache_hit_pct)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                attempt_id, ac["call_number"], ac["model"],
                ac["prompt_tokens"], ac["completion_tokens"],
                ac["total_tokens"], ac["cached_tokens"],
                ac["latency_ms"], ac["cache_hit_pct"],
            ),
        )

    # Insert tool_calls
    for tc in data.get("_tool_calls", []):
        conn.execute(
            """INSERT INTO tool_calls
               (attempt_id, call_number, tool_name, success, duration_ms,
                result_size_chars, error_type, error_snippet)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                attempt_id, tc["call_number"], tc["tool_name"],
                tc["success"], tc["duration_ms"],
                tc["result_size_chars"], tc["error_type"], tc["error_snippet"],
            ),
        )

    return attempt_id


def parse_all(log_dir: str | Path | None = None, db_path: str | Path | None = None,
              batch_size: int = 50) -> dict:
    """Parse all unparsed job logs into the analytics database.

    Args:
        log_dir: Directory containing claude_*.txt files (default: ~/.applypilot/logs)
        db_path: Path to analytics database (default: ~/.applypilot/analytics.db)
        batch_size: Commit every N inserts (default: 50)

    Returns:
        dict with counts: parsed, skipped, errors
    """
    log_dir = Path(log_dir or Path.home() / ".applypilot" / "logs")
    db_path = db_path or DB_PATH

    init_db(db_path)
    conn = sqlite3.connect(str(db_path))

    results: dict = {"parsed": 0, "skipped": 0, "errors": 0, "total_files": 0}

    # Collect log files, newest first
    log_files = sorted(log_dir.glob("claude_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    results["total_files"] = len(log_files)

    # Build a set of already-parsed paths for fast lookup
    already_parsed: set[str] = set()
    try:
        for row in conn.execute("SELECT log_file_path FROM job_attempts"):
            already_parsed.add(row[0])
    except Exception:
        pass  # table may not exist yet

    start = time.monotonic()
    last_report = start

    for idx, log_path in enumerate(log_files, 1):
        path_str = str(log_path)

        # Skip empty files (crash-on-startup)
        if log_path.stat().st_size < 500:
            results["skipped"] += 1
            continue

        if path_str in already_parsed:
            results["skipped"] += 1
            continue

        try:
            data = parse_log_file(log_path)
            if data and data["job_url"]:
                insert_attempt(conn, data)
                results["parsed"] += 1
            else:
                results["skipped"] += 1
        except Exception as e:
            logger.exception("Error parsing %s: %s", log_path, e)
            results["errors"] += 1

        # Batch commit
        if results["parsed"] % batch_size == 0 and results["parsed"] > 0:
            conn.commit()

        # Progress report every 5 seconds
        now = time.monotonic()
        if now - last_report > 5:
            elapsed = int(now - start)
            rate = results["parsed"] / max(elapsed, 1)
            remaining = (len(log_files) - idx) / max(rate, 0.1)
            print(
                f"[analytics] {idx}/{len(log_files)} files | "
                f"parsed={results['parsed']} skipped={results['skipped']} "
                f"errors={results['errors']} | "
                f"{rate:.1f} files/s | ~{remaining:.0f}s remaining",
                flush=True,
            )
            last_report = now

    conn.commit()
    conn.close()

    elapsed = int(time.monotonic() - start)
    print(
        f"[analytics] Done in {elapsed}s: "
        f"parsed={results['parsed']}, skipped={results['skipped']}, "
        f"errors={results['errors']}, total={results['total_files']}",
    )
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = parse_all()
    print(f"Parsed: {result['parsed']}, Skipped: {result['skipped']}, Errors: {result['errors']}, Total: {result['total_files']}")
