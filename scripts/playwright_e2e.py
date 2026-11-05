from __future__ import annotations

import base64
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
    ip = action("register_asset", {"kind": "ip", "value": "203.0.113.10", "parent_id": domain["id"]})
    service = action("register_asset", {"kind": "service", "value": "HTTPS / 443", "parent_id": ip["id"]})
    app = action("register_asset", {"kind": "app", "value": "https://assessment.example.test", "parent_id": service["id"]})
    endpoint = action("register_asset", {"kind": "endpoint", "value": "https://assessment.example.test/session", "parent_id": app["id"]})
    for name, content in [
        ("access-evidence.txt", b"Deterministic access boundary comparison: verified\n"),
        ("endpoint-inventory.txt", b"Session endpoint and cookie scope captured by the fixture\n"),
    ]:
        upload = page.request.post(f"{base}/projects/{project_id}/attachments", multipart={"file": {
            "name": name, "mimeType": "text/plain", "buffer": content,
        }})
        assert upload.ok, upload.text()
    evidence = action("record_evidence", {"summary": "Recorded access boundary comparison", "path": "attachments/access-evidence.txt", "command": "fixture access comparison"})
    endpoint_evidence = action("record_evidence", {"summary": "Recorded session endpoint metadata", "path": "attachments/endpoint-inventory.txt", "command": "fixture endpoint inventory"})
    finding = action("report_finding", {"title": "Fixture access boundary finding", "asset_id": app["id"], "vulnerability": "access_control",
        "description": "Fixture comparison reproduced the boundary condition", "reproduction": "Compare the two deterministic fixture responses",
        "remediation": "Enforce the fixture record ownership check", "evidence_ids": [evidence["id"]]})
    action("report_finding", {"title": "Session endpoint review", "asset_id": endpoint["id"], "vulnerability": "session_management", "severity": "low",
        "description": "Session cookie scope differs between fixture accounts", "reproduction": "Compare fixture account A and B session metadata",
        "remediation": "Bind fixture session records to their owners", "evidence_ids": [endpoint_evidence["id"]]})
    action("verify_finding", {"finding_id": finding["id"], "outcome": "confirmed", "evidence_ids": [evidence["id"]], "note": "Independent fixture review"})
    action("record_coverage", {"asset_id": app["id"], "test": "Access boundary", "state": "tested", "evidence_ids": [evidence["id"]]})
    action("record_coverage", {"asset_id": endpoint["id"], "test": "Session metadata", "state": "tested", "evidence_ids": [endpoint_evidence["id"]]})
    hint = page.request.post(f"{base}/projects/{project_id}/hints", data={"content": "完成访问控制复核后，继续检查会话端点。", "creator": "operator"})
    assert hint.ok, hint.text()
    board = page.request.get(f"{base}/projects/{project_id}/pentest").json()
    goal = next(item for item in board["records"] if item["kind"] == "goal")
    action("prove_goal", {"goal_id": goal["id"], "evidence_ids": [evidence["id"]], "finding_ids": [finding["id"]], "note": "The fixture objective is proven"})
    page.evaluate("async () => { await window._ipc.loadPentest(window._ipc.selectedId); window._ipc.renderPentest(true); }")
    page.wait_for_function("window._ipc.cy?.nodes().some(n => n.data('kind') === 'finding')")
    page.wait_for_function("window._ipc.cy?.scratch('pentestLayouting') === false")
    assert page.evaluate("window._ipc.cy.nodes().map(n=>n.data('kind')).sort()") == ["app", "domain", "endpoint", "evidence", "evidence", "fact", "finding", "finding", "hint", "ip", "service"]
    assert page.evaluate("window._ipc.cy.edges().map(e=>e.data('curveStyle')).sort()") == ["straight", "straight", "taxi", "taxi", "taxi", "taxi", "unbundled-bezier", "unbundled-bezier", "unbundled-bezier", "unbundled-bezier"]
    assert page.evaluate("window._ipc.pentestGraphStats()") == {"assets": 5, "clues": 6, "links": 10}
    assert page.evaluate("window._ipc.cy.edges().some(e=>e.data('rel')==='affects' && e.source().data('id')==='" + finding["id"] + "' && e.target().data('id')==='" + app["id"] + "')")
    before = page.evaluate("""() => {
      const cy=window._ipc.cy, node=cy.nodes().filter(n=>n.data('kind')==='finding');
      node.emit('tap'); cy.zoom(1.12); cy.pan({x:37,y:41});
      return {position:{...node.position()},pan:{...cy.pan()},zoom:cy.zoom(),selected:window._ipc.pentestSelected.data.id};
    }""")
    page.evaluate("async () => { await window._ipc.loadPentest(window._ipc.selectedId); }")
    page.wait_for_timeout(150)
    after = page.evaluate("""() => {
      const cy=window._ipc.cy, node=cy.nodes().filter(n=>n.data('kind')==='finding');
      return {position:{...node.position()},pan:{...cy.pan()},zoom:cy.zoom(),selected:window._ipc.pentestSelected.data.id};
    }""")
    assert after == before, {"before": before, "after": after}
    complete_nodes = page.evaluate("window._ipc.cy.nodes().length")
    complete_edges = page.evaluate("window._ipc.cy.edges().length")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.evaluate("""() => {
      window._ipc.sidebarOpen=true; window._ipc.pentestSelected=null;
      window._ipc.cy.elements().removeClass('dim chosen connected hovered');
      window._ipc.cy.resize(); window._ipc.fit();
    }""")
    page.wait_for_timeout(350)
    page.screenshot(path=str(artifact / "pentest-exploration.png"), full_page=True)
    full_graph = page.evaluate("window._ipc.cy.png({full:true,scale:1})")
    (artifact / "pentest-graph-full.png").write_bytes(base64.b64decode(full_graph.split(",", 1)[1]))
    for width, height in [(1634, 1288), (1440, 900), (768, 1024), (390, 844)]:
        page.set_viewport_size({"width": width, "height": height})
        page.evaluate("""() => {
          window._ipc.sidebarOpen=innerWidth>=1000; window._ipc.pentestInspectorOpen=false;
          window._ipc.cy.resize(); window._ipc.fit();
        }""")
        page.wait_for_timeout(350)
        bounds = page.evaluate("""() => {
          const dock=document.querySelector('[data-testid=pentest-dock]').getBoundingClientRect();
          return {width:innerWidth,scrollWidth:document.documentElement.scrollWidth,left:dock.left,right:dock.right};
        }""")
        assert bounds["scrollWidth"] <= width, bounds
        assert bounds["left"] >= -1 and bounds["right"] <= width + 1, bounds
        graph_bounds = page.evaluate("""() => {
          const cy=window._ipc.cy, canvas=document.querySelector('#cy').getBoundingClientRect();
          return {zoom:cy.zoom(),pan:cy.pan(),canvas:{x:canvas.x,y:canvas.y,width:canvas.width,height:canvas.height},extent:cy.extent(),
            nodes:cy.nodes().map(n=>({id:n.id(),position:n.position(),rendered:n.renderedPosition(),box:n.boundingBox()}))};
        }""")
        print(f"pentest viewport {width}x{height}:", graph_bounds, flush=True)
        page.screenshot(path=str(artifact / f"pentest-{width}x{height}.png"), full_page=True)
    page.get_by_role("button", name="记录", exact=True).click()
    page.wait_for_function("document.querySelector('[data-testid=pentest-inspector]').classList.contains('inspector-open')")
    page.get_by_role("button", name="关闭渗透详情", exact=True).click()
    page.set_viewport_size({"width": 1440, "height": 900})
    page.evaluate("window._ipc.sidebarOpen=true")
    page.get_by_role("tab", name="资产覆盖", exact=True).click()
    page.wait_for_function("window._ipc.pentestGraph?.view === 'assets' && window._ipc.cy && window._ipc.cy.nodes().length === 5")
    assert page.evaluate("window._ipc.pentestGraph.coverage.tested") == 2
    page.evaluate("() => { const node=window._ipc.cy.nodes().filter(n=>n.data('kind')==='domain'); node.emit('tap'); }")
    page.get_by_role("button", name="折叠 / 展开", exact=True).click()
    page.wait_for_function("window._ipc.cy.nodes().length === 1")
    page.get_by_role("button", name="折叠 / 展开", exact=True).click()
    page.wait_for_function("window._ipc.cy.nodes().length === 5")
    page.evaluate("""() => {
      window._ipc.pentestSelected=null; window._ipc.cy.elements().removeClass('dim chosen connected hovered');
      window._ipc.cy.resize(); window._ipc.fit();
    }""")
    page.wait_for_timeout(350)
    page.screenshot(path=str(artifact / "pentest-assets.png"), full_page=True)
    project = page.request.get(f"{base}/projects/{project_id}").json()["project"]
    assert project["status"] == "completed" and not project["flag"]
    page.reload(wait_until="networkidle")
    page.wait_for_function("window._ipc?.workspaceMode === 'pentest' && window._ipc.pentestBoard !== null")
    assert page.evaluate("window._ipc.filteredProjects().every(p=>p.category==='pentest')")
    page.get_by_role("button", name="CTF", exact=True).click()
    assert page.evaluate("window._ipc.filteredProjects().every(p=>p.category!=='pentest')")
    return {"project_id": project_id, "status": "completed", "node_count": complete_nodes, "edge_count": complete_edges, "finding_id": finding["id"], "evidence_sha256": evidence["data"]["sha256"]}


