#!/usr/bin/env python3
"""
CDP File Upload — upload a resume PDF to an ATS form via Chrome DevTools Protocol.

Auto-detects the correct browser: tries the Hermes agent's headless Chrome first,
then falls back to the user's Chrome (port 9515). Also auto-resolves the venv
Python so websocket-client dependency is available.

Usage:
    python3 cdp_upload.py <file_path>                    # auto-detect
    python3 cdp_upload.py <file_path> <css_selector>     # specific selector
    python3 cdp_upload.py <file_path> '' <port>          # specific port
    python3 cdp_upload.py --find                         # print the Hermes port
"""
import json
import os
import subprocess
import sys
import urllib.request

# ── Venv dependency bootstrap ──────────────────────────────────────────
# The websocket-client package is installed in the ApplyPilot venv. Add
# the venv site-packages to sys.path if needed.
_VENV_SITE = os.path.expanduser(
    "~/Code/applypilot/.venv/lib/python3.11/site-packages"
)
if os.path.isdir(_VENV_SITE) and _VENV_SITE not in sys.path:
    sys.path.insert(0, _VENV_SITE)
# ────────────────────────────────────────────────────────────────────────

import websocket


def find_hermes_port() -> int | None:
    """Find the Hermes agent's headless Chrome CDP port.

    Looks for headless Chrome/Chromium processes (started by agent-browser)
    that are NOT the user's Chrome on port 9515.
    """
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=5,
        )
        hermes_pids = set()
        for line in result.stdout.split("\n"):
            if "--headless=new" in line and "remote-debugging-port=" in line:
                parts = line.split()
                if parts:
                    try:
                        hermes_pids.add(int(parts[1]))
                    except (ValueError, IndexError):
                        pass

        if not hermes_pids:
            return None

        # Match PIDs to listening ports via ss
        ss_result = subprocess.run(
            ["ss", "-tlnp"],
            capture_output=True, text=True, timeout=5,
        )
        for line in ss_result.stdout.split("\n"):
            for pid in hermes_pids:
                if f"pid={pid}" in line or f",{pid}," in line:
                    parts = line.strip().split()
                    for p in parts:
                        if ":" in p and p.split(":")[-1].isdigit():
                            port = int(p.split(":")[-1])
                            if port != 9515:
                                return port
    except Exception:
        pass
    return None


def _get_pages(port: int) -> list[dict]:
    """Fetch the page list from Chrome's DevTools HTTP endpoint."""
    url = f"http://127.0.0.1:{port}/json"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read())


def _cdp_call(ws, method: str, params: dict | None = None) -> dict | None:
    """Send a CDP command and wait for its matching response."""
    msg_id = 1
    ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        resp = json.loads(ws.recv())
        if resp.get("id") == msg_id:
            if "error" in resp:
                raise RuntimeError(
                    f"CDP error in {method}: {resp['error']['message']}"
                )
            return resp.get("result")


def upload_file(file_path: str, css_selector: str = "input[type=file]",
                port: int | None = None) -> str:
    """Upload a file via CDP to the first available page.

    Auto-detects the correct browser: tries the Hermes headless Chrome
    first (it has the job form open), then falls back to the user's Chrome
    on port 9515.

    Args:
        file_path: Absolute path to the file to upload.
        css_selector: CSS selector for the file input element.
        port: CDP remote debugging port. If None, auto-detect.

    Returns:
        Status message.
    """
    if port is None:
        # Try Hermes headless browser first (it has the job form open)
        hermes_port = find_hermes_port()
        if hermes_port:
            port = hermes_port
        else:
            port = 9515

    pages = _get_pages(port)
    if not pages:
        raise RuntimeError("No browser pages found on port " + str(port))

    page = max(pages, key=lambda p: p.get("description", "").count("attached"))
    ws_url = page["webSocketDebuggerUrl"]

    ws = websocket.create_connection(ws_url, timeout=15)
    try:
        doc = _cdp_call(ws, "DOM.getDocument", {"depth": 0})
        if not doc or "root" not in doc:
            raise RuntimeError("DOM.getDocument returned no root")
        doc_node_id = doc["root"]["nodeId"]

        node_id = _find_file_input(ws, doc_node_id, css_selector)
        if node_id is None:
            raise RuntimeError(
                f"Could not find a file input element on the page. "
                f"Tried selectors: {_SELECTORS}"
            )

        _cdp_call(ws, "DOM.setFileInputFiles", {
            "nodeId": node_id,
            "files": [file_path],
        })

        _cdp_call(ws, "Runtime.evaluate", {
            "expression": (
                "(() => {"
                "  const el = document.activeElement || "
                "    document.querySelector('input[type=file]');"
                "  if (el) {"
                "    el.dispatchEvent(new Event('change', {bubbles: true}));"
                "    el.dispatchEvent(new Event('input', {bubbles: true}));"
                "  }"
                "})()"
            ),
            "userGesture": True,
        })

        title = page.get("title", "unknown page")
        return f"Uploaded {file_path} to \"{title}\" via CDP (port {port})"
    finally:
        ws.close()


_SELECTORS = [
    "input[type=file]",
    "input[name*=resume]",
    "input[name*=file]",
    "input[name*=cv]",
    "input[accept*=pdf]",
    "input[accept*=doc]",
    "input[accept*=application]",
    "form input[type=file]",
    "input[type=file]:not([style*='display:none'])",
]


def _find_file_input(ws, doc_node_id: int, primary_selector: str) -> int | None:
    if primary_selector != "input[type=file]":
        result = _cdp_call(ws, "DOM.querySelector", {
            "nodeId": doc_node_id,
            "selector": primary_selector,
        })
        nid = (result or {}).get("nodeId", 0)
        if nid and nid != 0:
            return nid

    for sel in _SELECTORS:
        try:
            result = _cdp_call(ws, "DOM.querySelector", {
                "nodeId": doc_node_id,
                "selector": sel,
            })
            nid = (result or {}).get("nodeId", 0)
            if nid and nid != 0:
                return nid
        except RuntimeError:
            continue

    try:
        result = _cdp_call(ws, "Runtime.evaluate", {
            "expression": """
                (() => {
                    const all = document.querySelectorAll('input[type=file]');
                    if (all.length === 0) return null;
                    const visible = Array.from(all).find(el =>
                        el.offsetParent !== null);
                    return (visible || all[0]).__cdp_node_id || null;
                })()
            """,
        })
        if result and result.get("result", {}).get("value"):
            resolve = _cdp_call(ws, "DOM.requestNode", {
                "objectId": result["result"].get("objectId", ""),
            })
            return (resolve or {}).get("nodeId")
    except RuntimeError:
        pass

    return None


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--find":
        port = find_hermes_port()
        if port:
            print(port)
        else:
            print("9515", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(1)

    file_path = os.path.abspath(sys.argv[1])
    css_selector = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else "input[type=file]"
    port = int(sys.argv[3]) if len(sys.argv) > 3 else None

    try:
        result = upload_file(file_path, css_selector, port)
        print(result)
    except Exception as e:
        print(f"CDP_UPLOAD_ERROR: {e}", file=sys.stderr)
        sys.exit(1)
