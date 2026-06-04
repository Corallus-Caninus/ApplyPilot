"""Analytics queries for continuous prompt improvement.

Run:  python3 -m applypilot.analytics.queries [--json]
"""

import json
import sqlite3
from pathlib import Path

from applypilot.analytics.schema import DB_PATH

# ── Named queries ────────────────────────────────────────────────────────────

QUERIES: dict[str, str] = {
    "model-performance": """
        -- How does each model perform? Success rate, speed, tool errors
        SELECT model, provider, attempts, applied, success_pct,
               avg_duration_s, avg_api_calls, avg_tool_errors,
               avg_api_latency_ms, avg_tokens
        FROM model_performance
        WHERE attempts >= 2
        ORDER BY success_pct DESC, attempts DESC
    """,

    "ats-breakdown": """
        -- Which ATS platforms are hardest with this model?
        SELECT ats_type, model, attempts, applied, success_pct,
               avg_duration_s, avg_tool_errors
        FROM ats_performance
        WHERE attempts >= 2
        ORDER BY success_pct ASC
    """,

    "error-patterns": """
        -- Most common tool errors, by model
        SELECT error_type, tool_name, model, occurrences, avg_wasted_s
        FROM error_analysis
        WHERE occurrences >= 2
        ORDER BY occurrences DESC
    """,

    "tool-usage": """
        -- Tool usage patterns by model
        SELECT tool_name, model, calls, success_pct, avg_duration_ms
        FROM tool_usage
        WHERE calls >= 5
        ORDER BY calls DESC
    """,

    "param-errors": """
        -- Specifically: ref vs target parameter confusion (nemotron's weakness)
        SELECT ja.model, COUNT(*) as errors, ROUND(AVG(tc.duration_ms)/1000, 1) as avg_wasted_s,
               ROUND(100.0 * COUNT(*) / NULLIF(ja_total.total_calls, 0), 1) as pct_of_all_calls
        FROM tool_calls tc
        JOIN job_attempts ja ON ja.id = tc.attempt_id
        JOIN (
            SELECT ja2.model, COUNT(*) as total_calls
            FROM tool_calls tc2
            JOIN job_attempts ja2 ON ja2.id = tc2.attempt_id
            WHERE tc2.success = 0
            GROUP BY ja2.model
        ) ja_total ON ja_total.model = ja.model
        WHERE tc.success = 0
          AND tc.error_type = 'parameter_error'
        GROUP BY ja.model
        ORDER BY errors DESC
    """,

    "no-result": """
        -- Jobs where agent never output a RESULT line
        SELECT ja.attempt_date, ja.company, ja.job_title, ja.model,
               ja.total_api_calls, ja.duration_ms
        FROM job_attempts ja
        WHERE ja.result IS NULL
        ORDER BY ja.attempt_date DESC
    """,

    "success-timeline": """
        -- Success rate over time (track prompt improvements)
        SELECT ja.attempt_date, ja.model,
               COUNT(*) as attempts,
               SUM(CASE WHEN ja.result = 'applied' THEN 1 ELSE 0 END) as applied,
               ROUND(100.0 * SUM(CASE WHEN ja.result = 'applied' THEN 1 ELSE 0 END)
                     / NULLIF(COUNT(*), 0), 1) as success_pct,
               ROUND(AVG(ja.duration_ms)/1000, 0) as avg_duration_s
        FROM job_attempts ja
        GROUP BY ja.attempt_date, ja.model
        ORDER BY ja.attempt_date DESC
    """,

    "tool-confusion-loop": """
        -- Detect wasted call chains: same tool called 3+ times in a row with same error
        SELECT tc1.attempt_id, tc1.tool_name, ja.model, ja.company,
               COUNT(*) as wasted_calls,
               GROUP_CONCAT(tc1.error_type) as error_chain
        FROM tool_calls tc1
        JOIN tool_calls tc2 ON tc2.attempt_id = tc1.attempt_id
            AND tc2.call_number = tc1.call_number + 1
            AND tc2.tool_name = tc1.tool_name
            AND tc2.success = 0
        JOIN job_attempts ja ON ja.id = tc1.attempt_id
        WHERE tc1.success = 0
        GROUP BY tc1.attempt_id, tc1.tool_name
        HAVING wasted_calls >= 2
        ORDER BY wasted_calls DESC
    """,

    "per-day-summary": """
        -- Daily summary with token costs and tool error rates
        SELECT ja.attempt_date, ja.model,
               COUNT(*) as attempts,
               ROUND(100.0 * SUM(CASE WHEN ja.result = 'applied' THEN 1 ELSE 0 END)
                     / NULLIF(COUNT(*), 0), 1) as success_pct,
               ROUND(AVG(COALESCE(ja.total_tool_errors, 0)), 1) as avg_tool_errors,
               ROUND(AVG(ja.total_api_calls), 0) as avg_api_calls,
               ROUND(AVG(ja.total_prompt_tokens + ja.total_completion_tokens), 0) as avg_tokens,
               ROUND(AVG(ja.duration_ms)/1000, 0) as avg_duration_s
        FROM job_attempts ja
        GROUP BY ja.attempt_date, ja.model
        ORDER BY ja.attempt_date DESC
    """,

    "stuck-sites": """
        -- Sites where agents get stuck the most
        SELECT ja.company, ja.ats_type, ja.model,
               COUNT(*) as attempts,
               SUM(CASE WHEN ja.result LIKE 'failed:stuck%' THEN 1 ELSE 0 END) as stuck_count,
               ROUND(100.0 * SUM(CASE WHEN ja.result LIKE 'failed:stuck%' THEN 1 ELSE 0 END)
                     / NULLIF(COUNT(*), 0), 1) as stuck_pct,
               ROUND(AVG(ja.duration_ms)/1000, 0) as avg_duration_s
        FROM job_attempts ja
        GROUP BY ja.company, ja.ats_type, ja.model
        HAVING attempts >= 2
        ORDER BY stuck_pct DESC
    """,
}


