from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.server.app import create_app
from tests.helpers import write_mock_config

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path):
    cfgdir = write_mock_config(tmp_path / "config")
    app = create_app(root=tmp_path)
    with TestClient(app) as c:
        c.app.state.ipc.config_dir = cfgdir
        c.app.state.ipc.reload_config()
        yield c


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "ipcApp" in body
    assert "CTF_Agent" not in body
    assert ">IPC Agent<" in body
    assert "BROADCAST" in body
    assert "Logs" in body
    assert "WP" in body
    assert "Derive" in body
    assert "project_log" in body
    assert "llm_log" in body
    assert "globalLogKind==='llm_log'" in body
    assert "'member: ' + logEntryMember(entry)" in body
    assert 'logEntryMember(entry){ return entry?.member || entry?.worker || entry?.agent || "unknown"; }' in body
    assert "memory_log" in body
    assert "New Project" in body
    assert "Memory" in body
    assert "First-time setup" not in body
    assert 'await this.authRequest("POST","/auth/setup"' not in body
    assert 'await this.authRequest("POST","/auth/login"' not in body
    assert "OpenAI-compatible action agent with durable IPC history" not in body
    assert "claudecode" not in body
    assert "Claude Code" not in body
    assert "OpenAI-compatible API" in body
    assert "IPC live log" not in body
    assert "opsLiveEvents" not in body
    assert "opsStatusLine()" in body
    assert "noteOpsToolEvent" in body
    assert 'event.type==="delta"' in body
    assert 'event.type==="thinking"' in body
    assert "opsReasoningOpen" in body
    assert "renderMarkdown" in body
    assert "DOMPurify.sanitize" in body
    assert "marked.min.js" in body
    assert "purify.min.js" in body
    for token in (
        "wpViewMode", "setWPView", "wpLiveStatus", "new EventSource",
        "closeProjectWP", "applyProjectWPUpdate", "wpDisplayHTML",
    ):
        assert token in body
    project_wp = body.split('x-show="showWP"', 1)[1].split('x-show="pentestDetailOpen"', 1)[0]
    assert 'x-show="wpSource===\'project\'"' in project_wp
    assert 'aria-label="Writeup 显示模式"' in project_wp
    assert 'x-html="wpDisplayHTML()"' in project_wp
    assert 'x-text="wpText"' in project_wp
    assert 'wpSource===\'project\' ? closeProjectWP() : showWP=false' in project_wp
    global_wp = body.split('x-show="showGlobalWP"', 1)[1].split('x-show="showWP"', 1)[0]
    assert "wpViewMode" not in global_wp
    assert "wpLiveStatus" not in global_wp
    pentest_js = (REPO_ROOT / "frontend/pentest.js").read_text(encoding="utf-8")
    assert 'this.wpSource = "pentest"' in pentest_js
    assert "opsNextCalls" in body
    assert "drainOpsNextCalls" in body
    assert "await this.interruptOpsRun();\r\n          return;" not in body
    assert "questionAnswers[question.id]" in body
    assert "has been answered. Resume the dependent operation" not in body
    assert "已回答问题" in body
    assert "Project phase: " in body
    assert "IPC startup: " not in body
    assert "deleteOpsWorkflow" in body
    assert "authorizeOpsWorkflow" in body
    assert "/confirmation" in body
    assert "onQuestionEnter($event, question.id)" in body
    assert "Workflow secret" not in body
    assert 'question.sensitive?"******"' not in body
    assert 'placeholder="confirmation phrase"' not in body
    assert "Review every URL, method and template above, then type:" not in body
    assert "startOpsRun" in body
    assert "opsStartBlockedReason" in body
    assert "/preflight" in body
    assert "payload.select=selection" in body
    assert "projectStatusFilter" in body
    assert "filteredProjects()" in body
    assert "projectStatusGroup(status)" in body
    assert "projectStatusRank" in body
    assert "s === 'completed'" not in body
    for label in ("Solved", "Running", "Created", "Stopped"):
        assert label in body
    assert "Archived" in body
    assert "Interrupt IPC" in body
    assert "/api/ops/chat/interrupt" in body
    assert "interruptOpsRun" in body
    assert "onOpsInputEnter" in body
    assert "autoGrowOpsInput" in body
    assert "Enter to send · Shift+Enter for newline" in body
    assert "Ctrl+Enter to send" not in body
    assert 'showOps=false;openIPCConfig()' in body
    assert '@click="openSkills()"' in body
    assert "flex flex-col gap-1.5 pb-2 mb-2" in body
    assert "this.opsWorkflowId===workflowId ? null : workflowId" in body
    assert "built-in GZCTF workflow setup" in body
    assert "New GZCTF" not in body
    assert "GZCTF via IPC" not in body
    assert "startPresetWithIpc" in body
    assert "loadWorkflowPresets" in body
    assert "Platform presets" in body
    assert "配置 GZCTF 预设" in body
    assert "secret_name=gzctf_token" not in body
    assert "Search loaded logs" in body
    # The log catalog is metadata-only; entry bodies load per selection.
    assert 'include_entries:"false"' in body
    assert "loadGlobalLogCatalog" in body
    assert "loadSelectedLogEntries" in body
    assert "selectLogKind" in body
    assert "logCompetitionFilterId" in body
    assert "/logs/projects?limit=500" not in body
    assert "workflow_id:this.opsWorkflowId" in body
    assert "skillSearchResults" in body
    assert "installSkillSpec" in body
    assert "uploadSkillFile" in body
    assert "skillDrop(folder)" in body
    assert "/skills/upload" in body
    assert '"/skills/move"' in body
    assert '"/skills/add"' in body
    assert "credentials for IPC" in body
    assert "pollOpsRun" in body
    assert "token/value (never added to chat history)" not in body
    assert "openOpsAgent" in body
    assert '"/api/ops/chat"' in body
    assert "confirmOpsWorkflow" in body
    # CTF execution is run-owned: the bypass buttons are gone and the run view
    # is the control surface instead.
    assert "submitFoundFlags" not in body
    assert "Import all" not in body
    assert "Submit all found flags" not in body
    assert "openRuns()" in body
    assert "/api/runs" in body
    assert "runControl(action)" in body
    assert "current.ownership.ownership==='competition'" in body
    assert "'run ' + (current.ownership.run_id||'').slice(0,8)" in body
    assert "activeRun()" in body
    assert "addMember" not in body
    assert "API surface" in body
    assert "chat_completions" in body
    assert "anthropic" in body
    assert "cfg.runtime.zap_enabled" not in body
    assert "工具目录" in body
    assert "经验记忆" in body
    assert "catalogRows" in body
    assert "openCatalogDoc" in body
    assert "/memory/catalog" in body
    assert "打开 Markdown" in body
    assert "Add Hint" in body
    assert "cytoscape-dagre.js" in body
    assert 'name:"dagre"' in body
    assert 'data-testid="ctf-motion"' in body
    assert 'data-testid="ctf-toolbar"' in body
    assert "decorateCtfEdges" in body
    assert "unbundled-bezier" in body
    assert "rememberCtfNodePosition" in body
    assert "restoreCtfManualPositions" in body
    assert "autoungrabify:false" in body
    assert 'id:"origin_ipc",source:"fact:origin",target:"ipc"' in body
    assert "for(const a of d.agents)" in body
    assert "memberForIntent(intent)" in body
    assert "intentDisplaySources(intent)" in body
    assert 'originDisplaySource' in body
    assert 'originDisplaySource(owner=null){ return owner || (this.agentRecord("aventurine") ? "aventurine" : null); }' in body
    assert 'intent.creator && this.agentRole(intent.creator)==="member"' in body
    assert 'Display Source' in body
    assert 'C: ${creator}\\nW: ${worker}' in body
    assert 'fid&&fid!=="origin"' in body
    assert "memberContextSource" in body
    assert 'etype:"context"' in body
    assert '"start","assign","report","flag","wp","return"' in body
    assert "source,target,etype:kind" in body
    assert 'etype:"producer"' not in body
    assert 'etype:kind' in body
    assert '"taxi-direction":"vertical"' in body
    assert '"control-point-distances":"data(curveDistances)"' in body
    # The standalone Competition console is gone; its per-contest view is a
    # project filter in the sidebar instead.
    assert 'x-show="showCompetition"' not in body
    assert "openCompetition()" not in body
    assert "loadCompetitionRuns" not in body
    assert "memberHealthDot(item)" not in body
    assert "mergedEvents()" not in body
    assert "formatQuietSeconds" not in body
    assert "/projects/competitions" in body
    assert "competitionFilterId" in body
    assert "competitionFilterOpen=!competitionFilterOpen" in body
    assert "loadCompetitions" in body
    assert "competitionProjects()" in body
    assert body.count("formatDuration(ms)") == 1
    # WP is a hot switch on the WP pages, mirroring the Logs "Enabled" toggle.
    assert "wpEnabled" in body
    assert "toggleWP()" in body
    assert '"/wp/status"' in body
    assert "热开关" in body
    for cat in ("pwn", "reverse", "crypto", "web", "misc", "ai", "osint"):
        assert cat in body
    assert "ATTACHMENT" in body


def test_static_assets(client):
    for path in ("/static/vendor/cytoscape.min.js", "/static/vendor/dagre.min.js",
                 "/static/vendor/cytoscape-dagre.js", "/static/vendor/alpine.min.js",
                 "/static/vendor/marked.min.js", "/static/vendor/purify.min.js",
                 "/static/ipc.png"):
        assert client.get(path).status_code == 200


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_ops_chat_helpers_behave():
    """Execute the chat helpers instead of only asserting that markup exists.

    The assertions above cannot catch a logic error, which is how a
    duplicate-name helper once silently shadowed a working one. This runs the
    markdown renderer and the nextcall queue for real.
    """
    result = subprocess.run(
        ["node", str(REPO_ROOT / "tests/js/ops_chat_checks.mjs"), str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip().endswith("ok")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_wp_viewer_helpers_behave():
    result = subprocess.run(
        ["node", str(REPO_ROOT / "tests/js/wp_viewer_checks.mjs"), str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip().endswith("ok")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_ctf_graph_helpers_behave():
    result = subprocess.run(
        ["node", str(REPO_ROOT / "tests/js/ctf_graph_checks.mjs"), str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip().endswith("ctf graph checks passed")
