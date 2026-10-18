from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import sync_playwright

_recovery_logger = logging.getLogger(__name__)

def check_pentest_workspace(page, base: str, artifact: Path) -> dict:
    page.get_by_role("button", name="Pentest", exact=True).first.click()
    page.get_by_role("button", name="New Project", exact=True).click()
    form = page.locator('[x-show="showNewProject"]')
    form.locator('[x-model="np.title"]').fill("Deterministic Pentest Assessment")
    form.locator('[x-model="np.origin"]').fill("https://assessment.example.test")
    form.locator('[x-model="np.goal"]').fill("Verify the fixture access boundary")
    form.locator('[x-model="pentestConstraints"]').fill("Only the deterministic fixture")
    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/projects")) as created:
        form.get_by_role("button", name="Create", exact=True).click()
    response = created.value
    assert response.ok, response.text()
    project_id = response.json()["project"]["id"]
    page.wait_for_function("pid => window._ipc.selectedId === pid && window._ipc.pentestBoard !== null", arg=project_id)

    def action(operation, data):
        result = page.request.post(f"{base}/projects/{project_id}/pentest/actions", data={"operation": operation, "data": data})
        assert result.ok, result.text()
        return result.json()

    domain = action("register_asset", {"kind": "domain", "value": "assessment.example.test"})
    app = action("register_asset", {"kind": "app", "value": "https://assessment.example.test", "parent_id": domain["id"]})
    upload = page.request.post(f"{base}/projects/{project_id}/attachments", multipart={"file": {
        "name": "access-evidence.txt", "mimeType": "text/plain", "buffer": b"Deterministic access boundary comparison: verified\n",
    }})
    assert upload.ok, upload.text()
    evidence = action("record_evidence", {"summary": "Recorded access boundary comparison", "path": "attachments/access-evidence.txt", "command": "fixture access comparison"})
    finding = action("report_finding", {"title": "Fixture access boundary finding", "asset_id": app["id"], "vulnerability": "access_control",
        "description": "Fixture comparison reproduced the boundary condition", "reproduction": "Compare the two deterministic fixture responses",
        "remediation": "Enforce the fixture record ownership check", "evidence_ids": [evidence["id"]]})
    action("verify_finding", {"finding_id": finding["id"], "outcome": "confirmed", "evidence_ids": [evidence["id"]], "note": "Independent fixture review"})
    action("record_coverage", {"asset_id": app["id"], "test": "Access boundary", "state": "tested", "evidence_ids": [evidence["id"]]})
    page.evaluate("async () => { await window._ipc.loadPentest(window._ipc.selectedId); window._ipc.renderPentest(true); }")
    page.wait_for_function("window._ipc.cy.nodes().some(n => n.data('kind') === 'finding')")
    page.screenshot(path=str(artifact / "pentest-exploration.png"), full_page=True)
    page.get_by_role("button", name="Asset coverage", exact=True).click()
    page.wait_for_function("window._ipc.pentestGraph?.view === 'assets' && window._ipc.cy.nodes().length === 2")
    assert page.evaluate("window._ipc.pentestGraph.coverage.tested") == 1
    page.evaluate("() => { const node=window._ipc.cy.nodes().filter(n=>n.data('kind')==='domain'); node.emit('tap'); }")
    page.get_by_role("button", name="Fold / expand", exact=True).click()
    page.wait_for_function("window._ipc.cy.nodes().length === 1")
    page.get_by_role("button", name="Fold / expand", exact=True).click()
    page.wait_for_function("window._ipc.cy.nodes().length === 2")
    page.screenshot(path=str(artifact / "pentest-assets.png"), full_page=True)
    board = page.request.get(f"{base}/projects/{project_id}/pentest").json()
    goal = next(item for item in board["records"] if item["kind"] == "goal")
    action("prove_goal", {"goal_id": goal["id"], "evidence_ids": [evidence["id"]], "finding_ids": [finding["id"]], "note": "The fixture objective is proven"})
    project = page.request.get(f"{base}/projects/{project_id}").json()["project"]
    assert project["status"] == "completed" and not project["flag"]
    page.reload(wait_until="networkidle")
    page.wait_for_function("window._ipc?.workspaceMode === 'pentest' && window._ipc.pentestBoard !== null")
    assert page.evaluate("window._ipc.filteredProjects().every(p=>p.category==='pentest')")
    page.get_by_role("button", name="CTF", exact=True).click()
    assert page.evaluate("window._ipc.filteredProjects().every(p=>p.category!=='pentest')")
    return {"project_id": project_id, "status": "completed", "finding_id": finding["id"], "evidence_sha256": evidence["data"]["sha256"]}


