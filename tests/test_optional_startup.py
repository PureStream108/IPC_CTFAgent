from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend import cli
from backend.api.config import ConfigUpdate, LLMUpdate, RuntimeUpdate, update_config
from backend.core.config import AppConfig, load_config
from backend.core.state import AppState


def test_app_state_starts_without_a_config_file(tmp_path):
    config_dir = tmp_path / "missing-config"
    state = AppState(root=tmp_path / "runtime", config_dir=config_dir)
    try:
        assert state.config.members == []
        assert state.config.startup_errors()
    finally:
        state.close()


def test_check_reports_ui_ready_without_llm_credentials(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", AppConfig)

    assert cli.main(["check"]) == 0
    output = capsys.readouterr().out
    assert "Web UI is available" in output
    assert "configure LLM endpoints" in output


def test_config_update_can_add_and_remove_first_member():
    state = SimpleNamespace(config=AppConfig(), save_config=lambda: None)

    view = update_config(
        ConfigUpdate(
            diamond=LLMUpdate(api_format="mock"),
            members={" Aventurine ": LLMUpdate(api_format="mock")},
            runtime=RuntimeUpdate(zap_enabled=True),
        ),
        state,
    )

    assert [member.name for member in state.config.members] == ["aventurine"]
    assert view["members"][0]["configured"] is True
    assert view["runtime"]["zap_enabled"] is True
    assert view["startup_errors"] == []

    view = update_config(
        ConfigUpdate(remove_members=["AVENTURINE"]),
        state,
    )
    assert state.config.members == []
    assert view["startup_errors"]


def test_config_update_rejects_invalid_member_names():
    state = SimpleNamespace(config=AppConfig(), save_config=lambda: None)

    with pytest.raises(HTTPException, match="member name"):
        update_config(
            ConfigUpdate(members={"../escape": LLMUpdate(api_format="mock")}),
            state,
        )


def test_zap_environment_override_is_optional(tmp_path, monkeypatch):
    monkeypatch.delenv("IPC_ZAP_ENABLED", raising=False)
    assert load_config(tmp_path).runtime.zap_enabled is False

    monkeypatch.setenv("IPC_ZAP_ENABLED", "true")
    assert load_config(tmp_path).runtime.zap_enabled is True

    monkeypatch.setenv("IPC_ZAP_ENABLED", "false")
    assert load_config(tmp_path).runtime.zap_enabled is False
