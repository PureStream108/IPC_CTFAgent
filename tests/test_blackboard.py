from __future__ import annotations

import pytest

from backend.blackboard import edge_store, graph_store, node_store
from backend.blackboard.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "graph.db").configure()
    return database


def test_create_project_seeds_facts_and_agents(db):
    with db.connect() as conn:
        pid = graph_store.create_project(conn, "Test", "origin desc", "goal desc", "web")
    with db.connect() as conn:
        detail = graph_store.project_detail(conn, pid)
    assert detail is not None
    assert detail.project.status == "created"
    assert detail.project.category == "web"
    fact_ids = {f.id for f in detail.facts}
    assert fact_ids == {"origin", "goal"}
    agent_names = {a.name for a in detail.agents}
    assert agent_names == {"ipc", "diamond"}


def test_intent_lifecycle_claim_conclude(db):
    with db.connect() as conn:
        pid = graph_store.create_project(conn, "T", "o", "g", "pwn")
        intent = edge_store.create_intent(conn, pid, ["origin"], "explore web", "diamond")
        assert intent.worker is None and intent.to is None
        edge_store.claim_intent(conn, pid, intent.id, "aventurine")
    with db.connect() as conn:
        row = edge_store.get_intent(conn, pid, intent.id)
        assert row["worker"] == "aventurine"
        fact = node_store.create_fact(conn, pid, "found a clue")
        edge_store.conclude_intent(conn, pid, intent.id, "aventurine", fact.id)
    with db.connect() as conn:
        model = edge_store.intent_to_model(conn, edge_store.get_intent(conn, pid, intent.id), pid)
        assert model.to == fact.id
        assert model.concluded_at is not None
        assert model.from_ == ["origin"]


def test_hyperedge_multiple_sources(db):
    with db.connect() as conn:
        pid = graph_store.create_project(conn, "T", "o", "g", "crypto")
        f1 = node_store.create_fact(conn, pid, "fact one")
        f2 = node_store.create_fact(conn, pid, "fact two")
        intent = edge_store.create_intent(conn, pid, [f1.id, f2.id], "combine", "diamond")
    assert set(intent.from_) == {f1.id, f2.id}


def test_expire_workers_releases_stale_claims(db):
    with db.connect() as conn:
        pid = graph_store.create_project(conn, "T", "o", "g", "misc")
        intent = edge_store.create_intent(conn, pid, ["origin"], "x", "diamond", worker="pearl")
        # backdate heartbeat
        conn.execute(
            "UPDATE intents SET last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (intent.id,),
        )
        edge_store.expire_workers(conn, timeout=30, project_id=pid)
        row = edge_store.get_intent(conn, pid, intent.id)
        assert row["worker"] is None


def test_agents_links_reports_overlay(db):
    with db.connect() as conn:
        pid = graph_store.create_project(conn, "T", "o", "g", "web")
        graph_store.add_agent(conn, pid, "aventurine", "member", state="active", start_fact_id="origin")
        graph_store.add_link(conn, pid, "ipc", "diamond", "start")
        graph_store.add_link(conn, pid, "diamond", "aventurine", "assign")
        graph_store.create_report(
            conn, pid, "aventurine", "halfway", "high", "origin",
            ["step1", "step2"], ["try sqli"], ["sql injection"],
        )
    with db.connect() as conn:
        detail = graph_store.project_detail(conn, pid)
    assert "aventurine" in {a.name for a in detail.agents}
    assert len(detail.agent_links) == 2
    assert detail.reports[0].difficulty == "high"
    assert detail.reports[0].steps == ["step1", "step2"]
    with db.connect() as conn:
        assert graph_store.active_member_names(conn, pid) == ["aventurine"]


def test_project_summary_counts(db):
    with db.connect() as conn:
        pid = graph_store.create_project(conn, "T", "o", "g", "reverse")
        edge_store.create_intent(conn, pid, ["origin"], "open intent", "diamond")
        graph_store.add_agent(conn, pid, "aventurine", "member", state="active")
    with db.connect() as conn:
        summaries = graph_store.project_summaries(conn)
    s = summaries[0]
    assert s.fact_count == 2
    assert s.unclaimed_intent_count == 1
    assert s.member_count == 1


def test_broadcast(db):
    with db.connect() as conn:
        pid = graph_store.create_project(conn, "Pwnme", "o", "g", "pwn")
        graph_store.add_broadcast(conn, pid, "Pwnme", "flag{abc}")
    with db.connect() as conn:
        bs = graph_store.list_broadcasts(conn)
    assert bs[0].flag == "flag{abc}"
    assert bs[0].title == "Pwnme"