def check_tsecbench_multiflag(page, base: str, artifact: Path) -> dict:
    fixture = "http://e2e-platform:8080"
    assert page.request.post(f"{fixture}/e2e/tsecbench/reset").ok
    competition = page.locator('[x-show="showCompetition"]')
    competition.get_by_role("button", name="New run", exact=True).click()
    competition.get_by_role("button", name="New TSecBench", exact=True).click()
    form = competition.locator('[x-show="showTsecbenchSetup"]')
    form.locator('[x-model="tsecbenchDraft.base_url"]').fill(fixture)
    form.locator('[x-model="tsecbenchDraft.name"]').fill("Deterministic TSecBench " + uuid4().hex[:8])
    form.locator('[x-model="tsecbenchToken"]').fill("fixture-benchmark-token")
    form.locator('[x-model="tsecbenchPentest"]').check()
    form.locator('[x-model="tsecbenchDraft.allow_private_networks"]').check()
    form.locator('[x-model\\.number="tsecbenchDraft.remote_concurrency"]').fill("1")
    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/api/ops/workflows/tsecbench")) as configured:
        form.get_by_role("button", name="Save workflow", exact=True).click()
    assert configured.value.ok, configured.value.text()
    workflow_id = configured.value.json()["id"]
    confirmed = page.request.post(f"{base}/api/ops/workflows/{workflow_id}/confirm", data={"confirmation_phrase": f"CONFIRM WORKFLOW {workflow_id}"})
    assert confirmed.ok, confirmed.text()
    form.get_by_role("button", name="Close", exact=True).click()
    page.evaluate("async id => { await window._ipc.loadOpsWorkflows(); window._ipc.competitionWorkflowId=id; }", workflow_id)
    with page.expect_response(lambda response: response.url.endswith(f"/api/workflows/{workflow_id}/preflight")) as preflight:
        competition.get_by_role("button", name="Preflight", exact=True).click()
    assert preflight.value.ok, preflight.value.text()
    assert preflight.value.json()["network"]["vpn_required"] is True
    with page.expect_response(lambda response: response.url.endswith(f"/api/workflows/{workflow_id}/start")) as started:
        competition.get_by_role("button", name="Start", exact=True).click()
    assert started.value.ok, started.value.text()
    run_id = started.value.json()["run"]["id"]
    partial = False
    snapshot = {}
    for _ in range(240):
        snapshot = page.request.get(f"{base}/api/runs/{run_id}").json()
        challenge = snapshot["challenges"][0]
        progress = challenge["metadata"].get("flag_progress") or {}
        if progress.get("correct_flag_count") == 1:
            assert challenge["state"] != "solved"
            assert any(a.get("released_at") is None for a in challenge.get("assignments", []))
            partial = True
        if snapshot["run"]["status"] == "finished":
            break
        page.wait_for_timeout(250)
    (artifact / "tsecbench-run.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    assert partial, "the fixture never observed partial progress with a live solver"
    assert snapshot["run"]["status"] == "finished", snapshot["run"]
    assert snapshot["challenges"][0]["state"] == "solved"
    project = page.request.get(f"{base}/projects/{snapshot['challenges'][0]['project_id']}").json()["project"]
    assert project["category"] == "pentest" and project["completion_policy"] == "platform_flags"
    state = page.request.get(f"{fixture}/e2e/tsecbench/state").json()
    assert state == {"active": False, "correct_flags": 2, "close_calls": 1, "hint_calls": 0}, state
    result = {"run_id": run_id, "partial_progress_observed": partial, **state}
    (artifact / "tsecbench-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    page.screenshot(path=str(artifact / "tsecbench-completed.png"), full_page=True)
    return result


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
            pentest_result = check_pentest_workspace(page, base, artifact)
            (artifact / "pentest-result.json").write_text(json.dumps(pentest_result, indent=2), encoding="utf-8")
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
            existing_runs = page.request.get(f"{base}/api/runs").json().get("runs", [])
            page.get_by_role("button", name="Competition", exact=True).click()
            competition = page.locator('[x-show="showCompetition"]')
            page.wait_for_function("({id,count}) => window._ipc.opsWorkflows.some(w=>w.id===id) && window._ipc.competitionRuns.length===count && (!count || !!window._ipc.competitionRun)", arg={"id": workflow_id, "count": len(existing_runs)})
            if existing_runs:
                competition.get_by_role("button", name="New run", exact=True).wait_for(state="visible")
                competition.get_by_role("button", name="New run", exact=True).click()
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
            tsecbench = check_tsecbench_multiflag(page, base, artifact)
            result = {"ok": True, "url": page.url, "console": console, "evidence": evidence, "tsecbench": tsecbench}
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
