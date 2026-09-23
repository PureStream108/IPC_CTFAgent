from __future__ import annotations

from pathlib import Path
import os
import stat

import pytest
import yaml

from backend.core.config import AppConfig, MEMBER_NAMES, LLMConfig, MemberConfig, RuntimeConfig, load_config, save_config
from tests.helpers import write_mock_config


def test_mock_config_loads_and_starts(tmp_path: Path):
    cfg = load_config(write_mock_config(tmp_path / "config"))
    assert cfg.startup_errors() == []
    assert len(cfg.members) == 10
    assert tuple(m.name for m in cfg.members) == MEMBER_NAMES
    assert cfg.runtime.eval_interval_steps == 20


def test_mock_is_always_configured():
    assert LLMConfig(api_format="mock").configured is True
    assert LLMConfig(api_format="openai").configured is False
    assert LLMConfig(api_format="openai", api_key="k", base_url="u").configured is True


def test_browser_runtime_limits_and_origins_are_validated():
    runtime = RuntimeConfig(
        browser_event_limit=1000,
        browser_response_preview_bytes=16384,
        browser_allowed_origins=["HTTPS://Example.Test:443/", "http://[::1]:8080"],
    )
    assert runtime.browser_allowed_origins == ["https://example.test", "http://[::1]:8080"]
    with pytest.raises(Exception):
        RuntimeConfig(browser_event_limit=1001)
    with pytest.raises(Exception):
        RuntimeConfig(browser_response_preview_bytes=16385)
    with pytest.raises(Exception):
        RuntimeConfig(browser_artifact_max_bytes=50 * 1024 * 1024 + 1)
    with pytest.raises(Exception):
        RuntimeConfig(browser_allowed_origins=["file:///tmp/page.html"])
    with pytest.raises(Exception):
        RuntimeConfig(browser_allowed_origins=["https://example.test/path"])


def test_llm_surface_and_reasoning_settings_are_validated():
    cfg = LLMConfig(api_surface="responses", reasoning_effort="low")
    assert cfg.api_surface == "responses"
    assert cfg.reasoning_effort == "low"
    with pytest.raises(Exception):
        LLMConfig(api_surface="legacy-completions")
    with pytest.raises(Exception):
        LLMConfig(reasoning_effort="ultra")


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
    assert len(avail) == 10
    assert all(m.api_key == "k" for m in avail)


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
    cfg = load_config(write_mock_config(tmp_path / "source"))
    cfg.diamond.api_format = "openai"
    cfg.diamond.api_key = "secret"
    cfg.diamond.base_url = "https://example.test/v1"
    cfg.diamond.api_surface = "responses"
    cfg.diamond.reasoning_effort = "low"
    out_dir = tmp_path / "saved"
    save_config(cfg, out_dir)
    reloaded = load_config(out_dir)
    assert reloaded.diamond.api_key == "secret"
    assert reloaded.diamond.base_url == "https://example.test/v1"
    assert reloaded.diamond.api_format == "openai"
    assert reloaded.diamond.api_surface == "responses"
    assert reloaded.diamond.reasoning_effort == "low"


@pytest.mark.parametrize("failure_point", ["fsync", "replace"])
def test_failed_config_save_preserves_previous_file_and_cleans_staging(tmp_path, monkeypatch, failure_point):
    original = AppConfig(diamond=LLMConfig(api_key="previous-key"))
    save_config(original, tmp_path)
    previous_bytes = (tmp_path / "config.yaml").read_bytes()
    replacement = AppConfig(diamond=LLMConfig(api_key="replacement-key"))

    def fail(*args):
        raise OSError("simulated storage failure")

    monkeypatch.setattr(f"backend.core.config.os.{failure_point}", fail)
    with pytest.raises(OSError, match="simulated storage failure"):
        save_config(replacement, tmp_path)

    assert (tmp_path / "config.yaml").read_bytes() == previous_bytes
    assert load_config(tmp_path).diamond.api_key == "previous-key"
    assert list(tmp_path.glob(".config-*.tmp")) == []


def test_failed_first_config_save_leaves_no_partial_file(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("simulated disk full")

    monkeypatch.setattr("backend.core.config.os.fsync", fail)
    with pytest.raises(OSError, match="simulated disk full"):
        save_config(AppConfig(), tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions; Windows uses inherited ACLs")
def test_saved_config_credentials_are_owner_only(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("log_enabled: true\n", encoding="utf-8")
    target.chmod(0o644)
    save_config(AppConfig(diamond=LLMConfig(api_key="private-key")), tmp_path)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_models_yaml_fills_empty_model(tmp_path: Path):
    # An empty `model` field must be filled from models.yaml defaults, keyed by
    # api_format. Build an isolated config so this never depends on the repo
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
    model_defaults = yaml.safe_load((tmp_path / "models.yaml").read_text(encoding="utf-8"))[
        "defaults"
    ]
    assert cfg.members[0].model == model_defaults["openai"]


def test_load_config_drops_legacy_memory_limits(tmp_path: Path):
    config_dir = write_mock_config(tmp_path / "config")
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "limits:\n"
        + "  total_cpu: 8\n"
        + "  total_memory_gb: 20\n"
        + "  total_disk_gb: 25\n"
        + "  per_agent_memory_gb: 5\n"
        + "  max_concurrent_tasks: 3\n",
        encoding="utf-8",
    )

    cfg = load_config(config_dir)

    assert cfg.limits.total_cpu == 8
    assert cfg.limits.max_concurrent_tasks == 3
    assert "total_memory_gb" not in cfg.limits.model_dump()
