from __future__ import annotations

from functools import lru_cache
from pathlib import Path


_INVENTORY_PATH = Path(__file__).with_name("member_tools.txt")


@lru_cache(maxsize=1)
def member_tool_inventory() -> str:
    try:
        return _INVENTORY_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Member tool inventory unavailable: {exc}"


def member_tool_inventory_path() -> str:
    return str(_INVENTORY_PATH)
