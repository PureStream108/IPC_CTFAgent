from __future__ import annotations

from backend.core.config import MemberConfig
from backend.members.adapters import make_adapter
from backend.members.base_member import BaseMember, MemberDeps


def create_member(config: MemberConfig, deps: MemberDeps, script: list | None = None) -> BaseMember:
    adapter = make_adapter(config, name=config.name, script=script)
    return BaseMember(config.name, adapter, deps)
