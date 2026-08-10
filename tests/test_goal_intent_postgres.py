from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from psycopg.errors import UniqueViolation

from backend.blackboard import edge_store, graph_store
from backend.persistence.database import Database


pytestmark = pytest.mark.postgres


def test_goal_intent_partial_unique_index_is_installed() -> None:
    database = Database().configure()
    try:
        with database.connect() as connection:
            row = connection.execute(
                """
                SELECT indexdef
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND tablename = 'intents'
                  AND indexname = 'uq_intents_one_goal_per_project'
                """
            ).fetchone()
    finally:
        database.close()

    assert row is not None
    definition = " ".join(row["indexdef"].lower().split())
    assert "unique index" in definition
    assert "(project_id)" in definition
    assert "where (to_fact_id = 'goal'::text)" in definition


def test_raw_sql_cannot_insert_a_second_goal_intent() -> None:
    database = Database().configure()
    try:
        with database.connect() as connection:
            project_id = graph_store.create_project(
                connection, "Unique goal", "origin", "goal", "web"
            )
            connection.execute(
                """
                INSERT INTO intents (
                    id, project_id, to_fact_id, description, creator,
                    created_at, concluded_at
                )
                VALUES ('intent_1', %s, 'goal', 'first', 'test', now(), now())
                """,
                (project_id,),
            )

        with pytest.raises(UniqueViolation):
            with database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO intents (
                        id, project_id, to_fact_id, description, creator,
                        created_at, concluded_at
                    )
                    VALUES ('intent_2', %s, 'goal', 'second', 'test', now(), now())
                    """,
                    (project_id,),
                )
    finally:
        database.close()


def test_concurrent_goal_completion_returns_one_intent() -> None:
    database = Database(min_size=2, max_size=4).configure()
    try:
        with database.connect() as connection:
            project_id = graph_store.create_project(
                connection, "Concurrent goal", "origin", "goal", "web"
            )
        barrier = threading.Barrier(2)

        def complete(worker: str) -> str:
            barrier.wait(timeout=5)
            with database.connect() as connection:
                intent = edge_store.complete_goal_intent(
                    connection,
                    project_id,
                    ["origin"],
                    f"completed by {worker}",
                    worker,
                    worker,
                )
                return intent.id

        with ThreadPoolExecutor(max_workers=2) as executor:
            ids = list(executor.map(complete, ("jade", "topaz")))

        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, lease_owner, lease_token, lease_expires_at
                FROM intents
                WHERE project_id = %s AND to_fact_id = 'goal'
                """,
                (project_id,),
            ).fetchall()
    finally:
        database.close()

    assert ids[0] == ids[1]
    assert rows == [
        {
            "id": ids[0],
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
        }
    ]
