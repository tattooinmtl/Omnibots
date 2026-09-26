"""A real local HTTP server that speaks the OpenAI streaming protocol, for
testing the provider layer end to end (real sockets, real SSE parsing) without
spending tokens. Each fake provider lives under /<name>/v1 and replays a
script of responses; it also records requests and peak concurrency."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def sse(text: str = "", *, reasoning: str = "", tool_calls: list | None = None, usage: dict | None = None,
        finish: str = "stop", split: int = 3) -> dict[str, Any]:
    """A streamed completion. `text` is sent in `split`-char deltas."""
    return {"kind": "stream", "text": text, "reasoning": reasoning, "tool_calls": tool_calls or [], "usage": usage,
            "finish": finish, "split": split}


def status(code: int, body: dict | None = None, headers: dict | None = None, delay: float = 0) -> dict[str, Any]:
    return {"kind": "status", "code": code, "body": body or {"error": {"message": f"mock {code}"}}, "headers": headers or {}, "delay": delay}


class MockProviders:
    def __init__(self):
        self.scripts: dict[str, list[dict]] = {}
        self.default: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.active = 0
        self.peak: dict[str, int] = {}
        self._active_by: dict[str, int] = {}
        self.lock = threading.Lock()
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                name = self.path.strip("/").split("/")[0]
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                with mock.lock:
                    mock.requests.append({"provider": name, "path": self.path, "body": body, "auth": self.headers.get("Authorization")})
                    queue = mock.scripts.get(name) or []
                    step = queue.pop(0) if queue else mock.default.get(name, sse("ok"))
                    if callable(step):                  # a scripted reply that reads the request (e.g. ids from tool results)
                        step = step(body)
                    mock._active_by[name] = mock._active_by.get(name, 0) + 1
                    mock.peak[name] = max(mock.peak.get(name, 0), mock._active_by[name])
                try:
                    self._respond(step)
                finally:
                    with mock.lock:
                        mock._active_by[name] -= 1

            def _respond(self, step):
                if step["kind"] == "status":
                    time.sleep(step.get("delay", 0))
                    data = json.dumps(step["body"]).encode()
                    self.send_response(step["code"])
                    for k, v in step["headers"].items():
                        self.send_header(k, v)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.send_header("x-ratelimit-remaining-requests", "99")
                self.end_headers()

                def emit(obj):
                    self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
                    self.wfile.flush()

                if step.get("hold"):
                    time.sleep(step["hold"])
                for i in range(0, len(step["reasoning"]), step["split"]):
                    emit({"choices": [{"index": 0, "delta": {"reasoning_content": step["reasoning"][i:i + step["split"]]}}]})
                for i in range(0, len(step["text"]), step["split"]):
                    emit({"choices": [{"index": 0, "delta": {"content": step["text"][i:i + step["split"]]}}]})
                for idx, call in enumerate(step["tool_calls"]):
                    args = json.dumps(call["args"])
                    emit({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": idx, "id": f"call_{idx}", "type": "function",
                                                                              "function": {"name": call["name"], "arguments": ""}}]}}]})
                    for j in range(0, len(args), 5):
                        emit({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": idx, "function": {"arguments": args[j:j + 5]}}]}}]})
                emit({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if step["tool_calls"] else step["finish"]}]})
                if step["usage"]:
                    emit({"choices": [], "usage": step["usage"]})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

        class Server(ThreadingHTTPServer):
            # the default listen backlog is 5: tests open up to 9 calls at once, and on a busy machine
            # Windows dropped the extras (WinError 10053), which the router saw as provider failures
            request_queue_size = 64

        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/{name}/v1"

    def script(self, name: str, *steps: dict) -> None:
        with self.lock:
            self.scripts.setdefault(name, []).extend(steps)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
