from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.config import AppConfig, LLMConfig, MemberConfig, load_config, save_config


def test_default_config_loads_and_starts():
    cfg = load_config()
    # Default config.yaml uses mock format, so it should boot.
    assert cfg.startup_errors() == []
    assert len(cfg.members) == 4
    assert {m.name for m in cfg.members} == {"aventurine", "pearl", "jade", "topaz"}
    assert cfg.runtime.eval_interval_steps == 20


def test_mock_is_always_configured():
    assert LLMConfig(api_format="mock").configured is True
    assert LLMConfig(api_format="openai").configured is False
    assert LLMConfig(api_format="openai", api_key="k", base_url="u").configured is True


def test_diamond_without_creds_blocks_startup():
    cfg = AppConfig(
        diamond=LLMConfig(api_format="openai"),  # no creds
        members=[MemberConfig(name="aventurine", api_format="mock")],
    )
    errors = cfg.startup_errors()
    assert any("Diamond" in e for e in errors)


def test_requires_at_least_one_member_with_creds():
    cfg = AppConfig(
        diamond=LLMConfig(api_format="mock"),
        members=[MemberConfig(name="a", api_format="openai")],  # no creds
    )
    errors = cfg.startup_errors()
    assert any("Member" in e for e in errors)


def test_available_members_filters_unconfigured():
    cfg = AppConfig(
        diamond=LLMConfig(api_format="mock"),
        members=[
            MemberConfig(name="a", api_format="openai", api_key="k", base_url="u"),
            MemberConfig(name="b", api_format="openai"),  # not configured
        ],
    )
    avail = cfg.available_members()
    assert len(avail) == 1
    assert avail[0].name == "a"


def test_duplicate_member_names_rejected():
    with pytest.raises(Exception):
        AppConfig(
            diamond=LLMConfig(api_format="mock"),
            members=[
                MemberConfig(name="dup", api_format="mock"),
                MemberConfig(name="dup", api_format="mock"),
            ],
        )


def test_save_and_reload_roundtrip(tmp_path: Path):
    cfg = load_config()
    cfg.diamond.api_format = "openai"
    cfg.diamond.api_key = "secret"
    cfg.diamond.base_url = "https://example.test/v1"
    save_config(cfg, tmp_path)
    reloaded = load_config(tmp_path)
    assert reloaded.diamond.api_key == "secret"
    assert reloaded.diamond.base_url == "https://example.test/v1"
    assert reloaded.diamond.api_format == "openai"


def test_models_yaml_fills_empty_model(tmp_path: Path):
    # An empty `model` field must be filled from models.yaml defaults, keyed by
    # api_format. Build an isolated config so this never depends on the shipped
    # config.yaml (which may carry real credentials + explicit models).
    src = Path(__file__).resolve().parent.parent / "backend" / "config"
    (tmp_path / "models.yaml").write_text((src / "models.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "log_enabled: true\n"
        "diamond:\n"
        "  api_format: mock\n"
        "  api_key: ''\n"
        "  base_url: ''\n"
        "  model: ''\n"
        "members:\n"
        "- name: aventurine\n"
        "  api_format: openai\n"
        "  api_key: 'k'\n"
        "  base_url: 'https://x/v1'\n"
        "  model: ''\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    # empty diamond model -> mock default; empty member model -> openai default
    assert cfg.diamond.model == "mock-model"
    assert cfg.members[0].model == "gpt-4o"
