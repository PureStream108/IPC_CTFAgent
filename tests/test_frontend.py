from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.server.app import create_app


@pytest.fixture
def client(tmp_path):
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    src = Path(__file__).resolve().parent.parent / "backend" / "config"
    for name in ("config.yaml", "models.yaml", "limits.yaml"):
        shutil.copy(src / name, cfgdir / name)
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
    # IPC branding + key UI elements present
    assert "Equal CTF Solver Agents" in body
    assert "BROADCAST" in body
    assert "+ New Project" in body
    assert "Memory" in body
    # dagre remains the hidden graph layout engine
    assert "cytoscape-dagre.js" in body
    assert "name:'dagre'" in body
    # category options
    for cat in ("pwn", "reverse", "crypto", "web", "misc", "ai", "osint"):
        assert cat in body
    # attachment upload label
    assert "ATTACHMENT" in body


def test_static_assets(client):
    for path in ("/static/vendor/cytoscape.min.js", "/static/vendor/dagre.min.js",
                 "/static/vendor/cytoscape-dagre.js", "/static/vendor/alpine.min.js", "/static/ipc.png"):
        assert client.get(path).status_code == 200
