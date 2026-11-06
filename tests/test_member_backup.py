from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.api.config import (
    ConfigUpdate,
    LLMUpdate,
    get_config,
    health_check,
    update_config,
)
from backend.core.config import MEMBER_NAMES, AppConfig, LLMConfig

PRIMARY = {
    "api_format": "openai",
    "api_key": "primary-key",
    "base_url": "https://primary.example/v1",
    "model": "deepseek-v4.1-flash",
    "reasoning_effort": "max",
}
BACKUP = {
    "api_format": "openai",
    "api_key": "backup-key",
    "base_url": "https://backup.example/v1",
    "model": "deepseek-v4.1-flash",
    "reasoning_effort": "max",
}


def _state() -> SimpleNamespace:
    config = AppConfig(diamond=LLMConfig(api_format="mock"), member=LLMConfig(**PRIMARY))
    return SimpleNamespace(config=config, save_config=lambda: None)


def test_view_reports_a_null_backup_and_all_primary_slots():
    view = get_config(state=_state())
    assert view["member_backup"] is None
    assert view["member_key_slots"] == {name: "primary" for name in MEMBER_NAMES}
    assert "primary-key" not in str(view)


def test_update_sets_the_backup_and_returns_a_redacted_view():
    state = _state()
    view = update_config(ConfigUpdate(member_backup=LLMUpdate(**BACKUP)), state)
    assert view["member_backup"]["api_key_set"] is True
    assert view["member_backup"]["configured"] is True
    assert view["member_backup"]["api_key_preview"] == "bac***"
    assert "backup-key" not in str(view)
    assert view["member_key_slots"]["amber"] == "primary"
    assert view["member_key_slots"]["agate"] == "backup"
    assert sum(slot == "backup" for slot in view["member_key_slots"].values()) == 5
    assert [member.api_key for member in state.config.members][0::2] == ["primary-key"] * 5
    assert [member.api_key for member in state.config.members][1::2] == ["backup-key"] * 5


def test_update_clear_drops_the_backup_and_restores_ten_primary_seats():
    state = _state()
    update_config(ConfigUpdate(member_backup=LLMUpdate(**BACKUP)), state)
    assert state.config.member_backup is not None

    view = update_config(ConfigUpdate(clear_member_backup=True), state)

    assert state.config.member_backup is None
    assert view["member_backup"] is None
    assert set(view["member_key_slots"].values()) == {"primary"}
    assert {member.api_key for member in state.config.members} == {"primary-key"}


def test_update_rejects_setting_and_clearing_the_backup_together():
    state = _state()
    with pytest.raises(HTTPException) as excinfo:
        update_config(
            ConfigUpdate(member_backup=LLMUpdate(**BACKUP), clear_member_backup=True),
            state,
        )
    assert excinfo.value.status_code == 400
    assert state.config.member_backup is None


def test_update_rejects_an_unknown_backup_format():
    with pytest.raises(ValueError):
        ConfigUpdate(member_backup={"api_format": "claudecode"})


def test_health_reports_a_skipped_backup_slot():
    state = SimpleNamespace(
        config=AppConfig(diamond=LLMConfig(api_format="mock"), member=LLMConfig(api_format="mock")),
        save_config=lambda: None,
    )
    result = health_check(state=state)["results"]
    assert result["member_backup"]["skipped"] is True
