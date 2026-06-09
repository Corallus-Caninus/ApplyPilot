#!/usr/bin/env python3
"""ApplyPilot MCP Server — credential + email + field cache tools for Hermes agent."""
import json, os, subprocess, sys, re

CRED_FILE = os.path.expanduser("~/.applypilot/credentials.json")
FIELD_DB = os.path.expanduser("~/.applypilot/applypilot.db")

def _cred_load():
    if os.path.exists(CRED_FILE):
        with open(CRED_FILE) as f:
            return json.load(f)
    return {}

def _cred_save(data):
    with open(CRED_FILE, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(CRED_FILE, 0o600)

def credentials_get(site):
    data = _cred_load()
    return data.get(site.lower(), {})

def credentials_save(site, email, password):
    data = _cred_load()
    data[site.lower()] = {"email": email, "password": password}
    _cred_save(data)
    return "Saved credentials for " + site

def credentials_list():
    data = _cred_load()
    return [{"site": s, "email": e.get("email",""), "has_password": bool(e.get("password",""))} for s, e in sorted(data.items())]

def credentials_update_password(site, new_password):
    data = _cred_load()
    sl = site.lower()
    if sl not in data:
        return "No credentials for " + site
    data[sl]["password"] = new_password
    _cred_save(data)
    return "Updated password for " + site

EMAIL_SCRIPT = os.path.expanduser("~/.applypilot/email_verifier.py")

def _run_email(*args):
    if not os.path.exists(EMAIL_SCRIPT):
        return json.dumps({"error": "email_verifier.py not found"})
    r = subprocess.run([sys.executable, EMAIL_SCRIPT] + list(args), capture_output=True, text=True, timeout=30)
    return r.stdout if r.returncode == 0 else json.dumps({"error": r.stderr or r.stdout})

def email_search(query):
    o = _run_email("search", query)
    try:
        return json.loads(o)
    except Exception:
        return [{"error": "parse failed: " + o[:200]}]

def email_read(msg_id):
    return _run_email("read", msg_id)

def email_extract_link(msg_id):
    return (_run_email("extract-link", msg_id) or "No link found").strip()

# ── Field cache — persistent label→value store ────────────────────────────
# Autofills known fields (name, email, phone, etc.) so the LLM doesn't
# waste tokens re-entering the same data on every application.

FIELD_NORMALIZE_RE = re.compile(r"[*•\s_\-]+")

def _field_normalize(label: str) -> str:
    """Normalize a field label for cache lookup: lowercase, strip punctuation."""
    return FIELD_NORMALIZE_RE.sub("", label).strip().lower()

def _field_load():
    label_re = re.compile(r"[*\s_\-]+")
    result = {}
    try:
        import sqlite3
        conn = sqlite3.connect(FIELD_DB)
        for row in conn.execute("SELECT label, value FROM field_cache"):
            result[row[0]] = row[1]
        conn.close()
    except Exception:
        pass
    return result

def field_list():
    """Return all cached field label→value pairs."""
    return _field_load()

def field_save_batch(pairs: list[dict]):
    """Save multiple field→value pairs to the cache. Overwrites existing keys.
    
    Each pair: {"label": "First Name", "value": "Josh"}
    Labels are normalized for lookup (case/whitespace/punctuation insensitive).
    """
    import sqlite3
    from datetime import datetime
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(FIELD_DB)
    saved = 0
    for pair in pairs:
        key = _field_normalize(pair.get("label", ""))
        if key and pair.get("value"):
            conn.execute(
                "INSERT OR REPLACE INTO field_cache (label, value, created_at, updated_at) VALUES (?, ?, COALESCE((SELECT created_at FROM field_cache WHERE label=?), ?), ?)",
                (key, pair["value"].strip(), key, now, now),
            )
            saved += 1
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM field_cache").fetchone()[0]
    conn.close()
    return f"Saved {saved} field(s) to cache ({total} total)"

def field_prefill_script():
    """Return a JS snippet that fills all cached fields on the current page.
    
    The agent should run this via mcp_playwright_browser_run_code_unsafe
    after page navigation but before calling fill_form or browser_fill.
    """
    cache = _field_load()
    if not cache:
        return "// No cached fields"
    js_data = json.dumps(cache)
    # Build JS snippet - inject cache as JSON in a var assignment
    script = (
        '(() => {'
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
                    'const m=[...e.options].find(o=>o.text.toLowerCase().includes(v.toLowerCase()));'
                    'if(m){e.value=m.value;f++;}'
                '}else{e.value=v;f++}'
                'e.dispatchEvent(new Event("input",{bubbles:true}));'
                'e.dispatchEvent(new Event("change",{bubbles:true}));'
            '});'
        '};'
        'D();setTimeout(D,1500);setTimeout(D,4000);'
        'return `Auto-filled ${f} field(s)`;'
        '})()'
    )
    return script

def field_capture_script():
    """Return a JS snippet that extracts all filled form fields on the current page.
    
    The agent runs this after RESULT:APPLIED, then calls field_save_batch()
    with the returned array to store the fields for future autofill.
    """
    return """(() => {
    const norm = s => s.replace(/[*•\\s_\\-]+/g,'').toLowerCase().trim();
    const findLabel = el => {
        let lbl = el.getAttribute('aria-label');
        if (lbl) return norm(lbl);
        const id = el.id && document.querySelector('label[for=\"'+el.id+'\"]');
        if (id) { lbl = id.textContent; if (lbl) return norm(lbl); }
        lbl = el.getAttribute('placeholder');
        if (lbl) return norm(lbl);
        lbl = el.getAttribute('name');
        if (lbl) return norm(lbl);
        const aria = el.getAttribute('aria-labelledby');
        if (aria) {
            const ref = document.getElementById(aria);
            if (ref) { lbl = ref.textContent; if (lbl) return norm(lbl); }
        }
        return null;
    };
    const pairs = [];
    document.querySelectorAll('input:not([type=hidden]):not([type=file]), select, textarea').forEach(el => {
        const label = findLabel(el);
        let value = el.value || '';
        if (el.tagName === 'SELECT' && el.selectedIndex >= 0) {
            value = el.options[el.selectedIndex].text;
        }
        if (label && value) pairs.push({label, value});
    });
    return JSON.stringify(pairs);
})();"""

TOOLS = [
    {"name":"credentials_get","description":"Get saved login credentials for a site (e.g. nvidia, workday, greenhouse).","inputSchema":{"type":"object","properties":{"site":{"type":"string"}},"required":["site"]}},
    {"name":"credentials_save","description":"Save login credentials for a site.","inputSchema":{"type":"object","properties":{"site":{"type":"string"},"email":{"type":"string"},"password":{"type":"string"}},"required":["site","email","password"]}},
    {"name":"credentials_list","description":"List all sites with saved credentials.","inputSchema":{"type":"object","properties":{}}},
    {"name":"credentials_update_password","description":"Update stored password for a site after reset.","inputSchema":{"type":"object","properties":{"site":{"type":"string"},"new_password":{"type":"string"}},"required":["site","new_password"]}},
    {"name":"email_search","description":"Search Gmail for verification/password-reset emails. Query: from:(domain) subject:(keyword OR keyword)","inputSchema":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},
    {"name":"email_read","description":"Read full email body by message ID (from email_search).","inputSchema":{"type":"object","properties":{"msg_id":{"type":"string"}},"required":["msg_id"]}},
    {"name":"email_extract_link","description":"Extract verification/reset link from an email.","inputSchema":{"type":"object","properties":{"msg_id":{"type":"string"}},"required":["msg_id"]}},
    {"name":"field_list","description":"List all cached form field label-value pairs for autofill.","inputSchema":{"type":"object","properties":{}}},
    {"name":"field_save_batch","description":"Save field label-value pairs from a completed application to the autofill cache. Each pair: {\"label\":\"First Name\",\"value\":\"Josh\"}","inputSchema":{"type":"object","properties":{"pairs":{"type":"array","items":{"type":"object","properties":{"label":{"type":"string"},"value":{"type":"string"}},"required":["label","value"]}}},"required":["pairs"]}},
    {"name":"field_prefill_script","description":"Return a JavaScript snippet that fills all cached fields into the current page. Run via mcp_playwright_browser_run_code_unsafe after navigation but before form filling.","inputSchema":{"type":"object","properties":{}}},
    {"name":"field_capture_script","description":"Return a JavaScript snippet that extracts all filled form fields on the current page. Run via mcp_playwright_browser_run_code_unsafe after submission, then call field_save_batch with the result.","inputSchema":{"type":"object","properties":{}}},
]

HANDLERS = {
    "credentials_get": lambda a: credentials_get(a["site"]),
    "credentials_save": lambda a: credentials_save(a["site"], a["email"], a["password"]),
    "credentials_list": lambda a: credentials_list(),
    "credentials_update_password": lambda a: credentials_update_password(a["site"], a["new_password"]),
    "email_search": lambda a: email_search(a["query"]),
    "email_read": lambda a: email_read(a["msg_id"]),
    "email_extract_link": lambda a: email_extract_link(a["msg_id"]),
    "field_list": lambda a: field_list(),
    "field_save_batch": lambda a: field_save_batch(a["pairs"]),
    "field_prefill_script": lambda a: field_prefill_script(),
    "field_capture_script": lambda a: field_capture_script(),
}

def handle(req):
    i, m = req.get("id"), req.get("method", "")
    # JSON-RPC notifications (no id) get no response
    if i is None:
        return None
    if m == "tools/list":
        return {"jsonrpc":"2.0","id":i,"result":{"tools":TOOLS}}
    if m == "tools/call":
        p = req.get("params", {})
        n, a = p.get("name",""), p.get("arguments", {})
        if n in HANDLERS:
            try:
                r = HANDLERS[n](a)
                return {"jsonrpc":"2.0","id":i,"result":{"content":[{"type":"text","text":json.dumps(r) if not isinstance(r,str) else r}]}}
            except Exception as e:
                return {"jsonrpc":"2.0","id":i,"error":{"code":-32603,"message":str(e)}}
        return {"jsonrpc":"2.0","id":i,"error":{"code":-32601,"message":"Unknown: "+n}}
    if m == "initialize":
        return {"jsonrpc":"2.0","id":i,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"applypilot","version":"1.0.0"}}}
    return {"jsonrpc":"2.0","id":i,"result":{}}

def main():
    for line in sys.stdin:
        l = line.strip()
        if not l: continue
        try:
            resp = handle(json.loads(l))
            if resp is not None:
                sys.stdout.write(json.dumps(resp)+"\n")
                sys.stdout.flush()
        except (json.JSONDecodeError, BrokenPipeError):
            pass

if __name__ == "__main__":
    main()
