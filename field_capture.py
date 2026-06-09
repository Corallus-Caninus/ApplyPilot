#!/usr/bin/env python3
"""
Capture filled form fields from Chrome after a successful application.

Connects to Chrome DevTools Protocol on the given port, navigates to
the active tab, runs a JS snippet that extracts all filled form fields
(label→value pairs), and saves them to the field cache.

Usage:  python3 field_capture.py [cdp_port=9515]
"""
import json, os, sys
from pathlib import Path

CDP_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9515
FIELD_CACHE = os.path.expanduser("~/.applypilot/field_cache.json")
FIELD_NORMALIZE_RE = __import__('re').compile(r"[*\s_\-]+")

CAPTURE_JS = (
    '(()=>{'
    'const N=s=>s.replace(/[*\\s_\\-]+/g,"").toLowerCase().trim();'
    'const L=e=>{'
        'let l=e.getAttribute("aria-label");'
        'if(l)return N(l);'
        'const i=e.id&&document.querySelector(`label[for="${e.id}"]`);'
        'if(i){l=i.textContent;if(l)return N(l);}'
        'l=e.getAttribute("placeholder");'
        'if(l)return N(l);'
        'l=e.getAttribute("name");'
        'if(l)return N(l);'
        'const a=e.getAttribute("aria-labelledby");'
        'if(a){const r=document.getElementById(a);'
        'if(r){l=r.textContent;if(l)return N(l);}}'
        'return null;'
    '};'
    'const pairs=[];'
    'document.querySelectorAll("input:not([type=hidden]):not([type=file]),select,textarea").forEach(e=>{'
        'const l=L(e);'
        'let v=e.value||"";'
        'if(e.tagName==="SELECT"&&e.selectedIndex>=0)v=e.options[e.selectedIndex].text;'
        'if(l&&v)pairs.push({label:l,value:v});'
    '});'
    'return JSON.stringify(pairs);'
    '})()'
)

def _get_tabs(port):
    """Get list of open tabs/pages via CDP HTTP."""
    import urllib.request
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5)
        return json.loads(resp.read())
    except Exception as e:
        print(f"[field_capture] Failed to get tabs: {e}", file=sys.stderr)
        return []

def _get_ws_url(port):
    """Get the browser WS URL from the first available tab."""
    tabs = _get_tabs(port)
    if not tabs:
        return None
    # Use the active tab (or first tab with a URL that's not about:blank)
    for t in tabs:
        url = t.get("url", "")
        if url and "about:blank" not in url:
            return t.get("webSocketDebuggerUrl")
    return tabs[0].get("webSocketDebuggerUrl")

def _run_js(ws_url, js_code):
    """Evaluate JS in the page via CDP and return the result."""
    import websocket
    ws = websocket.create_connection(ws_url, timeout=10)
    msg_id = 1

    def _send(method, params=None):
        nonlocal msg_id
        payload = json.dumps({"id": msg_id, "method": method, "params": params or {}})
        ws.send(payload)
        msg_id += 1
        resp = ws.recv()
        return json.loads(resp)

    # Enable Page domain first
    _send("Page.enable")
    # Need Runtime domain to evaluate
    _send("Runtime.enable")

    result = _send("Runtime.evaluate", {
        "expression": js_code,
        "returnByValue": True,
        "awaitPromise": True,
    })
    ws.close()

    if "result" in result and "result" in result["result"]:
        val = result["result"]["result"].get("value", "")
        if val and isinstance(val, str):
            try:
                return json.loads(val)
            except json.JSONDecodeError:
                return val
        return val
    return None

def _save_to_cache(pairs):
    """Merge captured pairs into the field cache."""
    cache = {}
    if os.path.exists(FIELD_CACHE):
        with open(FIELD_CACHE) as f:
            cache = json.load(f)
    new_count = 0
    for p in pairs:
        key = FIELD_NORMALIZE_RE.sub("", p.get("label", "")).strip().lower()
        val = p.get("value", "").strip()
        if key and val and key not in cache:
            cache[key] = val
            new_count += 1
    with open(FIELD_CACHE, "w") as f:
        json.dump(cache, f, indent=2)
    return new_count, len(cache)

if __name__ == "__main__":
    ws_url = _get_ws_url(CDP_PORT)
    if not ws_url:
        print("[field_capture] No browser tab found", file=sys.stderr)
        sys.exit(1)

    pairs = _run_js(ws_url, CAPTURE_JS)
    if not pairs:
        print("[field_capture] No fields captured")
        sys.exit(0)

    new, total = _save_to_cache(pairs)
    print(f"[field_capture] Captured {len(pairs)} fields ({new} new, {total} total)")
