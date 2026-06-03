#!/usr/bin/env python3
"""
Anthropic-to-OpenCode proxy for Claude Code.

Runs a local HTTP server that mimics the Anthropic Messages API.
Claude Code sends requests with model="claude-sonnet-4-..." etc.,
this proxy rewrites the model to "deepseek-v4-flash" and forwards
to the opencode.ai endpoint, then translates the response back.

Usage:
    python3 proxy.py                    # starts on port 6379
    python3 proxy.py --port 8080        # custom port

Then set:
    ANTHROPIC_BASE_URL=http://localhost:6379
    ANTHROPIC_API_KEY=your-opencode-key
"""
import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from http import HTTPStatus
import urllib.request
import urllib.error

PROXY_PORT = int(os.environ.get("PROXY_PORT", "6379"))
OPENCODE_BASE = os.environ.get("OPENCODE_BASE", "https://opencode.ai/zen/v1")
OPENCODE_KEY = os.environ.get("OPENCODE_API_KEY", "")
TARGET_MODEL = os.environ.get("PROXY_TARGET_MODEL", "deepseek-v4-flash")
SENTINEL_MODEL = os.environ.get("PROXY_SENTINEL_MODEL", "claude-sonnet-4-20250514")

if not OPENCODE_KEY:
    print("ERROR: OPENCODE_API_KEY not set", file=sys.stderr)
    sys.exit(1)


class ProxyHandler(BaseHTTPRequestHandler):
    """Handles Anthropic API requests and proxies them to opencode.ai."""

    def log_message(self, fmt, *args):
        """Quiet logging."""
        try:
            code = self.responses.get(self.status, (str(self.status), ""))[0]
            sys.stderr.write(f"[PROXY] {self.command} {self.path} {code}\n")
        except Exception:
            pass

    def _forward(self, body_bytes: bytes, is_stream: bool) -> None:
        """Forward a request to opencode.ai and relay the response."""
        req_body = json.loads(body_bytes)
        orig_model = req_body.get("model", "unknown")

        # Rewrite the model name
        req_body["model"] = TARGET_MODEL

        data = json.dumps(req_body).encode("utf-8")
        opencode_url = f"{OPENCODE_BASE}/messages"

        headers = {
            "Content-Type": "application/json",
            "x-api-key": OPENCODE_KEY,
            "anthropic-version": "2023-06-01",
            "User-Agent": "curl/8.7.1",
        }

        try:
            proxy_req = urllib.request.Request(
                opencode_url, data=data, headers=headers, method="POST"
            )
            with urllib.request.urlopen(proxy_req, timeout=120) as resp:
                resp_body = resp.read()
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(e.read())
            return
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "type": "error",
                "error": {"type": "proxy_error", "message": str(e)}
            }).encode())
            return

        text = resp_body.decode("utf-8")
        # Rewrite model name in response
        text = text.replace(f'"model":"{TARGET_MODEL}"',
                            f'"model":"{SENTINEL_MODEL}"')
        text = text.replace(f'"model": "{TARGET_MODEL}"',
                            f'"model": "{SENTINEL_MODEL}"')

        if is_stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(text.encode("utf-8"))
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(text.encode("utf-8"))

    def do_GET(self):
        """Handle GET requests — model list for discovery."""
        sys.stderr.write(f"[PROXY] GET {self.path}\n")
        if self.path == "/v1/models":
            models_data = {
                "data": [
                    {
                        "id": SENTINEL_MODEL,
                        "object": "model",
                        "created": 1700000000,
                        "owned_by": "anthropic",
                    }
                ]
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(models_data).encode())
        else:
            self.send_error(404)

    def do_POST(self):
        """Handle POST requests — proxy to opencode."""
        sys.stderr.write(f"[PROXY] POST {self.path}\n")
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if body:
            sys.stderr.write(f"[PROXY] body: {body[:200]}\n")

        if self.path.startswith("/v1/messages"):
            is_stream = False
            if body:
                try:
                    req = json.loads(body)
                    is_stream = req.get("stream", False)
                except json.JSONDecodeError:
                    pass
            self._forward(body, is_stream)
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        """CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()


def main():
    port = PROXY_PORT
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    server = HTTPServer(("127.0.0.1", port), ProxyHandler)
    print(f"[PROXY] Listening on http://127.0.0.1:{port}", flush=True)
    print(f"[PROXY] Routing: {SENTINEL_MODEL} -> {TARGET_MODEL} @ {OPENCODE_BASE}", flush=True)
    print(f"[PROXY] Set ANTHROPIC_BASE_URL=http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[PROXY] Shutting down", flush=True)
        server.server_close()


if __name__ == "__main__":
    main()