def check_tsecbench_multiflag(page, base: str, artifact: Path) -> dict:
    fixture = "http://e2e-platform:8080"
    assert page.request.post(f"{fixture}/e2e/tsecbench/reset").ok
    configured = page.request.post(
        f"{base}/api/ops/workflows/tsecbench",
        data={
            "base_url": fixture,
            "name": "Deterministic TSecBench " + uuid4().hex[:8],
            "remote_concurrency": 1,
            "allow_private_networks": True,
            "secrets": {"benchmark_token": "fixture-benchmark-token"},
        },
    )
    assert configured.ok, configured.text()
    workflow_id = configured.json()["id"]
    confirmed = page.request.post(f"{base}/api/ops/workflows/{workflow_id}/confirm", data={"confirmation_phrase": f"CONFIRM WORKFLOW {workflow_id}"})
    assert confirmed.ok, confirmed.text()
    # Preflight, Start and every later control live on the run API; the removed
    # standalone Competition console is not part of the UI any more.
    preflight = page.request.post(f"{base}/api/workflows/{workflow_id}/preflight")
    assert preflight.ok, preflight.text()
    assert preflight.json()["network"]["vpn_required"] is True
    started = page.request.post(
        f"{base}/api/workflows/{workflow_id}/start",
        data={"idempotency_key": f"e2e-tsecbench-{uuid4().hex[:12]}"},
    )
    assert started.ok, started.text()
    run_id = started.json()["run"]["id"]
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


