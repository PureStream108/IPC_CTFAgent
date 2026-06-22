from __future__ import annotations

import shutil
from pathlib import Path


def write_mock_config(config_dir: Path) -> Path:
    """Create a hermetic mock config directory for tests."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(
        "log_enabled: true\n"
        "diamond:\n"
        "  api_format: mock\n"
        "  api_key: ''\n"
        "  base_url: ''\n"
        "  model: ''\n"
        "members:\n"
        "- name: aventurine\n"
        "  api_format: mock\n"
        "  api_key: ''\n"
        "  base_url: ''\n"
        "  model: ''\n"
        "- name: pearl\n"
        "  api_format: mock\n"
        "  api_key: ''\n"
        "  base_url: ''\n"
        "  model: ''\n"
        "- name: jade\n"
        "  api_format: mock\n"
        "  api_key: ''\n"
        "  base_url: ''\n"
        "  model: ''\n"
        "- name: topaz\n"
        "  api_format: mock\n"
        "  api_key: ''\n"
        "  base_url: ''\n"
        "  model: ''\n"
        "runtime:\n"
        "  eval_interval_steps: 20\n"
        "  interval: 2\n"
        "  intent_timeout: 30\n"
        "  reason_timeout: 30\n"
        "  max_members_per_report: 4\n"
        "  sandbox_backend: local\n"
        "  max_member_steps: 60\n"
        "  max_member_actions_per_task: 20\n",
        encoding="utf-8",
    )

    src = Path(__file__).resolve().parent.parent / "backend" / "config"
    for name in ("models.yaml", "limits.yaml"):
        shutil.copy(src / name, config_dir / name)
    return config_dir