def run_queries(db_path: str | Path | None = None, as_json: bool = False,
                query_names: list[str] | None = None) -> dict | str:
    """Run analytics queries against the analytics database.

    Args:
        db_path: Path to analytics database.
        as_json: If True, return JSON string instead of dict.
        query_names: Subset of queries to run (None = all).

    Returns:
        Dict mapping query name -> list of row dicts.
    """
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    names = query_names or list(QUERIES.keys())
    results = {}

    for name in names:
        sql = QUERIES.get(name)
        if not sql:
            continue
        try:
            rows = conn.execute(sql).fetchall()
            results[name] = [dict(r) for r in rows]
        except Exception as e:
            results[name] = {"error": str(e)}

    conn.close()

    if as_json:
        return json.dumps(results, indent=2, default=str)
    return results


def print_report(results: dict) -> None:
    """Print a human-readable analytics report."""
    for qname, rows in results.items():
        title = qname.replace("-", " ").title()
        print(f"\n{'=' * 72}")
        print(f"  {title}")
        print(f"{'=' * 72}")

        if isinstance(rows, dict) and "error" in rows:
            print(f"  ERROR: {rows['error']}")
            continue

        if not rows:
            print("  (no data)")
            continue

        # Print header from keys
        headers = list(rows[0].keys())
        # Column widths
        widths = {}
        for h in headers:
            str_vals = [str(r[h] or "") for r in rows] + [h]
            widths[h] = max(len(v) for v in str_vals) + 2

        # Header row
        for h in headers:
            print(f"  {h.upper():<{widths[h]}}", end="")
        print()

        # Separator
        for h in headers:
            print(f"  {'-' * (widths[h] - 2):<{widths[h]}}", end="")
        print()

        # Data rows
        for row in rows:
            for h in headers:
                val = row[h] if row[h] is not None else ""
                print(f"  {str(val):<{widths[h]}}", end="")
            print()

    print()


if __name__ == "__main__":
    import sys
    as_json = "--json" in sys.argv
    results = run_queries(as_json=as_json)
    if as_json:
        print(results)
    else:
        print_report(results)
