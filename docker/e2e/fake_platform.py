from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TSEC_FLAGS: set[str] = set()
TSEC_ACTIVE = False
TSEC_CLOSES = 0
TSEC_HINTS = 0
TSEC_CODE = "e2e-pentest-multi"
TSEC_ANSWERS = ("flag{e2e-tsec-one}", "flag{e2e-tsec-two}")


CHALLENGE = {
    "id": "e2e-web-1",
    "name": "Deterministic E2E Web",
    "category": "web",
    "description": "The deterministic acceptance flag is flag{e2e-deterministic}.",
    "files": [],
    "score": 100,
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        global TSEC_HINTS
        path = urlsplit(self.path).path
        if path == "/e2e/tsecbench/state":
            self._send(200, {"active": TSEC_ACTIVE, "correct_flags": len(TSEC_FLAGS), "close_calls": TSEC_CLOSES, "hint_calls": TSEC_HINTS})
            return
        if path.startswith("/openapi/v1/challenges"):
            if self.headers.get("BENCHMARK_TOKEN") != "fixture-benchmark-token":
                self._send(404, {"code": "task_not_found", "message": "fixture task not found"})
            elif path.endswith("/hint"):
                TSEC_HINTS += 1
                self._send(200, {"hint": "Score-reducing fixture hint"})
            else:
                self._send(200, [{"unique_code": TSEC_CODE, "description": "Deterministic two-flag pentest", "difficulty": "easy", "level": 1,
                                  "total_score": 100, "flag_count": 2, "correct_flag_count": len(TSEC_FLAGS), "is_completed": len(TSEC_FLAGS) == 2,
                                  "container_status": "available" if TSEC_ACTIVE else "stopped", "container_addr": ["e2e-platform:8080"] if TSEC_ACTIVE else []}])
            return
        if self.path == "/api/challenges":
            self._send(200, {"data": [CHALLENGE]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        global TSEC_ACTIVE, TSEC_CLOSES, TSEC_HINTS
        parsed = urlsplit(self.path)
        if parsed.path == "/e2e/tsecbench/reset":
            TSEC_FLAGS.clear()
            TSEC_ACTIVE = False
            TSEC_CLOSES = TSEC_HINTS = 0
            self._send(200, {"ok": True})
            return
        if parsed.path.startswith("/openapi/v1/challenges/"):
            if self.headers.get("BENCHMARK_TOKEN") != "fixture-benchmark-token":
                self._send(404, {"code": "task_not_found", "message": "fixture task not found"})
                return
            if parsed.path.endswith("/submit"):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                flag = body.get("flag")
                if body.get("unique_code") != TSEC_CODE:
                    self._send(404, {"code": "challenge_not_found", "message": "wrong unique_code"})
                elif flag in TSEC_FLAGS:
                    self._send(409, {"code": "duplicate", "message": "flag already counted"})
                else:
                    correct = flag in TSEC_ANSWERS
                    if correct:
                        TSEC_FLAGS.add(flag)
                    self._send(200, {"correct": correct, "awarded": 50 if correct else 0, "cumulative_score": 50 * len(TSEC_FLAGS),
                                     "correct_flag_count": len(TSEC_FLAGS), "total_flag_count": 2, "matched_flag_index": TSEC_ANSWERS.index(flag) if correct else None})
                return
            if parse_qs(parsed.query).get("unique_code") != [TSEC_CODE]:
                self._send(404, {"code": "challenge_not_found", "message": "missing unique_code query"})
            elif parsed.path.endswith("/start"):
                TSEC_ACTIVE = True
                self._send(200, {"unique_code": TSEC_CODE, "container_addr": ["e2e-platform:8080"]})
            elif parsed.path.endswith("/close"):
                TSEC_ACTIVE = False
                TSEC_CLOSES += 1
                self._send(200, {"unique_code": TSEC_CODE, "closed": True})
            else:
                self._send(404, {"code": "challenge_not_found", "message": "unknown fixture path"})
            return
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
