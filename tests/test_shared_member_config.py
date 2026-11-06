from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.api.config import ConfigUpdate, LLMUpdate, RuntimeUpdate, update_config
from backend.core.config import (
    MEMBER_NAMES,
    AppConfig,
    LLMConfig,
    MemberConfig,
    load_config,
    save_config,
)


def test_conflicting_legacy_endpoints_require_explicit_selection(tmp_path):
    config = AppConfig(diamond=LLMConfig(api_format="mock"), members=[
        MemberConfig(name="aventurine", api_format="mock", model="one"),
        MemberConfig(name="pearl", api_format="mock", model="two"),
    ])
    assert config.member_config_conflict
    assert config.available_members() == []
    save_config(config, tmp_path)
    config = load_config(tmp_path)
    assert config.member_config_conflict
    state = SimpleNamespace(config=config, save_config=lambda: None)
    update_config(ConfigUpdate(use_legacy_member="pearl"), state)
    assert not state.config.member_config_conflict
    assert [m.name for m in state.config.members] == list(MEMBER_NAMES)
    assert {m.model for m in state.config.members} == {"two"}


def test_update_all_member_fields_once_and_preserve_key():
    state = SimpleNamespace(config=AppConfig(member=LLMConfig(api_key="secret", base_url="https://api.example")), save_config=lambda: None)
    view = update_config(ConfigUpdate(member=LLMUpdate(model="new", reasoning_effort="max")), state)
    assert all(m.api_key == "secret" and m.model == "new" and m.reasoning_effort == "max" for m in state.config.members)
    assert "secret" not in str(view)
    with pytest.raises(HTTPException):
        update_config(ConfigUpdate(members={"amber": LLMUpdate(model="other")}), state)
    assert state.config.member.model == "new"


def test_legacy_removed_field_is_consumed_only_on_load(tmp_path):
    (tmp_path / "config.yaml").write_text("runtime:\n  zap_enabled: true\n", encoding="utf-8")
    config = load_config(tmp_path)
    save_config(config, tmp_path)
    assert "zap_enabled" not in (tmp_path / "config.yaml").read_text(encoding="utf-8")


def test_new_reasoning_api_disallows_removed_values():
    for value in ("auto", "none", "minimal"):
        with pytest.raises(ValueError):
            LLMUpdate(reasoning_effort=value)


def test_removed_browser_runtime_update_is_rejected():
    with pytest.raises(ValueError):
        RuntimeUpdate(browser_allowed_origins=["https://legacy.test"])
    with pytest.raises(ValueError):
        ConfigUpdate(runtime={"browser_response_preview_bytes": 8192})


def test_removed_runtime_setting_is_rejected_by_new_api():
    with pytest.raises(ValueError):
        ConfigUpdate(runtime={"zap_enabled": True})
    with pytest.raises(ValueError):
        ConfigUpdate(zap_enabled=True)
