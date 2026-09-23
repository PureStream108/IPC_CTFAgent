from __future__ import annotations

from psycopg.types.json import Jsonb

from backend.competition.store import CompetitionConflict, new_id
from backend.ops.models import validate_secret_name
from backend.ops.store import OpsStore


class QuestionStore:
    def __init__(self, state):
        self.state = state

    def create(self, *, session_id: str, operation_key: str, title: str,
               options: list[str] | None = None, workflow_id: str | None = None,
               secret_name: str | None = None) -> dict:
        options = options or []
        if not title.strip() or len(title) > 2000 or not operation_key or len(operation_key) > 200:
            raise ValueError("question requires a title and stable operation key")
        if len(options) > 20 or any(not isinstance(o, str) or len(o) > 500 for o in options):
            raise ValueError("invalid question options")
        if secret_name:
            secret_name = validate_secret_name(secret_name)
            if not workflow_id:
                raise ValueError("sensitive questions require a target workflow")
        scoped_key = f"{session_id}:{operation_key}"
        with self.state.db.connect() as connection:
            if not connection.execute("SELECT id FROM sessions WHERE id=%s", (session_id,)).fetchone():
                raise ValueError("question session not found")
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (scoped_key,))
            old = connection.execute("SELECT * FROM competition_questions WHERE operation_key=%s", (scoped_key,)).fetchone()
            if old:
                if (old["title"], old["options"], old["workflow_id"], old["secret_name"]) != (title, options, workflow_id, secret_name):
                    raise CompetitionConflict("question operation key has different content")
                return old
            return connection.execute(
                """INSERT INTO competition_questions
                   (id,operation_key,ops_session_id,title,options,sensitive,workflow_id,secret_name)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (new_id(), scoped_key, session_id, title, Jsonb(options), bool(secret_name), workflow_id, secret_name),
            ).fetchone()

    def list(self, session_id: str) -> list[dict]:
        with self.state.db.connect() as connection:
            return connection.execute(
                "SELECT * FROM competition_questions WHERE ops_session_id=%s AND state='pending' ORDER BY created_at", (session_id,),
            ).fetchall()

    def answer(self, question_id: str, session_id: str, value: str) -> dict:
        if not value.strip() or len(value) > 16384:
            raise ValueError("answer must contain 1 to 16384 characters")
        # Reuse the established credential store; raw sensitive answers never
        # enter the question table or conversation events.
        ops = OpsStore(self.state.root, database=self.state.db)
        with self.state.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM competition_questions WHERE id=%s AND ops_session_id=%s FOR UPDATE", (question_id, session_id),
            ).fetchone()
            if not row:
                raise KeyError(question_id)
            if row["state"] in {"answered", "consumed"}:
                return {"id": row["id"], "state": row["state"], "answer": row["answer"]}
            if row["state"] != "pending":
                raise CompetitionConflict("question is no longer pending")
            if row["sensitive"]:
                ops.save_workflow_secrets(row["workflow_id"], {row["secret_name"]: value})
                answer = {"workflow_id": row["workflow_id"], "secret_name": row["secret_name"], "stored": True}
            else:
                answer = {"text": value}
            return connection.execute(
                """UPDATE competition_questions SET answer=%s,state='answered',answered_at=now()
                   WHERE id=%s RETURNING id,state,answer""", (Jsonb(answer), question_id),
            ).fetchone()

    def result(self, question_id: str, session_id: str) -> dict:
        with self.state.db.connect() as connection:
            row = connection.execute(
                "SELECT id,state,answer FROM competition_questions WHERE id=%s AND ops_session_id=%s",
                (question_id, session_id),
            ).fetchone()
            if not row:
                raise KeyError(question_id)
            return row
