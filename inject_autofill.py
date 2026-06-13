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
  if (window.__applypilot_autofill_v2) return;
  window.__applypilot_autofill = Date.now();
  window.__applypilot_autofill_v2 = Date.now();
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
    // ── Handle comboboxes (div-based dropdowns like Greenhouse country) ──
    document.querySelectorAll('[role="combobox"]').forEach(e => {{
      const l = L(e); if (!l) return;
      const key = l + '::' + (e.id || '');
      if (filled.has(key)) return;
      const v = C[l]; if (!v || e.value) return;
      filled.add(key);
      const root = e.closest('[class*="dropdown"],[class*="select"],[class*="field"],[class*="wrapper"]') || e.parentElement?.parentElement;
      if (!root) return;
      // Check if option list is already rendered
      let lb = root.querySelector('[role="listbox"]');
      if (lb) {{
        const opt = [...lb.querySelectorAll('[role="option"]')].find(o => o.textContent.toLowerCase().includes(v.toLowerCase()));
        if (opt) {{ opt.click(); f++; }}
      }} else {{
        // Click expand toggle so options render for the next pass
        const btn = e.parentElement?.querySelector('button');
        if (btn) btn.click();
      }}
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
    """Inject autofill JS into one CDP target via WebSocket.
    
    Uses addScriptToEvaluateOnNewDocument for future pages/iframes,
    AND Runtime.evaluate for pages already loaded.
    """
    try:
        import websocket
        ws = websocket.create_connection(ws_url, timeout=10)

        # Register for future documents
        cmd1 = json.dumps({
            "id": 1,
            "method": "Page.addScriptToEvaluateOnNewDocument",
            "params": {"source": AUTOFILL_JS}
        })
        ws.send(cmd1)
        resp1 = json.loads(ws.recv())

        # Run on current page immediately (handles already-loaded pages)
        cmd2 = json.dumps({
            "id": 2,
            "method": "Runtime.evaluate",
            "params": {
                "expression": f"(()=>{{{AUTOFILL_JS}}})()",
                "awaitPromise": False,
            }
        })
        ws.send(cmd2)
        resp2 = json.loads(ws.recv())

        ws.close()
        return "result" in resp1
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
_RELOAD_INTERVAL = 15  # reload cache from DB every N poll cycles
_reload_counter = 0

def main():
    global _injected, cache, cache_json, _reload_counter
    load_injected()
    print(f"[autofill] Daemon started on port {CDP_PORT} ({len(cache)} fields, {len(_injected)} previously injected)", flush=True)

    while True:
        try:
            # Periodically reload cache to pick up newly captured fields
            _reload_counter += 1
            if _reload_counter >= _RELOAD_INTERVAL:
                _reload_counter = 0
                new_cache = load_cache()
                if len(new_cache) > len(cache):
                    cache = new_cache
                    cache_json = json.dumps(cache)
                    print(f"[autofill] Cache updated: {len(cache)} fields", flush=True)
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
