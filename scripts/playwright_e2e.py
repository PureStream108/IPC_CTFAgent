from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import sync_playwright

_recovery_logger = logging.getLogger(__name__)

def main() -> int:
    base = os.environ.get("IPC_E2E_BASE_URL", "http://127.0.0.1:8000")
    artifact = Path(os.environ.get("IPC_E2E_ARTIFACT_DIR", "data/exports/e2e"))
    artifact.mkdir(parents=True, exist_ok=True)
    console: list[str] = []
    run_id = None
    evidence = {}
    with sync_playwright() as playwright:
        executable_path = os.environ.get("IPC_PLAYWRIGHT_EXECUTABLE")
        launch_options = {"headless": True}
        if executable_path:
            launch_options["executable_path"] = executable_path
        browser = playwright.chromium.launch(**launch_options)
        context = browser.new_context()
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = context.new_page()
        page.on("console", lambda message: console.append(f"{message.type}: {message.text}"))
        try:
            page.goto(base, wait_until="networkidle")
            page.screenshot(path=str(artifact / "initial.png"), full_page=True)
            page.get_by_text("IPC", exact=False).first.wait_for()
            run_token = uuid4().hex[:12]
            workflow = {
                "name": f"Deterministic E2E Competition {run_token}",
                "competition_id": f"e2e-competition-{run_token}",
                "team_id": f"e2e-team-{run_token}",
                "allow_private_networks": True,
                "challenges": {
                    "platform": "http_json",
                    "list_url": "http://e2e-platform:8080/api/challenges",
                    "list_path": "data",
                    "id_field": "id",
                    "title_field": "name",
                    "category_field": "category",
                    "description_field": "description",
                    "attachments_field": "files",
                },
                "submit": {
                    "url": "http://e2e-platform:8080/api/submit",
                    "method": "POST",
                    "json_template": {"challenge_id": "{{external_id}}", "flag": "{{flag}}"},
                    "success_path": "verdict",
                    "success_values": ["correct"],
                    "wrong_values": ["wrong"],
                },
            }
            created = page.request.post(f"{base}/api/ops/workflows", data={"workflow": workflow})
            created_json = created.json()
            workflow_id = created_json["id"]
            confirmed = page.request.post(
                f"{base}/api/ops/workflows/{workflow_id}/confirm",
                data={"confirmation_phrase": f"CONFIRM WORKFLOW {workflow_id}"},
            )
            if not confirmed.ok:
                raise RuntimeError(confirmed.text())
            page.get_by_role("button", name="Competition", exact=True).click()
            competition = page.locator('[x-show="showCompetition"]')
            selector = competition.locator('select[x-model="competitionWorkflowId"]')
            selector.select_option(workflow_id)
            assert selector.locator(f'option[value="{workflow_id}"]').inner_text() == f"{workflow['name']} (confirmed)"
            with page.expect_response(lambda response: response.url.endswith(f"/api/workflows/{workflow_id}/preflight")) as response_info:
                competition.get_by_role("button", name="Preflight", exact=True).click()
            if not response_info.value.ok:
                raise RuntimeError("UI preflight failed")
            with page.expect_response(lambda response: response.url.endswith(f"/api/workflows/{workflow_id}/start")) as response_info:
                competition.get_by_role("button", name="Start", exact=True).click()
            started = response_info.value
            if not started.ok:
                raise RuntimeError(f"run start failed ({started.status}): {started.text()}")
            started_json = started.json()
            run_id = started_json["run"]["id"]
            assignment = None
            run_json = started_json
            for _ in range(60):
                run_response = page.request.get(f"{base}/api/runs/{run_id}")
                run_json = run_response.json()
                assignments = run_json["challenges"][0].get("assignments") or []
                if assignments:
                    assignment = assignments[0]
                    break
                page.wait_for_timeout(500)
            if assignment is None:
                raise RuntimeError("deterministic Member assignment was not created")

            evidence = {}
            for _ in range(120):
                evidence_response = page.request.get(f"{base}/e2e/runs/{run_id}/evidence")
                evidence = evidence_response.json()
                evidence_text = json.dumps(evidence, ensure_ascii=False)
                if "e2e-sandbox-command-ok" in evidence_text and "memory_catalog" in evidence_text:
                    break
                page.wait_for_timeout(500)
            else:
                raise RuntimeError("scripted Member did not complete sandbox and MCP actions")

            for _ in range(60):
                run_json = (page.request.get(f"{base}/api/runs/{run_id}")).json()
                if run_json["challenges"][0]["state"] == "solved":
                    break
                page.wait_for_timeout(500)
            if run_json["challenges"][0]["state"] != "solved":
                raise RuntimeError("scripted Member flag submission did not solve the challenge")

            required_events = {"assignment.started", "submission.verdict", "wp.completed", "run.finished"}
            for _ in range(60):
                evidence = (page.request.get(f"{base}/e2e/runs/{run_id}/evidence")).json()
                event_kinds = {event["kind"] for event in evidence.get("run_events", [])}
                if required_events.issubset(event_kinds):
                    break
                page.wait_for_timeout(500)
            else:
                raise RuntimeError(f"incomplete closeout events: {sorted(event_kinds)}")
            if evidence.get("collaboration_backlog") != 0:
                raise RuntimeError("collaboration backlog was not drained")
            if any(item.get("released_at") is None for item in evidence.get("assignments", [])):
                raise RuntimeError("an assignment remained active after closeout")
            if not evidence.get("wp_jobs") or any(job.get("status") != "done" for job in evidence["wp_jobs"]):
                raise RuntimeError("writeup job did not complete")
            competition.get_by_text("solved", exact=True).first.wait_for(timeout=30_000)
            score = competition.get_by_test_id("competition-score")
            score.wait_for()
            assert score.inner_text() == "100 solved challenge points"
            page.screenshot(path=str(artifact / "completed.png"), full_page=True)
            result = {"ok": True, "url": page.url, "console": console, "evidence": evidence}
        except Exception as exc:
            _recovery_logger.debug('Error handled in main', exc_info=True)
            page.screenshot(path=str(artifact / "failure.png"), full_page=True)
            if run_id:
                evidence = page.request.get(f"{base}/e2e/runs/{run_id}/evidence").json()
            result = {"ok": False, "error": str(exc), "url": page.url, "console": console, "evidence": evidence}
        finally:
            context.tracing.stop(path=str(artifact / "trace.zip"))
            context.close()
            browser.close()
    (artifact / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (artifact / "run-events.json").write_text(json.dumps(evidence.get("run_events", []), indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
