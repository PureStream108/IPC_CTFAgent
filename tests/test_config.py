from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from backend.core.config import (
    MEMBER_NAMES,
    AppConfig,
    LLMConfig,
    MemberConfig,
    RuntimeConfig,
    load_config,
    save_config,
)
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


def test_claudecode_is_no_longer_an_accepted_api_format():
    """The sidecar runtime is gone; only real wire formats remain."""
    with pytest.raises(ValueError):
        LLMConfig(api_format="claudecode")


def test_stored_claudecode_ops_config_is_migrated_with_a_warning(tmp_path: Path):
    """An existing deployment must still load after the runtime was removed.

    ``LLMConfig`` rejects unknown formats outright, so without this migration a
    stored ``claudecode`` config would fail every startup instead of degrading
    to the Anthropic endpoint it always pointed at.
    """
    import json

    from backend.ops.store import OpsStore

    config_dir = tmp_path / "data" / "ops-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "api_format": "claudecode",
                "base_url": "https://api.anthropic.com",
                "model": "claude-opus-4-8",
            }
        ),
        encoding="utf-8",
    )

    class _NoDatabase:
        def configure(self):
            return self

    store = OpsStore(tmp_path, database=_NoDatabase())
    config = store.load_llm_config()

    assert config.api_format == "anthropic"
    assert config.base_url == "https://api.anthropic.com"
    assert store.migrated_api_format is not None
    assert "claudecode" in store.migrated_api_format


def test_legacy_browser_runtime_keys_are_ignored_on_load(tmp_path: Path):
    config_dir = write_mock_config(tmp_path / "config")
    path = config_dir / "config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw.setdefault("runtime", {}).update(
        {
            "browser_event_limit": 300,
            "browser_console_limit": 50,
            "browser_error_limit": 20,
            "browser_response_preview_bytes": 8192,
            "browser_allowed_origins": ["https://legacy.test"],
            "browser_artifact_max_bytes": 1024,
            "zap_enabled": True,
        }
    )
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    cfg = load_config(config_dir)

    assert not any(key.startswith("browser_") for key in cfg.runtime.model_dump())
    assert "zap_enabled" not in cfg.runtime.model_dump()
    with pytest.raises(ValidationError):
        RuntimeConfig(browser_event_limit=300)


def test_llm_surface_and_reasoning_settings_are_validated():
    cfg = LLMConfig(api_surface="responses", reasoning_effort="low")
    assert cfg.api_surface == "responses"
    assert cfg.reasoning_effort == "low"
    with pytest.raises(ValidationError):
        LLMConfig(api_surface="legacy-completions")
    with pytest.raises(ValidationError):
        LLMConfig(reasoning_effort="ultra")


def test_diamond_without_creds_blocks_startup():
    cfg = AppConfig(
        diamond=LLMConfig(api_format="openai"),
        members=[MemberConfig(name="aventurine", api_format="mock")],
    )
    errors = cfg.startup_errors()
    assert any("Diamond" in e for e in errors)


def test_requires_at_least_one_member_with_creds():
    cfg = AppConfig(
        diamond=LLMConfig(api_format="mock"),
        members=[MemberConfig(name="a", api_format="openai")],
    )
    errors = cfg.startup_errors()
    assert any("Member" in e for e in errors)


def test_available_members_filters_unconfigured():
    cfg = AppConfig(
        diamond=LLMConfig(api_format="mock"),
        members=[
            MemberConfig(name="a", api_format="openai", api_key="k", base_url="u"),
            MemberConfig(name="b", api_format="openai"),
        ],
    )
    avail = cfg.available_members()
    assert len(avail) == 10
    assert all(m.api_key == "k" for m in avail)


def test_duplicate_member_names_rejected():
    with pytest.raises(ValidationError):
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


def test_pentest_mode_switches_default_off_and_ladder_is_monotonic():
    runtime = RuntimeConfig()
    assert runtime.diamond_pentest_mode is False
    assert runtime.diamond_actions_per_tick == 2
    assert (runtime.ladder_help_minutes, runtime.ladder_upgrade_minutes, runtime.ladder_park_minutes) == (10, 25, 40)
    assert runtime.state_write_ratio > runtime.compaction_trigger_ratio

    with pytest.raises(ValidationError, match="help < upgrade < park"):
        RuntimeConfig(ladder_help_minutes=30, ladder_upgrade_minutes=20)
    with pytest.raises(ValidationError, match="state_write_ratio must be above"):
        RuntimeConfig(compaction_trigger_ratio=0.9, state_write_ratio=0.85)


def test_container_limits_have_safe_defaults_and_validation():
    from backend.core.config import LimitsConfig

    limits = LimitsConfig()
    assert limits.mem_limit == "2g"
    assert limits.pids_limit == 512
    assert LimitsConfig(mem_limit="1536m", pids_limit=256).mem_limit == "1536m"
    with pytest.raises(ValidationError):
        LimitsConfig(mem_limit="two gigabytes")
    with pytest.raises(ValidationError):
        LimitsConfig(pids_limit=4)


