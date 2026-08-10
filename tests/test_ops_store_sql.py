from __future__ import annotations

import json
import threading
from contextlib import contextmanager

from backend.ops.models import PlatformWorkflowSpec
from backend.ops.store import OpsStore


class _Cursor:
    rowcount = 1


class _RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, statement: str, params=()) -> _Cursor:
        self.calls.append((" ".join(statement.split()), tuple(params)))
        return _Cursor()


class _RecordingDatabase:
    def __init__(self) -> None:
        self.connection = _RecordingConnection()

    @contextmanager
    def connect(self):
        yield self.connection


def _store() -> tuple[OpsStore, _RecordingConnection]:
    database = _RecordingDatabase()
    store = object.__new__(OpsStore)
    store.db = database
    store._lock = threading.RLock()
    return store, database.connection


def _workflow_spec(name: str = "Example") -> PlatformWorkflowSpec:
    return PlatformWorkflowSpec.model_validate(
        {
            "name": name,
            "challenges": {
                "list_url": "https://ctf.example/api/challenges",
            },
        }
    )


def test_finish_run_casts_serialized_response_to_jsonb():
    store, connection = _store()
    store.get_run = lambda run_id: {"id": run_id, "status": "completed"}

    response = {"reply": "done", "metadata": {"attempt": 2}}
    store.finish_run("run_12345678", status="completed", response=response)

    statement, params = connection.calls[0]
    assert "response_json = %s::jsonb" in statement
    assert json.loads(params[1]) == response


def test_workflow_writes_cast_serialized_specs_to_jsonb():
    store, connection = _store()
    store.get_workflow = lambda workflow_id: {"id": workflow_id}

    created = store.create_workflow(_workflow_spec())
    store.update_workflow(created["id"], _workflow_spec("Updated"))

    insert_statement, insert_params = connection.calls[0]
    update_statement, update_params = connection.calls[1]
    assert "%s::jsonb" in insert_statement
    assert "spec_json = %s::jsonb" in update_statement
    assert json.loads(insert_params[4])["name"] == "Example"
    assert json.loads(update_params[1])["name"] == "Updated"
