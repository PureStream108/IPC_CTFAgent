from __future__ import annotations

from pathlib import Path

import pytest

from backend.blackboard import graph_store
from backend.core.wp_writer import persist_validated_writeup


def _complete_markdown(flag: str) -> str:
    return f'''# Writer test

## Summary

This regression fixture demonstrates a complete, validated writeup artifact.

## Solution

```python
import re


def main() -> None:
    response = "proof: {flag}"
    match = re.search(r"flag\\{{[^}}]+\\}}", response)
    if not match:
        raise SystemExit("flag not found")
    print(match.group(0))


if __name__ == "__main__":
    main()
```

## Flag

`{flag}`
'''


@pytest.mark.parametrize("has_existing_file", [False, True])
def test_transactional_writer_restores_file_when_path_registration_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_existing_file: bool
) -> None:
    wp_dir = tmp_path / "wp"
    wp_dir.mkdir()
    target = wp_dir / "Writer test.md"
    if has_existing_file:
        target.write_text("# original\n", encoding="utf-8")

    class _Result:
        @staticmethod
        def fetchone():
            return {"title": "Writer test", "wp_path": str(target) if has_existing_file else None}

    class _Connection:
        @staticmethod
        def execute(query, params):
            del query, params
            return _Result()

    def fail_registration(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("database registration failed")

    monkeypatch.setattr(graph_store, "set_wp_path", fail_registration)

    with pytest.raises(RuntimeError, match="database registration failed"):
        persist_validated_writeup(
            _Connection(),
            "project-1",
            wp_dir,
            _complete_markdown("flag{writer_test}"),
            expected_flag="flag{writer_test}",
        )

    if has_existing_file:
        assert target.read_text(encoding="utf-8") == "# original\n"
    else:
        assert not target.exists()
