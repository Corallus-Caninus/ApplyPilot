#!/usr/bin/env python3
"""ApplyPilot Analytics CLI — parse logs, run queries, track prompt improvements.

Usage:
    python3 -m applypilot.analytics parse              # Parse all job logs into DB
    python3 -m applypilot.analytics report             # Full analytics report
    python3 -m applypilot.analytics report --json     # JSON output
    python3 -m applypilot.analytics query <name>      # Single query
    python3 -m applypilot.analytics list-queries       # Show available queries
    python3 -m applypilot.analytics watch              # Parse-only mode (for cron)
"""

import json
import sys
from pathlib import Path

# Ensure src is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from applypilot.analytics import parser
from applypilot.analytics import queries as qry
from applypilot.analytics.queries import QUERIES


def cmd_parse():
    result = parser.parse_all()
    print(f"[analytics] Parsed: {result['parsed']}, "
          f"Skipped: {result['skipped']}, "
          f"Errors: {result['errors']}, "
          f"Total files: {result['total_files']}")


def cmd_report():
    as_json = "--json" in sys.argv
    if as_json:
        print(qry.run_queries(as_json=True))
    else:
        qry.print_report(qry.run_queries())


def cmd_query():
    name = sys.argv[2] if len(sys.argv) > 2 else None
    if not name or name not in QUERIES:
        print(f"Available queries: {', '.join(QUERIES.keys())}", file=sys.stderr)
        sys.exit(1)
    as_json = "--json" in sys.argv
    results = qry.run_queries(query_names=[name], as_json=as_json)
    if as_json:
        print(results)
    else:
        qry.print_report(results)


def cmd_list_queries():
    print("Available analytics queries:\n")
    for name, sql in QUERIES.items():
        desc = sql.strip().split("\n")[0].lstrip("- ").strip(":")
        print(f"  {name:25s}  {desc}")


def cmd_watch():
    """Parse-only mode for cron — no output on success, errors on stderr."""
    import logging
    logging.basicConfig(level=logging.WARNING)
    result = parser.parse_all()
    if result["errors"]:
        print(f"[analytics] {result['errors']} parse errors", file=sys.stderr)


COMMANDS = {
    "parse": cmd_parse,
    "report": cmd_report,
    "query": cmd_query,
    "list-queries": cmd_list_queries,
    "watch": cmd_watch,
}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]
    fn = COMMANDS.get(cmd)
    if fn:
        fn()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
