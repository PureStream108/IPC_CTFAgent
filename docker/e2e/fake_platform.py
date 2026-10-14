from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


CHALLENGE = {
    "id": "e2e-web-1",
    "name": "Deterministic E2E Web",
    "category": "web",
    "description": "The deterministic acceptance flag is flag{e2e-deterministic}.",
    "files": [],
    "score": 100,
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/challenges":
            self._send(200, {"data": [CHALLENGE]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/submit":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            verdict = "correct" if payload.get("flag") == "flag{e2e-deterministic}" else "wrong"
            self._send(200, {"verdict": verdict, "submission_id": "e2e-submission-1"})
            return
        self._send(404, {"error": "not found"})

    def log_message(self, *_args) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
