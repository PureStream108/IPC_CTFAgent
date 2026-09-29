from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend import cli
from backend.api.config import ConfigUpdate, LLMUpdate, update_config
from backend.core.config import AppConfig, MEMBER_NAMES, load_config
from backend.core.state import AppState


def test_app_state_starts_without_a_config_file(tmp_path):
    config_dir = tmp_path / "missing-config"
    state = AppState(root=tmp_path / "runtime", config_dir=config_dir)
    try:
        assert [member.name for member in state.config.members] == list(MEMBER_NAMES)
        assert not any(member.configured for member in state.config.members)
        assert state.config.startup_errors()
    finally:
        state.close()


def test_check_reports_ui_ready_without_llm_credentials(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", AppConfig)

    assert cli.main(["check"]) == 0
    output = capsys.readouterr().out
    assert "Web UI is available" in output
    assert "configure LLM endpoints" in output


def test_config_update_keeps_fixed_member_roster():
    state = SimpleNamespace(config=AppConfig(), save_config=lambda: None)
    view = update_config(ConfigUpdate(diamond=LLMUpdate(api_format="mock"), member=LLMUpdate(api_format="mock")), state)
    assert [m.name for m in state.config.members] == list(MEMBER_NAMES)
    assert all(m["configured"] for m in view["members"])
    assert view["startup_errors"] == []
    with pytest.raises(HTTPException):
        update_config(ConfigUpdate(remove_members=["amber"]), state)


def test_config_update_rejects_per_member_endpoints():
    state = SimpleNamespace(config=AppConfig(), save_config=lambda: None)
    with pytest.raises(HTTPException, match="shared member"):
        update_config(ConfigUpdate(members={"amber": LLMUpdate(api_format="mock")}), state)


def test_removed_zap_environment_does_not_restore_integration(tmp_path, monkeypatch):
    monkeypatch.setenv("IPC_ZAP_ENABLED", "true")
    assert "zap_enabled" not in load_config(tmp_path).runtime.model_dump()
