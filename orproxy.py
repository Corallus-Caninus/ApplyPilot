#!/usr/bin/env python3
"""
Anthropic-to-OpenRouter proxy for Claude Code.

Routes Claude Code requests through OpenRouter using a free model.
Claude Code sends requests with model="claude-sonnet-4-20250514" etc.,
this proxy rewrites the model to a free OpenRouter model and forwards.

Usage:
    export OPENROUTER_API_KEY=sk-or-v1-...
    python3 orproxy.py

Then set:
    ANTHROPIC_BASE_URL=http://localhost:6378
    ANTHROPIC_API_KEY=anything
"""
import json
import os
import sys
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PROXY_PORT", "6378"))
OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")
TARGET_MODEL = os.environ.get("PROXY_TARGET_MODEL", "google/gemma-4-31b-it:free")
SENTINEL_MODEL = os.environ.get("PROXY_SENTINEL_MODEL", "claude-sonnet-4-20250514")

if not OR_KEY:
    print("ERROR: OPENROUTER_API_KEY not set", file=sys.stderr)
    sys.exit(1)


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        try:
            code = self.responses.get(self.status, (str(self.status), ""))[0]
            sys.stderr.write(f"[ORPROXY] {self.command} {self.path} {code}\n")
        except Exception:
            pass

    def _forward(self, body_bytes: bytes, is_stream: bool) -> None:
        req_body = json.loads(body_bytes)
        req_body["model"] = TARGET_MODEL

        data = json.dumps(req_body).encode("utf-8")
        or_url = "https://openrouter.ai/api/v1/chat/completions"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OR_KEY}",
            "HTTP-Referer": "https://applypilot.local",
            "X-Title": "ApplyPilot",
        }

        # Convert Anthropic messages format to OpenAI format
        # This is needed because OpenRouter's /v1/chat/completions is OpenAI format
        system_msg = None
        openai_messages = []
        
        # Extract system message
        if "system" in req_body:
            if isinstance(req_body["system"], list):
                system_msg = " ".join(
                    item.get("text", "") for item in req_body["system"]
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            elif isinstance(req_body["system"], str):
                system_msg = req_body["system"]

        # Convert Anthropic messages to OpenAI format
        for msg in req_body.get("messages", []):
            role = msg["role"]
            content = msg.get("content", "")
            
            if isinstance(content, list):
                # Anthropic content blocks -> extract text
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            text_parts.append(json.dumps(block))
                        elif block.get("type") == "tool_result":
                            text_parts.append(str(block.get("content", "")))
                content = "\n".join(text_parts)
            
            if role == "assistant" and "tool_calls" in msg:
                # Map Anthropic tool_use to OpenAI tool_calls format
                pass
            
            openai_messages.append({"role": role, "content": content})

        if system_msg:
            openai_messages.insert(0, {"role": "system", "content": system_msg})

        # Remove tools for models that don't support them well
        openai_body = {
            "model": TARGET_MODEL,
            "messages": openai_messages,
            "max_tokens": req_body.get("max_tokens", 4096),
            "stream": is_stream,
        }

        openai_data = json.dumps(openai_body).encode("utf-8")

        try:
            proxy_req = urllib.request.Request(
                or_url, data=openai_data, headers=headers, method="POST"
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
                "type": "error", "error": {"type": "proxy_error", "message": str(e)}
            }).encode())
            return

        if is_stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            # Rewrite OpenAI streaming format to Anthropic SSE format
            text = resp_body.decode("utf-8")
            self.wfile.write(self._rewrite_stream(text).encode("utf-8"))
        else:
            # Convert OpenAI response back to Anthropic format
            try:
                openai_resp = json.loads(resp_body)
                anthropic_resp = self._to_anthropic(openai_resp)
                resp_json = json.dumps(anthropic_resp)
            except Exception:
                resp_json = resp_body.decode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp_json.encode("utf-8"))

    def _rewrite_stream(self, text: str) -> str:
        """Rewrite OpenAI SSE stream to look like Anthropic SSE."""
        lines = []
        for line in text.split("\n"):
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    data = json.loads(line[6:])
                    choices = data.get("choices", [{}])
                    delta = choices[0].get("delta", {}) if choices else {}
                    content = delta.get("content", "")
                    if content:
                        anthro = {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": content}
                        }
                        lines.append(f"data: {json.dumps(anthro)}")
                except json.JSONDecodeError:
                    lines.append(line)
            elif line == "data: [DONE]":
                lines.append("data: [DONE]")
            else:
                lines.append(line)
        return "\n".join(lines)

    def _to_anthropic(self, openai_resp: dict) -> dict:
        """Convert OpenAI response to Anthropic format."""
        choices = openai_resp.get("choices", [{}])
        choice = choices[0] if choices else {}
        message = choice.get("message", {})
        content_text = message.get("content", "") or ""
        
        return {
            "id": openai_resp.get("id", "msg_proxy"),
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": content_text}],
            "model": SENTINEL_MODEL,
            "stop_reason": choice.get("finish_reason", "end_turn"),
            "usage": {
                "input_tokens": openai_resp.get("usage", {}).get("prompt_tokens", 0),
                "output_tokens": openai_resp.get("usage", {}).get("completion_tokens", 0),
            }
        }

    def do_GET(self):
        if self.path == "/v1/models":
            models_data = {"data": [{"id": SENTINEL_MODEL, "object": "model", "created": 1700000000, "owned_by": "anthropic"}]}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(models_data).encode())
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path.startswith("/v1/messages"):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
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
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()


def main():
    port = PORT
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    server = HTTPServer(("127.0.0.1", port), ProxyHandler)
    print(f"[ORPROXY] Listening on http://127.0.0.1:{port}", flush=True)
    print(f"[ORPROXY] Routing: {SENTINEL_MODEL} -> {TARGET_MODEL} @ OpenRouter", flush=True)
    print(f"[ORPROXY] Set ANTHROPIC_BASE_URL=http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[ORPROXY] Shutting down", flush=True)
        server.server_close()


if __name__ == "__main__":
    main()
