from __future__ import annotations

import os
import json
from collections.abc import Iterator
from typing import Any

import requests


class ClaudeCodeRunnerError(RuntimeError):
    """The Claude Code sidecar could not run or return a usable result."""


class ClaudeCodeRunner:
    """Small client for the isolated Claude Code sidecar.

    The sidecar owns the Claude Code process and its native agent loop.  The
    IPC app only sends a prompt plus the already configured provider settings;
    API keys never enter the conversation history or the browser response.
    """

    def __init__(self) -> None:
        self.base_url = os.environ.get("IPC_CLAUDE_RUNNER_URL", "").strip().rstrip("/")
        self.token = os.environ.get("IPC_RUNNER_TOKEN", "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "configured": False, "error": "Claude Code runner is not configured"}
        try:
            response = requests.get(
                f"{self.base_url}/health",
                headers=self._headers(),
                timeout=(5, 15),
            )
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {"ok": False, "error": "invalid runner health response"}
        except (requests.RequestException, ValueError) as exc:
            return {"ok": False, "configured": True, "error": str(exc)}

    def run(
        self,
        *,
        prompt: str,
        api_key: str,
        base_url: str,
        model: str,
        session_id: str | None = None,
        resume_session_id: str | None = None,
        run_id: str | None = None,
        max_turns: int = 32,
        timeout: int = 900,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise ClaudeCodeRunnerError("Claude Code runner is not configured")
        if not api_key.strip():
            raise ClaudeCodeRunnerError("Claude Code provider key is empty")
        payload = {
            "prompt": prompt,
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
            "session_id": session_id,
            "resume_session_id": resume_session_id,
            "run_id": run_id,
            "max_turns": max(1, min(int(max_turns), 100)),
            "timeout_ms": max(60, min(int(timeout), 1800)) * 1000,
        }
        try:
            response = requests.post(
                f"{self.base_url}/run",
                headers=self._headers(),
                json=payload,
                timeout=(10, max(60, timeout)),
            )
        except requests.RequestException as exc:
            raise ClaudeCodeRunnerError(f"Claude Code runner request failed: {exc}") from exc
        if response.status_code >= 400:
            detail = _safe_error_detail(response)
            raise ClaudeCodeRunnerError(
                f"Claude Code runner returned HTTP {response.status_code}: {detail}"
            )
        try:
            result = response.json()
        except ValueError as exc:
            raise ClaudeCodeRunnerError("Claude Code runner returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise ClaudeCodeRunnerError("Claude Code runner returned a non-object result")
        return result

    def stream(
        self,
        *,
        prompt: str,
        api_key: str,
        base_url: str,
        model: str,
        session_id: str | None = None,
        resume_session_id: str | None = None,
        run_id: str | None = None,
        max_turns: int = 32,
        timeout: int = 900,
    ) -> Iterator[dict[str, Any]]:
        """Yield NDJSON events from Claude Code while it is running."""
        if not self.enabled:
            raise ClaudeCodeRunnerError("Claude Code runner is not configured")
        if not api_key.strip():
            raise ClaudeCodeRunnerError("Claude Code provider key is empty")
        payload = {
            "prompt": prompt,
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
            "session_id": session_id,
            "resume_session_id": resume_session_id,
            "run_id": run_id,
            "max_turns": max(1, min(int(max_turns), 100)),
            "timeout_ms": max(60, min(int(timeout), 1800)) * 1000,
        }
        try:
            response = requests.post(
                f"{self.base_url}/run/stream",
                headers={**self._headers(), "Accept": "application/x-ndjson"},
                json=payload,
                stream=True,
                timeout=(10, max(60, timeout)),
            )
        except requests.RequestException as exc:
            raise ClaudeCodeRunnerError(f"Claude Code runner request failed: {exc}") from exc
        try:
            if response.status_code >= 400:
                detail = _safe_error_detail(response)
                raise ClaudeCodeRunnerError(
                    f"Claude Code runner returned HTTP {response.status_code}: {detail}"
                )
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    yield {"type": "stderr", "text": str(line)[:12_000]}
                    continue
                if isinstance(value, dict):
                    yield value
                else:
                    yield {"type": "event", "event": {"type": "runner", "text": str(value)}}
        except requests.RequestException as exc:
            raise ClaudeCodeRunnerError(f"Claude Code runner stream failed: {exc}") from exc
        finally:
            response.close()

    def cancel(self, run_id: str) -> dict[str, Any]:
        """Request termination of one running Claude Code child process."""

        if not self.enabled:
            raise ClaudeCodeRunnerError("Claude Code runner is not configured")
        try:
            response = requests.post(
                f"{self.base_url}/runs/cancel",
                headers=self._headers(),
                json={"run_id": run_id},
                timeout=(5, 15),
            )
        except requests.RequestException as exc:
            raise ClaudeCodeRunnerError(f"Claude Code cancellation request failed: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ClaudeCodeRunnerError("Claude Code runner returned invalid cancellation JSON") from exc
        if not isinstance(payload, dict):
            raise ClaudeCodeRunnerError("Claude Code runner returned a non-object cancellation response")
        if response.status_code not in (200, 202, 404):
            detail = str(payload.get("error") or payload.get("detail") or "unknown error")
            raise ClaudeCodeRunnerError(
                f"Claude Code runner cancellation returned HTTP {response.status_code}: {detail[:1000]}"
            )
        return payload

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-IPC-Runner-Token"] = self.token
        return headers


def _safe_error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = payload.get("error") or payload.get("detail")
            if detail:
                return str(detail)[:1000]
    except ValueError:
        pass
    return response.text[:1000] or response.reason or "unknown runner error"
