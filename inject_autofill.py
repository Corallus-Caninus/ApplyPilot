#!/usr/bin/env python3
"""Persistent daemon: inject autofill script into every Chrome page/iframe.

Reads field_cache from applypilot.db and injects a MutationObserver via
Page.addScriptToEvaluateOnNewDocument into every CDP target (main pages,
cross-origin iframes, popups).  Runs until stdin is closed or the daemon
is killed.

Uses the browser-level WebSocket (Target.setDiscoverTargets) to detect
new targets as they appear, so Greenhouse iframes and similar cross-origin
forms get autofilled automatically within seconds of loading.

Usage:  python3 inject_autofill.py [cdp_port=9516]
"""

import json, os, sys, sqlite3, time, threading

CDP_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9516
POLL_INTERVAL = 2  # seconds between target discovery scans
DB = os.path.expanduser("~/.applypilot/applypilot.db")
INJECTED_FILE = os.path.expanduser(f"~/.applypilot/autofill_injected_{CDP_PORT}.json")
_injected: set[str] = set()

# ── Load field cache from DB ──────────────────────────────────────────────
def load_cache() -> dict:
    cache = {}
    if os.path.exists(DB):
        try:
            conn = sqlite3.connect(DB)
            # Create table if it doesn't exist
            conn.execute("CREATE TABLE IF NOT EXISTS field_cache (label TEXT PRIMARY KEY, value TEXT)")
            for row in conn.execute("SELECT label, value FROM field_cache"):
                key = row[0].strip().lower().replace("*", "").replace(" ", "")
                val = row[1].strip()
                if key and val:
                    cache[key] = val
            conn.close()
        except Exception as e:
            print(f"[autofill] DB error: {e}", flush=True)
    return cache

cache = load_cache()

# Auto-seed from profile if cache is empty
if not cache:
    try:
        from seed_autofill_cache import seed
        n = seed()
        if n:
            cache = load_cache()
    except Exception:
        pass

if not cache:
    print("[autofill] No cached fields — exiting", flush=True)
    sys.exit(0)

cache_json = json.dumps(cache)

# ── The autofill JS injected into every document ─────────────────────────
# Runs on DOMContentLoaded, then again at 1.5s and 4s (for dynamic forms).
# Also watches for DOM mutations that add new form fields.
AUTOFILL_JS = f"""
(() => {{
  if (window.__applypilot_autofill) return;
  window.__applypilot_autofill = Date.now();
  const C = {cache_json};
  const filled = new Set();  // never refill same field twice, even if cleared
  const N = s => typeof s === 'string' ? s.replace(/[*\\s_\\-]+/g,"").toLowerCase().trim() : '';
  const L = e => {{
    let l = e.getAttribute('aria-label') || e.getAttribute('label');
    if (l) return N(l);
    const i = e.id && document.querySelector('label[for="' + e.id + '"]');
    if (i) {{ l = i.textContent; if (l) return N(l); }}
    l = e.getAttribute('placeholder') || e.getAttribute('name');
    if (l) return N(l);
    const p = e.closest('.field, .form-group, [class*=field], [class*=form]');
    if (p) {{ l = p.querySelector('.label, label, .field-label'); if (l) return N(l.textContent); }}
    return null;
  }};
  const F = () => {{
    let f = 0;
    document.querySelectorAll('input:not([type=hidden]):not([type=file]),select,textarea,div[contenteditable]').forEach(e => {{
      const l = L(e);
      if (!l) return;
      const key = l + '::' + (e.name || e.id || '');
      if (filled.has(key)) return;  // already filled this field on this page
      const v = C[l];
      if (!v || e.value) return;
      filled.add(key);
      if (e.tagName === 'SELECT') {{
        const m = [...e.options].find(o => o.text.toLowerCase().includes(v.toLowerCase()));
        if (m) {{ e.value = m.value; f++; }}
      }} else if (e.isContentEditable) {{
        e.textContent = v; f++;
      }} else {{
        e.value = v; f++;
      }}
      ['input','change','blur'].forEach(ev => e.dispatchEvent(new Event(ev,{{bubbles:true}})));
    }});
    if (f) console.log('[autofill] Filled ' + f + ' field(s) on', window.location.href);
  }};
  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', F);
  }} else {{
    F();
  }}
  setTimeout(F, 1500);
  setTimeout(F, 4000);
  setTimeout(F, 8000);
  new MutationObserver(F).observe(document.body, {{childList:true, subtree:true, attributes:false}});
}})();
"""

# ── CDP helpers ───────────────────────────────────────────────────────────
def _req(url: str, timeout: float = 5) -> dict | list:
    """Simple HTTP GET with json parse."""
    import urllib.request
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        return json.loads(resp.read().decode())
    except Exception:
        return {}


def inject_target(ws_url: str) -> bool:
    """Inject autofill JS into one CDP target via WebSocket."""
    try:
        import websocket
        ws = websocket.create_connection(ws_url, timeout=10)
        cmd = json.dumps({
            "id": 1,
            "method": "Page.addScriptToEvaluateOnNewDocument",
            "params": {"source": AUTOFILL_JS}
        })
        ws.send(cmd)
        resp = json.loads(ws.recv())
        ws.close()
        return "result" in resp
    except Exception:
        return False


# ── Load previously injected targets ──────────────────────────────────────
def load_injected():
    global _injected
    try:
        if os.path.exists(INJECTED_FILE):
            with open(INJECTED_FILE) as f:
                _injected = set(json.load(f))
    except Exception:
        _injected = set()


def save_injected():
    try:
        with open(INJECTED_FILE, "w") as f:
            json.dump(list(_injected), f)
    except Exception:
        pass


# ── Main daemon loop ──────────────────────────────────────────────────────
def main():
    global _injected
    load_injected()
    print(f"[autofill] Daemon started on port {CDP_PORT} ({len(cache)} fields, {len(_injected)} previously injected)", flush=True)

    while True:
        try:
            targets = _req(f"http://127.0.0.1:{CDP_PORT}/json")
            if not isinstance(targets, list):
                time.sleep(POLL_INTERVAL)
                continue

            for t in targets:
                tid = t.get("id", "")
                if not tid or tid in _injected:
                    continue
                ws_url = t.get("webSocketDebuggerUrl")
                if not ws_url:
                    continue
                # Skip non-page targets (background workers, service workers, etc.)
                if t.get("type") not in ("page", "iframe"):
                    continue
                if inject_target(ws_url):
                    _injected.add(tid)
                    url = t.get("url", "?")[:80]
                    print(f"[autofill] Injected into {t.get('type','?')}: {url}", flush=True)
                    save_injected()

        except Exception as e:
            # Chrome not ready yet or disconnected — keep trying
            pass

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
