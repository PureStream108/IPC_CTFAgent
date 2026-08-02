from __future__ import annotations

from pathlib import Path

import pytest

from backend.blackboard import graph_store
from backend.core.state import AppState
from backend.core.wp_writer import WRITEUP_REQUEST, WriteupGenerationError, write_wp, write_wp_content
from tests.helpers import write_mock_config


@pytest.fixture
def state(tmp_path):
    config_dir = write_mock_config(tmp_path / "config")
    return AppState(root=tmp_path, config_dir=config_dir)


def _project(state: AppState, flag: str = "flag{writer_test}") -> str:
    with state.db.connect() as conn:
        project_id = graph_store.create_project(
            conn,
            "Writer test",
            "http://challenge.invalid",
            "capture the flag",
            "web",
        )
        graph_store.set_flag(conn, project_id, flag)
    return project_id


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


def test_auto_writer_rejects_the_generation_prompt_instead_of_persisting_it(state):
    project_id = _project(state)

    with pytest.raises(WriteupGenerationError, match="generation prompt"):
        write_wp(
            state.db,
            project_id,
            state.wp_dir,
            generator=lambda _: WRITEUP_REQUEST,
        )

    assert list(state.wp_dir.glob("*.md")) == []


def test_auto_writer_persists_a_valid_model_writeup(state):
    flag = "flag{writer_test}"
    project_id = _project(state, flag)

    path = Path(
        write_wp(
            state.db,
            project_id,
            state.wp_dir,
            generator=lambda _: _complete_markdown(flag),
        )
    )

    assert path.read_text(encoding="utf-8") == _complete_markdown(flag).rstrip() + "\n"


def test_final_writer_rejects_a_prompt_stub_but_allows_a_draft(state):
    project_id = _project(state)

    with pytest.raises(ValueError, match="generation prompt"):
        write_wp_content(state.db, project_id, state.wp_dir, WRITEUP_REQUEST)

    path = Path(write_wp_content(state.db, project_id, state.wp_dir, "# working notes"))
    assert path.read_text(encoding="utf-8") == "# working notes\n"


def test_final_writer_requires_the_verified_flag_and_python_script(state):
    project_id = _project(state)

    with pytest.raises(ValueError, match="verified flag"):
        write_wp_content(
            state.db,
            project_id,
            state.wp_dir,
            _complete_markdown("flag{different}"),
            expected_flag="flag{writer_test}",
            require_complete=True,
        )
