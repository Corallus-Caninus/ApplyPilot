#!/usr/bin/env python3
"""Seed the field cache from all past successful application sessions."""
import sqlite3, os, json, re

state_db = os.path.expanduser("~/.applypilot/hermes-home-0/state.db")
field_cache = os.path.expanduser("~/.applypilot/field_cache.json")

if not os.path.exists(state_db):
    print("No state.db found")
    exit(0)

conn = sqlite3.connect(state_db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Get ALL sessions with fill_form data
cur.execute("""SELECT DISTINCT session_id FROM messages 
    WHERE role='assistant' AND tool_calls LIKE '%fill_form%'
    ORDER BY id""")
sessions = [row[0] for row in cur.fetchall()]

norm_re = re.compile(r"[*\s_\-]+")
def norm(s):
    return norm_re.sub("", s).strip().lower()

cache = {}
if os.path.exists(field_cache):
    with open(field_cache) as f:
        cache = json.load(f)

SKIP_KEYS = {"password", "verify", "date", "search", "device", "checkbox", "terms", "accept"}

session_count = 0
for sid in sessions:
    cur.execute("""SELECT tool_calls FROM messages 
        WHERE session_id=? AND role='assistant' AND tool_calls LIKE '%fill_form%'
        ORDER BY id""", (sid,))
    rows = cur.fetchall()
    n = 0
    for row in rows:
        try:
            data = json.loads(row["tool_calls"])
            for call in data:
                fn = call.get("function", {})
                args_raw = fn.get("arguments", "{}")
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                fields = args.get("fields", [])
                if isinstance(fields, str):
                    fields = json.loads(fields)
                for f in fields:
                    label = f.get("name") or f.get("element", "")
                    val = f.get("value", "")
                    key = norm(label)
                    if key and val and key not in cache:
                        if any(x in key for x in SKIP_KEYS):
                            continue
                        cache[key] = val.strip()
                        n += 1
        except:
            pass
    if n:
        session_count += 1

conn.close()

with open(field_cache, "w") as f:
    json.dump(cache, f, indent=2)

print(f"Seeded from {session_count} sessions — {len(cache)} fields in cache:")
for k, v in sorted(cache.items()):
    print(f"  {k}: {v}")