def test_pentest_runtime_keys_survive_a_save_load_roundtrip(tmp_path: Path):
    config_dir = write_mock_config(tmp_path / "config")
    cfg = load_config(config_dir)
    cfg.runtime.diamond_pentest_mode = True
    cfg.runtime.diamond_actions_per_tick = 1
    cfg.runtime.ladder_park_minutes = 55
    cfg.limits.mem_limit = "1536m"
    save_config(cfg, config_dir)

    reloaded = load_config(config_dir)
    assert reloaded.runtime.diamond_pentest_mode is True
    assert reloaded.runtime.diamond_actions_per_tick == 1
    assert reloaded.runtime.ladder_park_minutes == 55
    assert reloaded.limits.mem_limit == "1536m"

def test_member_backup_absent_keeps_one_shared_key():
    cfg = AppConfig(
        diamond=LLMConfig(api_format="mock"),
        member=LLMConfig(api_format="openai", api_key="primary-key", base_url="https://api.example/v1"),
    )
    assert cfg.member_backup is None
    assert len(cfg.members) == 10
    assert {m.api_key for m in cfg.members} == {"primary-key"}


def test_member_backup_splits_the_ten_seats_five_five():
    cfg = AppConfig(
        diamond=LLMConfig(api_format="mock"),
        member=LLMConfig(
            api_format="openai",
            api_key="primary-key",
            base_url="https://primary.example/v1",
            model="deepseek-v4.1-flash",
            reasoning_effort="max",
        ),
        member_backup=LLMConfig(
            api_format="openai",
            api_key="backup-key",
            base_url="https://backup.example/v1",
            model="deepseek-v4.1-flash",
            reasoning_effort="max",
        ),
    )
    by_name = {m.name: m for m in cfg.members}
    assert [by_name[name].api_key for name in MEMBER_NAMES] == [
        "primary-key", "backup-key", "primary-key", "backup-key", "primary-key",
        "backup-key", "primary-key", "backup-key", "primary-key", "backup-key",
    ]
    assert by_name["amber"].api_key == "primary-key"
    assert by_name["agate"].api_key == "backup-key"
    assert cfg.startup_errors() == []
    assert "backup key equals primary (no spread)" not in cfg.startup_warnings()


def test_member_backup_requires_key_and_base_url():
    cfg = AppConfig(
        diamond=LLMConfig(api_format="mock"),
        member=LLMConfig(api_format="openai", api_key="primary-key", base_url="https://primary.example/v1"),
        member_backup=LLMConfig(api_format="openai", base_url="https://backup.example/v1"),
    )
    assert any("Member backup requires api_key and base_url" in error for error in cfg.startup_errors())


def test_member_backup_with_the_primary_key_warns():
    cfg = AppConfig(
        diamond=LLMConfig(api_format="mock"),
        member=LLMConfig(api_format="openai", api_key="same-key", base_url="https://api.example/v1"),
        member_backup=LLMConfig(api_format="openai", api_key="same-key", base_url="https://api.example/v1"),
    )
    assert "backup key equals primary (no spread)" in cfg.startup_warnings()


def test_member_backup_inherits_unset_model_surface_and_effort():
    cfg = AppConfig(
        diamond=LLMConfig(api_format="mock"),
        member=LLMConfig(
            api_format="openai",
            api_key="primary-key",
            base_url="https://primary.example/v1",
            model="deepseek-v4.1-flash",
            api_surface="responses",
            reasoning_effort="max",
        ),
        member_backup=LLMConfig(
            api_format="openai",
            api_key="backup-key",
            base_url="https://backup.example/v1",
            reasoning_effort="auto",
        ),
    )
    assert cfg.member_backup.model == "deepseek-v4.1-flash"
    assert cfg.member_backup.api_surface == "responses"
    assert cfg.member_backup.reasoning_effort == "max"
    assert {m.model for m in cfg.members} == {"deepseek-v4.1-flash"}


def test_member_backup_survives_a_save_load_roundtrip(tmp_path: Path):
    config_dir = write_mock_config(tmp_path / "config")
    cfg = load_config(config_dir)
    cfg.member_backup = LLMConfig(
        api_format="openai",
        api_key="backup-key",
        base_url="https://backup.example/v1",
        model="deepseek-v4.1-flash",
        reasoning_effort="max",
    )
    save_config(cfg, config_dir)
    raw = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
    assert list(raw) == [
        "log_enabled", "wp_enabled", "diamond", "member", "member_backup",
        "member_config_conflict", "members", "runtime", "limits",
    ]
    assert raw["member_backup"]["api_key"] == "backup-key"

    reloaded = load_config(config_dir)
    assert reloaded.member_backup is not None
    assert reloaded.member_backup.api_key == "backup-key"
    assert reloaded.members[1].api_key == "backup-key"
    assert reloaded.members[0].api_key == reloaded.member.api_key