def check_project_wp(page, base: str, artifact: Path, project_id: str) -> dict:
    first = "# E2E-WP-FIRST\n\n" + "\n".join(f"- line {i}" for i in range(120))
    second = "# E2E-WP-SECOND\n\n" + "\n".join(f"- changed {i}" for i in range(120))
    response = page.request.post(f"{base}/e2e/projects/{project_id}/wp", data=json.dumps({"content": first}), headers={"Content-Type": "application/json"})
    assert response.ok, response.text()

    page.evaluate("window._ipc.showRuns = false; window._ipc.showGlobalWP = false")
    page.evaluate("async pid => { await window._ipc.openProject(pid); }", project_id)
    page.wait_for_function("pid => window._ipc.current?.project?.id === pid", arg=project_id)
    page.locator('button:has-text("WP")').last.click()
    page.wait_for_function("window._ipc.showWP && window._ipc.wpSource === 'project'")
    # The viewer renders asynchronously; measuring scroll before the first
    # document lands would always read 0 and prove nothing.
    page.wait_for_function(
        "() => (window._ipc.wpText || '').includes('E2E-WP-FIRST')", timeout=15000
    )
    page.wait_for_function(
        "() => { const el = document.querySelector('[x-ref=wpScroll]');"
        " return !!el && el.scrollHeight > el.clientHeight + 40; }",
        timeout=15000,
    )

    pill = page.locator('[x-show="showWP"]').get_by_role("group", name="Writeup 显示模式")
    pill.wait_for()
    pill.get_by_role("button", name="源码", exact=True).click()
    source_text = page.locator('[x-show="showWP"] pre:visible').inner_text()
    assert "E2E-WP-FIRST" in source_text, source_text[:300]
    pill.get_by_role("button", name="预览", exact=True).click()
    page.wait_for_function("document.querySelector('[x-show=showWP] h1')?.textContent.includes('E2E-WP-FIRST')")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")

    page.evaluate("document.querySelector('[x-ref=wpScroll]').scrollTop = 80")
    # A background tick may land between the assignment and this read; retry the
    # assignment until it sticks so the measurement is about preservation, not
    # about who won the race.
    for _ in range(20):
        if page.evaluate("document.querySelector('[x-ref=wpScroll]').scrollTop") >= 60:
            break
        page.evaluate("document.querySelector('[x-ref=wpScroll]').scrollTop = 80")
        page.wait_for_timeout(100)
    print("wp e2e scroll before:", page.evaluate("document.querySelector('[x-ref=wpScroll]').scrollTop"), flush=True)
    response = page.request.post(f"{base}/e2e/projects/{project_id}/wp", data=json.dumps({"content": second}), headers={"Content-Type": "application/json"})
    assert response.ok, response.text()
    page.wait_for_function("window._ipc.wpText.includes('E2E-WP-SECOND')", timeout=5000)
    page.wait_for_function("document.querySelector('[x-show=showWP] h1')?.textContent.includes('E2E-WP-SECOND')")
    page.wait_for_function("document.querySelector('[x-ref=wpScroll]').scrollTop >= 60", timeout=5000)
    scroll_after = page.evaluate("document.querySelector('[x-ref=wpScroll]').scrollTop")
    print("wp e2e scroll after:", scroll_after, flush=True)
    assert scroll_after >= 60, f"scroll position was not preserved: {scroll_after}"
    page.screenshot(path=str(artifact / "wp-live.png"), full_page=True)

    page.locator('[x-show="showWP"]').get_by_role("button", name="Close", exact=True).click()
    page.wait_for_function("!window._ipc.showWP")
    page.evaluate("window._ipc.openGlobalWP()")
    page.wait_for_function("window._ipc.showGlobalWP")
    assert page.locator('[x-show="showGlobalWP"]').get_by_role("group", name="Writeup 显示模式").count() == 0
    assert page.locator('[x-show="showGlobalWP"]').get_by_text("实时", exact=True).count() == 0
    page.evaluate("window._ipc.showGlobalWP = false")
    page.evaluate("window._ipc.openRuns()")
    page.wait_for_function("window._ipc.showRuns")
    return {"project_id": project_id, "pill": True, "live_update": True, "global_untouched": True}


