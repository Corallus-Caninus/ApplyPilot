#!/usr/bin/env python3
"""Inject autofill script into Chrome via CDP on every new document.
Called from run_apply.py after Chrome starts. Reads the field_cache
table from applypilot.db and injects a script that auto-fills forms
on every page load using the cached values.

Usage:  python3 inject_autofill.py [cdp_port=9515]
"""
import json, os, sys, sqlite3, time

CDP_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9515

# ── Load field cache from DB ──────────────────────────────────────────────
DB = os.path.expanduser("~/.applypilot/applypilot.db")
cache = {}
if os.path.exists(DB):
    try:
        conn = sqlite3.connect(DB)
        for row in conn.execute("SELECT label, value FROM field_cache"):
            key = row[0].strip().lower().replace("*", "").replace(" ", "")
            val = row[1].strip()
            if key and val:
                cache[key] = val
        conn.close()
    except Exception:
        pass

if not cache:
    sys.exit(0)

cache_json = json.dumps(cache)

# ── CDP inject script via WebSocket ─────────────────────────────────────
# JS that runs on every new document: matches cached labels vs form fields
AUTOFILL_JS = f"""
(() => {{
  if (window.__applypilot_autofill) return;
  window.__applypilot_autofill = true;
  const C = {cache_json};
  const N = s => s.replace(/[*\\s_\\-]+/g,"").toLowerCase().trim();
  const L = e => {{
    let l = e.getAttribute("aria-label");
    if (l) return N(l);
    const i = e.id && document.querySelector(`label[for="${{e.id}}"]`);
    if (i) {{ l = i.textContent; if (l) return N(l); }}
    l = e.getAttribute("placeholder");
    if (l) return N(l);
    l = e.getAttribute("name");
    if (l) return N(l);
    return null;
  }};
  const D = () => {{
    let f = 0;
    document.querySelectorAll("input:not([type=hidden]):not([type=file]),select,textarea").forEach(e => {{
      const l = L(e);
      if (!l) return;
      const v = C[l];
      if (!v || e.value) return;
      if (e.tagName === "SELECT") {{
        const m = [...e.options].find(o => o.text.toLowerCase().includes(v.toLowerCase()));
        if (m) {{ e.value = m.value; f++; }}
      }} else {{ e.value = v; f++; }}
      e.dispatchEvent(new Event("input",{{bubbles:true}}));
      e.dispatchEvent(new Event("change",{{bubbles:true}}));
    }});
    if (f) console.log(`[autofill] Pre-filled ${{f}} field(s)`);
  }};
  D();
  setTimeout(D, 1500);
  setTimeout(D, 4000);
  // Also watch for dynamically loaded forms
  new MutationObserver(D).observe(document.body, {{childList:true, subtree:true}});
}})();
"""

try:
    import requests
    # Get WebSocket debug URL from any page target
    targets = requests.get(f"http://127.0.0.1:{CDP_PORT}/json", timeout=5).json()
    ws_url = None
    for t in targets:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            ws_url = t["webSocketDebuggerUrl"]
            break
    if not ws_url and targets:
        ws_url = targets[0].get("webSocketDebuggerUrl")
    
    if ws_url:
        import websocket
        ws = websocket.create_connection(ws_url, timeout=10)
        # Send Page.addScriptToEvaluateOnNewDocument
        cmd = json.dumps({
            "id": 1,
            "method": "Page.addScriptToEvaluateOnNewDocument",
            "params": {"source": AUTOFILL_JS}
        })
        ws.send(cmd)
        resp = json.loads(ws.recv())
        ws.close()
        if "result" in resp:
            print(f"[autofill] Injected {len(cache)} fields into every page", flush=True)
    else:
        print("[autofill] No page target found", flush=True)
except Exception as e:
    print(f"[autofill] Failed: {e}", flush=True)
