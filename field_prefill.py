#!/usr/bin/env python3
"""
Inject field-cache prefill into all open Chrome tabs.

Reads ~/.applypilot/applypilot.db and injects a JS snippet into every
open page tab via CDP's Page.addScriptToEvaluateOnNewDocument.  This causes
the prefill JS to run on every page load/navigation within that tab.

Usage:  python3 field_prefill.py [cdp_port=9515]
"""
import json, os, sys, time, sqlite3

CDP_PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 9515
FIELD_DB = os.path.expanduser("~/.applypilot/applypilot.db")

def _build_prefill_js():
    cache = {}
    if os.path.exists(FIELD_DB):
        try:
            conn = sqlite3.connect(FIELD_DB)
            cur = conn.cursor()
            cur.execute("SELECT label, value FROM field_cache")
            for row in cur.fetchall():
                cache[row[0]] = row[1]
            conn.close()
        except Exception:
            pass
    if not cache:
        return ""
    js_data = json.dumps(cache)
    return (
        '(()=>{'
        'const C=' + js_data + ';'
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
        'let f=0;'
        'const D=()=>{'
            'document.querySelectorAll("input:not([type=hidden]):not([type=file]),select,textarea").forEach(e=>{'
                'const l=L(e);'
                'if(!l)return;'
                'const v=C[l];'
                'if(!v||e.value)return;'
                'if(e.tagName==="SELECT"){'
                    'const ov=v.toLowerCase();'
                    'const m=[...e.options].find(o=>o.text.toLowerCase().includes(ov)||o.value.toLowerCase()===ov);'
                    'if(m){e.value=m.value;f++;}'
                '}else{e.value=v;f++}'
                'e.dispatchEvent(new Event("input",{bubbles:true}));'
                'e.dispatchEvent(new Event("change",{bubbles:true}));'
            '});'
        '};'
        'D();setTimeout(D,1500);setTimeout(D,4000);'
        'console.log(`[field_cache] Auto-filled ${f} field(s)`);'
        '})()'
    )

def get_tabs(port):
    import urllib.request
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5)
        return json.loads(resp.read())
    except Exception as e:
        print(f"[field_prefill] Failed to list tabs: {e}", file=sys.stderr)
        return []

def inject_into_tab(ws_url, script):
    import websocket
    try:
        ws = websocket.create_connection(
            ws_url, timeout=10,
            header={"Origin": f"http://127.0.0.1:{CDP_PORT}"}
        )
        msg_id = 1
        def send(method, params=None):
            nonlocal msg_id
            mid = msg_id
            msg_id += 1
            ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            return mid

        send("Page.addScriptToEvaluateOnNewDocument", {
            "source": script,
            "runImmediately": True,
        })
        # Read the response
        ws.settimeout(2)
        try:
            resp = json.loads(ws.recv())
            if "result" in resp and "identifier" in resp.get("result", {}):
                ident = resp["result"]["identifier"]
                ws.close()
                return ident
        except:
            pass
        ws.close()
        return None
    except Exception as e:
        print(f"[field_prefill] Inject failed for {ws_url[:50]}: {e}", file=sys.stderr)
        return None

if __name__ == "__main__":
    script = _build_prefill_js()
    if not script:
        print("[field_prefill] No cached fields — nothing to inject")
        sys.exit(0)

    # Retry connecting to Chrome for up to 15s (Chrome takes a few seconds to start)
    tabs = []
    for attempt in range(15):
        tabs = get_tabs(CDP_PORT)
        page_tabs = [t for t in tabs if t.get("type") == "page"]
        if page_tabs:
            break
        time.sleep(1)
    page_tabs = [t for t in tabs if t.get("type") == "page"]
    if not page_tabs:
        print("[field_prefill] No page tabs found — try again after navigating")
        sys.exit(0)

    injected = 0
    for tab in page_tabs:
        ws_url = tab.get("webSocketDebuggerUrl", "")
        if ws_url:
            result = inject_into_tab(ws_url, script)
            if result is not None:
                injected += 1

    print(f"[field_prefill] Injected prefill into {injected}/{len(page_tabs)} tabs")