def check_ctf_workspace(page, artifact: Path, project_id: str) -> dict:
    page.evaluate("window._ipc.showRuns = false")
    page.evaluate("async pid => { await window._ipc.openProject(pid); }", project_id)
    page.wait_for_function("window._ipc.workspaceMode === 'ctf' && window._ipc.cy?.nodes().length > 1")
    page.evaluate("void window._ipc.cy.fit(undefined, 64)")
    curve_styles = page.evaluate("window._ipc.cy.edges().map(edge => edge.style('curve-style'))")
    assert "unbundled-bezier" in curve_styles
    start = page.evaluate("""() => {
      const canvas=document.querySelector('#cy').getBoundingClientRect();
      const node=window._ipc.cy.nodes().first();
      const point=node.renderedPosition();
      return {id:node.id(),x:canvas.x+point.x,y:canvas.y+point.y};
    }""")
    page.mouse.move(start["x"], start["y"])
    page.mouse.down()
    page.mouse.move(start["x"] + 110, start["y"] + 55, steps=12)
    page.mouse.up()
    page.wait_for_function("window._ipc.ctfManualPositionCount() > 0")
    before = page.evaluate("""() => {
      const cy=window._ipc.cy,node=cy.nodes().first();
      return {position:{...node.position()},pan:{...cy.pan()},zoom:cy.zoom()};
    }""")
    page.evaluate("window._ipc.relayoutCtf(false, true); window._ipc.renderGraph(false)")
    page.evaluate("""async () => {
      await window._ipc.loadProject(window._ipc.selectedId);
      window._ipc.cy.zoom(1.1); window._ipc.cy.pan({x:17,y:23});
      window._ipc.renderGraph(false);
    }""")
    after = page.evaluate("""() => {
      const cy=window._ipc.cy,node=cy.nodes().first();
      return {position:{...node.position()},pan:{...cy.pan()},zoom:cy.zoom()};
    }""")
    assert after["position"] == before["position"], {"before": before, "after": after}
    assert after["pan"] == {"x": 17, "y": 23} and after["zoom"] == 1.1
    assert page.evaluate("window._ipc.cy.nodes().first().grabbable()")
    page.get_by_test_id("ctf-motion").click()
    page.wait_for_function("document.querySelector('[data-testid=ctf-motion]').getAttribute('aria-pressed') === 'false'")
    def edge_offset():
        return float(page.evaluate("window._ipc.cy.edges(`[etype = 'explore'], [etype = 'orch']`).style('line-dash-offset')"))
    paused_offset = edge_offset()
    page.wait_for_timeout(160)
    assert edge_offset() == paused_offset
    page.get_by_test_id("ctf-motion").click()
    page.wait_for_timeout(180)
    assert edge_offset() != paused_offset
    page.get_by_test_id("ctf-motion").click()
    page.evaluate("window._ipc.sidebarOpen=true; void window._ipc.cy.fit(undefined,64)")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.wait_for_timeout(350)
    page.screenshot(path=str(artifact / "ctf-workspace.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("window._ipc.sidebarOpen=false; window._ipc.cy.resize(); void window._ipc.cy.fit(undefined,64)")
    page.wait_for_timeout(350)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.evaluate("document.querySelector('#cy').clientWidth >= 380")
    page.screenshot(path=str(artifact / "ctf-mobile.png"), full_page=True)
    page.get_by_test_id("ctf-inspector-toggle").click()
    page.wait_for_function("document.querySelector('[data-testid=pentest-inspector]').classList.contains('inspector-open')")
    page.get_by_role("button", name="关闭 CTF 详情", exact=True).click()
    page.wait_for_function("!document.querySelector('[data-testid=pentest-inspector]').classList.contains('inspector-open')")
    return {"project_id": project_id, "curve_styles": sorted(set(curve_styles)), "drag_preserved": True, "motion_toggle": True, "mobile_drawer": True}


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
            run_preflight = page.request.post(f"{base}/api/workflows/{workflow_id}/preflight")
            if not run_preflight.ok:
                raise RuntimeError(f"preflight failed ({run_preflight.status}): {run_preflight.text()}")
            started = page.request.post(
                f"{base}/api/workflows/{workflow_id}/start",
                data={"idempotency_key": f"e2e-run-{uuid4().hex[:12]}"},
            )
            if not started.ok:
                raise RuntimeError(f"run start failed ({started.status}): {started.text()}")
            started_json = started.json()
            run_id = started_json["run"]["id"]
            page.evaluate("window._ipc.openRuns()")
            page.wait_for_function("id => window._ipc.showRuns && window._ipc.runs.some(r=>r.id===id)", arg=run_id)
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
            final_snapshot = page.request.get(f"{base}/api/runs/{run_id}").json()
            assert final_snapshot["run"]["status"] in {"running", "finished"}
            assert all(
                challenge["state"] == "solved" for challenge in final_snapshot["challenges"]
            ), final_snapshot["challenges"]
            page.wait_for_function(
                "id => window._ipc.runSnapshot?.run?.id === id", arg=run_id
            )
            page.locator('[x-show="showRuns"]').get_by_text("Runs", exact=True).first.wait_for()
            page.screenshot(path=str(artifact / "completed.png"), full_page=True)
            wp_result = check_project_wp(
                page, base, artifact, run_json["challenges"][0]["project_id"]
            )
            ctf_workspace = check_ctf_workspace(
                page, artifact, run_json["challenges"][0]["project_id"]
            )
            tsecbench = check_tsecbench_multiflag(page, base, artifact)
            result = {"ok": True, "url": page.url, "console": console, "evidence": evidence, "wp": wp_result, "ctf_workspace": ctf_workspace, "tsecbench": tsecbench}
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
