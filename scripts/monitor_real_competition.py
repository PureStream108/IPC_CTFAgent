"""Observe competition execution, anomalies and autonomous recovery.

After an explicitly requested Start, this process only reads APIs, logs and
PostgreSQL. It never stops, retries, resumes or otherwise repairs a live run.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import requests
import yaml

REQUIRED_CATEGORIES = ("web", "misc", "reverse", "crypto", "pwn", "ai")
TERMINAL = {"finished", "stopped"}
SECRET_KEYS = {
    "flag", "candidate", "normalized_flag", "password", "api_key", "token",
    "access_token", "refresh_token", "secret", "secret_value", "client_secret",
    "cookie", "set-cookie", "authorization", "x-api-key", "database_url",
    "cookies", "secrets", "credentials",
}
SUCCESSFUL_RECOVERY = {
    "session.handed_off", "session.rebuilt", "run.recovered", "run.resumed",
    "project_recovered", "tool_order_repaired",
    "instance.repaired",
}


def timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("monitor timestamps must include a timezone")
    return parsed


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Remove known credentials/flags, including inside serialized tool output."""
    if isinstance(value, dict):
        return {
            key: "[redacted]" if str(key).lower() in SECRET_KEYS else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        # Native tool messages often contain JSON inside a content string.
        if value.strip().startswith(("{", "[")):
            try:
                encoded = json.loads(value)
            except ValueError:
                pass
            else:
                return json.dumps(redact(encoded, secrets), ensure_ascii=False)
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[redacted]")
        value = re.sub(r"(?:flag|ctf)\{[^}\r\n]+\}", "[redacted-flag]", value, flags=re.IGNORECASE)
        value = re.sub(r"(://)[^/\s:@]+:[^/@\s]+@", r"\1[redacted]@", value)
        value = re.sub(
            r"(?i)((?:authorization|cookie|set-cookie|x-api-key)\s*:\s*)[^\r\n]+",
            r"\1[redacted]", value,
        )
        return re.sub(
            r"(?i)((?:api_key|access_token|token|password|secret|flag|candidate)=)[^&\s]+",
            r"\1[redacted]", value,
        )
    return value


def credential_values(root: Path) -> tuple[str, ...]:
    """Load deployment secret values for redaction; never print or export them."""
    values = {value for key, value in os.environ.items()
              if key.startswith("IPC_") and any(word in key for word in ("KEY", "TOKEN", "COOKIE", "PASSWORD")) and value}

    def collect(item, secret_context=False):
        if isinstance(item, dict):
            for key, child in item.items():
                collect(child, secret_context or str(key).lower() in SECRET_KEYS)
        elif isinstance(item, list):
            for child in item:
                collect(child, secret_context)
        elif secret_context and isinstance(item, str) and item:
            values.add(item)

    for path in (root / "backend/config/config.yaml", root / "data/ops-agent/secrets.json"):
        if path.is_file():
            collect(yaml.safe_load(path.read_text(encoding="utf-8")), path.suffix == ".json")
    return tuple(values)


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    offset = (len(ordered) - 1) * quantile
    low, high = math.floor(offset), math.ceil(offset)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (offset - low), 3)


def distribution(values: list[float]) -> dict:
    return {"count": len(values), "p50": percentile(values, 0.5),
            "p95": percentile(values, 0.95), "max": round(max(values), 3) if values else None}


def choose_challenges(challenges: list[dict[str, Any]], count: int = 20) -> list[str]:
    if count < len(REQUIRED_CATEGORIES):
        raise ValueError("challenge count must cover all required categories")
    unsolved = [item for item in challenges if not item.get("solved")]
    by_id = {str(item["external_id"]): item for item in unsolved}
    selected: list[str] = []
    for category in REQUIRED_CATEGORIES:
        candidate = next((key for key, item in by_id.items() if item.get("category") == category), None)
        if candidate is None:
            raise RuntimeError(f"workflow has no unsolved {category} challenge")
        selected.append(candidate)
    for key in sorted(by_id, key=lambda key: (str(by_id[key].get("category", "")), key)):
        if len(selected) == count:
            break
        if key not in selected:
            selected.append(key)
    if len(selected) < count:
        raise RuntimeError(f"workflow exposes only {len(by_id)} unique unsolved challenges; need {count}")
    return selected


def db_snapshot(connection, run_id: str, *, after_event=0, after_agent_event=0) -> dict[str, Any]:
    """One read-only, consistent snapshot, with incremental event collection."""
    def rows(query, params=(run_id,)):
        return [dict(row) for row in connection.execute(query, params).fetchall()]

    with connection.transaction():
        if connection.execute("SELECT 1 FROM competition_runs WHERE id=%s", (run_id,)).fetchone() is None:
            raise ValueError("run is absent from the monitoring database; verify the API and database configuration")
        events = rows("""SELECT event_id,kind,payload,created_at FROM competition_run_events
                         WHERE run_id=%s AND event_id>%s ORDER BY event_id""", (run_id, after_event))
        agent_events = rows("""SELECT e.*,s.challenge_id FROM agent_events e
                               JOIN competition_sessions s ON s.id=e.session_id
                               WHERE s.run_id=%s AND e.event_id>%s ORDER BY e.event_id""", (run_id, after_agent_event))
        challenges = rows("""SELECT c.id,c.external_id,c.project_id,c.state,c.metadata,
                                      c.first_assigned_at,c.deadline_at,c.instance_generation
                               FROM competition_challenges c JOIN competition_run_challenges rc ON rc.challenge_id=c.id
                               WHERE rc.run_id=%s ORDER BY c.id""")
        assignments = rows("""SELECT id,challenge_id,session_id,member,role,epoch,released_at
                               FROM competition_assignments WHERE run_id=%s ORDER BY id""")
        submissions = rows("""SELECT id,challenge_id,session_id,member,status,created_at,submitted_at,candidate
                               FROM competition_submissions WHERE run_id=%s ORDER BY created_at""")
        jobs = rows("""SELECT challenge_id,session_id,status,attempts,next_attempt_at,last_error
                       FROM competition_wp_jobs WHERE run_id=%s""")
        instances = rows("""SELECT challenge_id,state,owned,metadata,updated_at FROM competition_instances WHERE run_id=%s""")
        messages = rows("""SELECT count(*) AS total,
                                  count(*) FILTER (WHERE m.sequence>COALESCE(c.sequence,0)) AS backlog,
                                  count(*) FILTER (WHERE m.envelope->>'sender_member_id'<>'competition-control') AS member_messages
                           FROM competition_messages m LEFT JOIN competition_message_cursors c
                             ON c.session_id=m.envelope->>'receiver_session_id' WHERE m.run_id=%s""")[0]
        health = rows("""SELECT h.* FROM agent_session_health h JOIN competition_sessions s ON s.id=h.session_id
                          WHERE s.run_id=%s ORDER BY h.session_id""")
    return {"events": events, "agent_events": agent_events, "challenges": challenges,
            "assignment_rows": assignments, "submission_rows": submissions,
            "wp_jobs": jobs, "instance_rows": instances, "messages": messages,
            "agent_sessions": health}


def log_rows(session, base: str, project_ids: list[str], secrets: tuple[str, ...]) -> list[dict]:
    records = []
    for project_id in project_ids:
        for kind in ("project", "tool", "llm", "memory"):
            response = session.get(f"{base}/logs/{project_id}", params={"kind": kind, "limit": 500}, timeout=30)
            response.raise_for_status()
            payload = response.json()
            entries = payload if isinstance(payload, list) else payload.get("entries", [])
            for item in entries:
                records.append(redact({**item, "project_id": project_id, "log_kind": kind}, secrets))
    return records


def elapsed(start: Any, end: Any) -> float | None:
    start_time, end_time = timestamp(start), timestamp(end)
    if start_time is None or end_time is None or end_time < start_time:
        return None
    return (end_time - start_time).total_seconds()


def capacity_violations(events: list[dict], initial_limit: int) -> list[dict]:
    """Check new admissions; lowering a limit never preempts prior instances."""
    active = set()
    limit = initial_limit
    violations = []
    for event in events:
        payload = event.get("payload") or {}
        challenge_id = payload.get("challenge_id")
        if event["kind"] == "run.limits_updated" and "remote_concurrency" in payload.get("limits", {}):
            limit = int(payload["limits"]["remote_concurrency"])
        elif event["kind"] in {"instance.stopped", "instance.expired"}:
            active.discard(challenge_id)
        elif event["kind"] == "instance.started":
            if challenge_id not in active and not payload.get("repair") and len(active) >= limit:
                violations.append({"kind": "remote.capacity_exceeded", "created_at": event["created_at"],
                                   "challenge_id": challenge_id, "limit": limit, "previously_active": len(active)})
            active.add(challenge_id)
    return violations


def tool_outcome(payload: dict) -> str:
    message = payload.get("message") or {}
    raw = message.get("content", message.get("output", ""))
    try:
        output = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        output = {}
    if not isinstance(output, dict):
        return "unknown"
    status = output.get("status")
    if status in {"unknown", "cancelled"}:
        return str(status)
    if output.get("error") or output.get("ok") is False or output.get("invalid_action") or status in {"failed", "rejected", "invalid_action"}:
        return "error"
    observation = str(output.get("observation") or "")
    if re.search(r"[\{'\"]error['\"]?\s*:", observation):
        return "error"
    return "ok"


def metrics(snapshot: dict, run_events: list[dict], agent_events: list[dict], logs: list[dict], samples: list[dict]) -> dict:
    """Match timings by challenge/session/call/submission, never by first kind."""
    latency: dict[str, list[float]] = defaultdict(list)
    durations = []
    calls = {}
    results = {}
    session_events = defaultdict(list)
    for event in agent_events:
        session_events[event["session_id"]].append(event)
        payload = event.get("payload") or {}
        call_id = payload.get("id")
        if call_id and event["kind"] == "tool_call":
            calls[(event["session_id"], call_id)] = event
        if call_id and event["kind"] == "tool_result":
            results[(event["session_id"], call_id)] = event
    for key, event in calls.items():
        result = results.get(key)
        duration = elapsed(event["created_at"], result["created_at"]) if result else None
        name = (event.get("payload") or {}).get("name", "unknown")
        durations.append({"session_id": key[0], "call_id": key[1], "name": name,
                          "duration_seconds": duration,
                          "outcome": tool_outcome(result["payload"]) if result else "pending"})
        if duration is not None:
            latency["tool_call_to_result"].append(duration)
            if name == "mcp_tool":
                latency["mcp_call_to_result"].append(duration)

    allocated = {}
    member_started = {}
    submission_verdicts = {}
    for event in run_events:
        payload = event.get("payload") or {}
        key = (payload.get("challenge_id"), payload.get("session_id"))
        if event["kind"] == "assignment.started":
            allocated.setdefault(key, event["created_at"])
        if event["kind"] == "member.started":
            member_started.setdefault(key, event["created_at"])
        if event["kind"] == "submission.verdict":
            submission_verdicts.setdefault(payload.get("submission_id"), event["created_at"])
    for challenge in snapshot["challenges"]:
        duration = elapsed((challenge.get("metadata") or {}).get("ready_at"), challenge.get("first_assigned_at"))
        if duration is not None:
            latency["import_to_primary_assignment"].append(duration)
            if (challenge.get("metadata") or {}).get("remote"):
                latency["remote_queue"].append(duration)
    for key, start in allocated.items():
        duration = elapsed(start, member_started.get(key))
        if duration is not None:
            latency["assignment_to_member_start"].append(duration)
        events = session_events[key[1]]
        first_response = next((event for event in events if event["kind"] == "status"
                               and event["payload"].get("status") == "provider.first_response"), None)
        if first_response:
            duration = elapsed(start, first_response["created_at"])
            if duration is not None:
                latency["assignment_to_first_provider_response"].append(duration)
        for event in events:
            if event["kind"] == "status" and event["payload"].get("status") == "provider.first_response":
                duration = event["payload"].get("latency_seconds")
                if isinstance(duration, (int, float)):
                    latency["provider_time_to_first_response"].append(duration)
    for submission in snapshot["submission_rows"]:
        duration = elapsed(submission.get("submitted_at"), submission_verdicts.get(submission["id"]))
        if duration is not None:
            latency["flag_submit_to_verdict"].append(duration)
    for row in logs:
        if row.get("event") == "member_start":
            challenge = next((item for item in snapshot["challenges"] if item.get("project_id") == row.get("project_id")), None)
            if challenge:
                duration = elapsed(challenge.get("first_assigned_at"), row.get("ts"))
                if duration is not None:
                    latency["assignment_to_logged_member_start"].append(duration)

    assignments = snapshot["assignment_rows"]
    active = [row for row in assignments if row.get("released_at") is None]
    resolved_mcp = [row for row in durations if row["name"] == "mcp_tool" and row["outcome"] != "pending"]
    kinds = Counter(event["kind"] for event in run_events)
    categories = defaultdict(Counter)
    for challenge in snapshot["challenges"]:
        categories[(challenge.get("metadata") or {}).get("category", "misc")][challenge["state"]] += 1
    remote_samples = [row["resource_capacity"].get("remote", {}) for row in samples]
    return {
        "latency": {name: distribution(latency.get(name, [])) for name in (
            "import_to_primary_assignment", "assignment_to_member_start",
            "assignment_to_logged_member_start", "assignment_to_first_provider_response",
            "provider_time_to_first_response", "tool_call_to_result", "mcp_call_to_result",
            "flag_submit_to_verdict", "remote_queue",
        )},
        "tool_calls": durations,
        "mcp": {"calls": sum(row["name"] == "mcp_tool" for row in durations),
                "resolved": len(resolved_mcp), "errors": sum(row["outcome"] == "error" for row in resolved_mcp),
                "unknown": sum(row["outcome"] == "unknown" for row in resolved_mcp),
                "error_rate": (sum(row["outcome"] == "error" for row in resolved_mcp) / len(resolved_mcp)) if resolved_mcp else None},
        "containers": {action: {"attempts": kinds[f"instance.{action}_requested"], "completed": kinds[f"instance.{completed}"]}
                       for action, completed in (("start", "started"), ("renew", "renewed"), ("rebuild", "rebuilt"), ("stop", "stopped"))},
        "remote_capacity": {"max_used": max((item.get("used", 0) for item in remote_samples), default=0), "samples": remote_samples},
        "assignments": {"total": len(assignments), "active": len(active), "released": len(assignments) - len(active),
                        "active_wp": sum(row["role"] == "wp" for row in active),
                        "helpers": sum(row["role"] == "helper" for row in assignments)},
        "submissions": dict(Counter(row["status"] for row in snapshot["submission_rows"])),
        "wp": dict(Counter(row["status"] for row in snapshot["wp_jobs"])),
        "instances": dict(Counter(row["state"] for row in snapshot["instance_rows"])),
        "collaboration_backlog": int(snapshot["messages"].get("backlog", 0)),
        "collaboration": snapshot["messages"],
        "states": dict(Counter(row["state"] for row in snapshot["challenges"])),
        "category_states": {name: dict(values) for name, values in categories.items()},
    }


def acceptance_checks(snapshot: dict, measured: dict, status: str, observations: list[dict], run_events=()) -> dict:
    def check(ok, evidence, pending=False):
        return {"status": "in_progress" if pending else "pass" if ok else "fail", "evidence": evidence}
    assignments = measured["assignments"]
    terminal = status in TERMINAL
    remote_ids = {row["id"] for row in snapshot["challenges"] if (row.get("metadata") or {}).get("remote")}
    target_missing = []
    for session_id in {row["session_id"] for row in snapshot["assignment_rows"] if row["challenge_id"] in remote_ids and row["role"] != "wp"}:
        events = sorted((row for row in snapshot["agent_events"] if row["session_id"] == session_id), key=lambda row: row["sequence"])
        first_tool = next((row["sequence"] for row in events if row["kind"] == "tool_call"), None)
        target = next((row["sequence"] for row in events if row["event_key"].startswith("recon:")
                       and "target_instance" in json.dumps(row["payload"])), None)
        if first_tool is not None and (target is None or target >= first_tool):
            target_missing.append(session_id)
    quiet = [row for row in observations if row["kind"] == "agent.no_events_for_five_minutes"]
    capacity_failures = [row for row in observations if row["kind"] == "remote.capacity_exceeded"]
    active_jobs = sum(row["status"] not in {"done", "failed"} for row in snapshot["wp_jobs"])
    lifecycle_issues = []
    active_instances = set()
    rebuilds = defaultdict(list)
    for row in run_events:
        challenge_id = (row.get("payload") or {}).get("challenge_id")
        if row["kind"] == "instance.started":
            active_instances.add(challenge_id)
        elif row["kind"] == "instance.expired":
            active_instances.discard(challenge_id)
        elif row["kind"] == "instance.stopped":
            if challenge_id not in active_instances:
                lifecycle_issues.append({"kind": "instance.duplicate_stop", "challenge_id": challenge_id})
            active_instances.discard(challenge_id)
        elif row["kind"] == "instance.rebuilt":
            at = timestamp(row["created_at"])
            rebuilds[challenge_id] = [previous for previous in rebuilds[challenge_id] if (at - previous).total_seconds() < 60]
            rebuilds[challenge_id].append(at)
            if len(rebuilds[challenge_id]) > 3:
                lifecycle_issues.append({"kind": "instance.rebuild_storm", "challenge_id": challenge_id})
    return {
        "terminal_run": check(terminal, status, not terminal),
        "remote_capacity": check(not capacity_failures, capacity_failures),
        "target_before_first_tool": check(not target_missing, {"missing_sessions": target_missing}),
        "no_five_minute_silent_seat": check(not quiet, quiet),
        "assignment_cleanup": check(assignments["active"] == 0, assignments, not terminal),
        "wp_cleanup": check(active_jobs == 0 and assignments["active_wp"] == 0,
                            {"outstanding_jobs": active_jobs, "active_wp": assignments["active_wp"]}, not terminal),
        "collaboration_cleanup": check(measured["collaboration_backlog"] == 0, measured["collaboration_backlog"], not terminal),
        "wp_retry_bound": check(all(row["attempts"] <= 5 for row in snapshot["wp_jobs"]),
                                {row["challenge_id"]: row["attempts"] for row in snapshot["wp_jobs"]}),
        "container_lifecycle": check(not lifecycle_issues, lifecycle_issues),
    }


def markdown_report(report: dict[str, Any]) -> str:
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    final = report.get("final_resources") or {}
    measured = report.get("metrics") or final
    assignments = measured.get("assignments") or {}
    lines = ["# Competition monitor report", "", f"Run: `{cell(report.get('run_id', ''))}`",
             f"Status: `{cell(report.get('status', 'unknown'))}`", "",
             f"Observation duration: {report.get('duration_seconds')} seconds.",
             f"Self-recovery confirmed by later progress: {bool(report.get('self_recovery_observed'))}.", "",
             "## Latency (seconds)", "", "| metric | count | p50 | p95 | max |", "| --- | ---: | ---: | ---: | ---: |"]
    for name, values in sorted((report.get("latency") or {}).items()):
        lines.append(f"| {cell(name)} | {values.get('count', '-')} | {values.get('p50')} | {values.get('p95')} | {values.get('max')} |")
    lines.extend(["", "## Final resources", "", f"Assignments: {json.dumps(assignments, ensure_ascii=False)}.",
                  f"WP: {json.dumps(measured.get('wp', {}), ensure_ascii=False)}.",
                  f"States by category: {json.dumps(measured.get('category_states', {}), ensure_ascii=False)}.",
                  f"Containers: {json.dumps(measured.get('containers', {}), ensure_ascii=False)}.",
                  f"Collaboration backlog: {measured.get('collaboration_backlog', 0)}.", "",
                  "## Acceptance", "", "| invariant | result |", "| --- | --- |"])
    for name, result in sorted((report.get("checks") or {}).items()):
        lines.append(f"| {cell(name)} | {cell(result['status'])} |")
    lines.extend(["", "## Observed anomalies and recovery", "", "| type | event | time |", "| --- | --- | --- |"])
    for key in ("anomalies", "recoveries"):
        for row in report.get(key) or []:
            lines.append(f"| {key} | {cell(row.get('kind', 'unknown'))} | {cell(row.get('created_at', ''))} |")
    lines.extend(["", "Unavailable timing is reported with count 0 and null percentiles; it is not treated as zero latency.",
                  "The monitor made no remediation calls. A duration limit ends observation and leaves a live run running."])
    return "\n".join(lines) + "\n"


def export_report(report: dict, export: Path, secrets=()) -> tuple[Path, Path]:
    export.mkdir(parents=True, exist_ok=True)
    safe = redact(report, secrets)
    # run_id comes from a server/CLI; do not permit arbitrary output paths.
    run_id = str(report["run_id"])
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", run_id):
        raise ValueError("invalid run id for export")
    base = export / f"real_competition_{run_id}_monitor"
    json_path, markdown_path = base.with_suffix(".json"), base.with_suffix(".md")
    contents = {".json": json.dumps(safe, ensure_ascii=False, indent=2, default=str), ".md": markdown_report(safe)}
    for suffix, content in contents.items():
        # Preserve the original plan's acceptance filenames as well as the
        # explicit monitor filenames. Replace each export atomically so a
        # crash during sampling leaves the preceding report readable.
        for target in (base.with_suffix(suffix), export / f"real_competition_{run_id}{suffix}"):
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(target)
    return json_path, markdown_path


def monitor(base_url: str, run_id: str, duration: float, database_url: str, *, export=Path("data/exports"),
            secrets=(), poll_interval=5.0, session=None, selection=None, drain_timeout=600.0) -> dict[str, Any]:
    session = session or requests.Session()
    base = base_url.rstrip("/")
    started = time.monotonic()
    samples, anomalies, recoveries, events, agent_events, logs = [], [], [], [], [], []
    after_event = after_agent = 0
    seen_logs, seen_anomalies = set(), set()
    snapshot = None
    report = None
    status = "unknown"
    observed_until = None
    export.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            response = session.get(f"{base}/api/runs/{run_id}", timeout=30)
            response.raise_for_status()
            run_view = response.json()
            # Each sample gets a fresh read-only connection, so a PostgreSQL
            # restart can recover observation without restarting any Agent.
            with psycopg.connect(database_url, row_factory=psycopg.rows.dict_row, autocommit=True) as connection:
                connection.execute("SET default_transaction_read_only TO on")
                connection.execute("SET default_transaction_isolation TO 'repeatable read'")
                connection.execute("SET statement_timeout TO 30000")
                snapshot = db_snapshot(connection, run_id, after_event=after_event, after_agent_event=after_agent)
            secrets = tuple(set(secrets) | {row["candidate"] for row in snapshot["submission_rows"] if row.get("candidate")})
            for key, target in (("events", events), ("agent_events", agent_events)):
                target.extend(snapshot[key])
            after_event = max((row["event_id"] for row in events), default=0)
            after_agent = max((row["event_id"] for row in agent_events), default=0)
            snapshot["events"], snapshot["agent_events"] = events, agent_events
            status = run_view["run"]["status"]
            ends_at = timestamp((run_view["run"].get("config_snapshot") or {}).get("workflow_spec", {}).get("ends_at"))
            if duration <= 0 and ends_at is None:
                raise ValueError("workflow has no deadline; specify --max-duration")
            observed_until = (ends_at + timedelta(seconds=drain_timeout)) if ends_at and duration <= 0 else None
            current_logs = log_rows(session, base, [row["project_id"] for row in snapshot["challenges"] if row.get("project_id")], secrets)
            for row in current_logs:
                key = json.dumps(row, sort_keys=True, default=str)
                if key not in seen_logs:
                    seen_logs.add(key)
                    logs.append(row)
            sample = {"at": datetime.now(timezone.utc).isoformat(), "status": status,
                      "resource_capacity": run_view.get("resource_capacity", {}),
                      "agent_sessions": snapshot["agent_sessions"]}
            samples.append(sample)
            remote_capacity = sample["resource_capacity"].get("remote") or {}
            if remote_capacity.get("source") not in {None, "unreported"} and remote_capacity.get("used", 0) > remote_capacity.get("limit", 0):
                # A lower hot-updated cap may intentionally grandfather
                # existing tasks. Record the observation for review.
                anomalies.append({"kind": "remote.usage_above_current_limit", "created_at": sample["at"],
                                  "capacity": remote_capacity, "requires_admission_review": True})
            initial_limit = (run_view["run"].get("config_snapshot") or {}).get("limits_snapshot", {}).get("remote_concurrency")
            if initial_limit is not None:
                for violation in capacity_violations(events, int(initial_limit)):
                    key = (violation["kind"], violation["challenge_id"], str(violation["created_at"]))
                    if key not in seen_anomalies:
                        seen_anomalies.add(key)
                        anomalies.append(violation)
            occupied = {row["session_id"] for row in snapshot["assignment_rows"] if row.get("released_at") is None}
            for row in snapshot["agent_sessions"]:
                silence = elapsed(row.get("last_event_at") or row.get("last_progress_at"), sample["at"])
                if row["session_id"] in occupied and silence is not None and silence >= 300:
                    key = ("agent.no_events_for_five_minutes", row["session_id"])
                    if key not in seen_anomalies:
                        seen_anomalies.add(key)
                        anomalies.append({"kind": key[0], "session_id": key[1], "created_at": sample["at"], "silent_seconds": silence})
            measured = metrics(snapshot, events, agent_events, logs, samples)
            checks = acceptance_checks(snapshot, measured, status, anomalies, events)
            records = [*events, *agent_events, *logs]
            for row in records:
                kind = str(row.get("kind", row.get("event", "")))
                payload = row.get("payload") or {}
                signal = payload.get("status") if kind == "status" else kind
                if signal in SUCCESSFUL_RECOVERY:
                    key = ("recovery", row.get("session_id"), row.get("event_id"), kind, str(row.get("created_at", row.get("ts"))))
                    if key not in seen_anomalies:
                        seen_anomalies.add(key)
                        recoveries.append({**row, "kind": signal})
                if kind == "error" or any(word in kind for word in ("failed", "stalled", "blocked", "lost")):
                    key = (kind, row.get("session_id"), row.get("event_id"), str(row.get("created_at", row.get("ts"))))
                    if key not in seen_anomalies:
                        seen_anomalies.add(key)
                        anomalies.append(row)
            confirmed_recoveries = []
            for recovery in recoveries:
                payload = recovery.get("payload") or {}
                recovered_session = recovery.get("session_id") or payload.get("session_id")
                recovered_challenge = recovery.get("challenge_id") or payload.get("challenge_id")
                if not recovered_challenge and recovery.get("project_id"):
                    recovered_challenge = next((row["id"] for row in snapshot["challenges"]
                                                if row.get("project_id") == recovery["project_id"]), None)
                recovered_at = recovery.get("created_at", recovery.get("ts"))
                run_recovery = str(recovery.get("kind", "")).startswith("run.")
                later = [row for row in agent_events if row["kind"] == "tool_result" and tool_outcome(row["payload"]) == "ok"
                         and ((recovered_session and row["session_id"] == recovered_session)
                              or (recovered_challenge and row.get("challenge_id") == recovered_challenge)
                              or run_recovery)
                         and elapsed(recovered_at, row["created_at"]) is not None
                         and elapsed(recovered_at, row["created_at"]) > 0]
                if later:
                    confirmed_recoveries.append({"kind": recovery["kind"], "session_id": recovered_session,
                                                 "progress_event_id": later[0]["event_id"]})
            report = {"run_id": run_id, "status": status, "selection": selection or [],
                      "duration_seconds": round(time.monotonic() - started, 3),
                      "latency": measured["latency"], "metrics": measured, "samples": samples,
                      "anomalies": anomalies, "recoveries": recoveries,
                      "confirmed_recoveries": confirmed_recoveries,
                      "self_recovery_observed": bool(confirmed_recoveries), "checks": checks,
                      "final_resources": {**measured, "agent_sessions": snapshot["agent_sessions"]},
                      "run_events": events, "agent_events": agent_events, "logs": logs,
                      "read_only_observation": True, "termination_reason": "observing"}
            report["model_mode"] = (run_view["run"].get("config_snapshot") or {}).get("member_config", {}).get("api_format", "unknown")
            if selection:
                categories = {row.get("metadata", {}).get("category") for row in snapshot["challenges"]}
                report["checks"]["twenty_challenge_selection"] = {
                    "status": "pass" if len(selection) == len(snapshot["challenges"]) == 20 and set(REQUIRED_CATEGORIES).issubset(categories) else "fail",
                    "evidence": {"selected": len(selection), "imported": len(snapshot["challenges"]), "categories": sorted(categories)},
                }
            now = datetime.now(timezone.utc)
            reason = "terminal_run" if status in TERMINAL else "max_duration" if duration > 0 and time.monotonic() - started >= duration else "deadline_drain_timeout" if observed_until and now >= observed_until else None
            report["termination_reason"] = reason or "observing"
            export_report(report, export, secrets)
            print(json.dumps({"run_id": run_id, "status": status, "samples": len(samples),
                              "active_assignments": measured["assignments"]["active"],
                              "anomalies": len(anomalies), "recoveries": len(confirmed_recoveries)}), flush=True)
            if reason:
                return report
        except (requests.RequestException, psycopg.Error) as exc:
            error = {"kind": "monitor.collection_failed", "created_at": datetime.now(timezone.utc).isoformat(),
                     "error": redact(str(exc), secrets)}
            anomalies.append(error)
            partial = {**(report or {}), "run_id": run_id, "status": status, "anomalies": anomalies,
                       "termination_reason": "monitor_error", "read_only_observation": True,
                       "duration_seconds": round(time.monotonic() - started, 3)}
            export_report(partial, export, secrets)
            if duration > 0 and time.monotonic() - started >= duration:
                raise
        wait = min(poll_interval, max(0.01, duration - (time.monotonic() - started))) if duration > 0 else poll_interval
        time.sleep(wait)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("IPC_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--workflow-id", help="confirmed workflow to use; required if more than one is confirmed")
    parser.add_argument("--run-id", help="observe an existing run without starting any work")
    parser.add_argument("--max-duration", type=float, default=0.0, help="observation time limit; default follows workflow deadline; does not stop the run")
    parser.add_argument("--live-run", action="store_true", help="acknowledge a new real competition run and submissions")
    parser.add_argument("--database-url", default=os.environ.get("IPC_DATABASE_URL", ""))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--export-dir", type=Path, default=Path("data/exports"))
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or IPC_DATABASE_URL is required")
    if args.max_duration < 0:
        parser.error("--max-duration must be nonnegative")
    selection = []
    session = requests.Session()
    base = args.base_url.rstrip("/")
    if args.live_run and not args.run_id:
        response = session.get(f"{base}/api/ops/workflows", timeout=30)
        response.raise_for_status()
        confirmed = [row for row in response.json().get("workflows", []) if row.get("status") == "confirmed"]
        if args.workflow_id:
            confirmed = [row for row in confirmed if row["id"] == args.workflow_id]
        if len(confirmed) != 1:
            parser.error("select exactly one confirmed workflow with --workflow-id")
        workflow = confirmed[0]
        spec = workflow.get("spec") or {}
        if args.max_duration == 0 and not spec.get("ends_at"):
            parser.error("workflow has no deadline; specify --max-duration before Start")
        response = session.post(f"{base}/api/workflows/{workflow['id']}/preflight", timeout=60)
        response.raise_for_status()
        selection = choose_challenges(response.json().get("challenges", []))
        # Verify durable observation before initiating real work. A wrong DSN
        # must fail before Start, not leave a live run without its monitor.
        with psycopg.connect(args.database_url, row_factory=psycopg.rows.dict_row, autocommit=True) as connection:
            connection.execute("SET default_transaction_read_only TO on")
            stored = connection.execute("SELECT status FROM workflows WHERE id=%s", (workflow["id"],)).fetchone()
            if stored is None or stored["status"] != "confirmed":
                parser.error("monitoring database has no matching confirmed workflow; Start was not called")
        response = session.post(f"{base}/api/workflows/{workflow['id']}/start",
                                json={"idempotency_key": f"monitor-{uuid4().hex}", "select": selection}, timeout=120)
        response.raise_for_status()
        args.run_id = response.json()["run"]["id"]
    if not args.run_id:
        parser.error("--run-id is required unless --live-run is used")
    report = monitor(base, args.run_id, args.max_duration, args.database_url, export=args.export_dir,
                     secrets=credential_values(args.root), session=session, selection=selection)
    if any(row["status"] == "fail" for row in report.get("checks", {}).values()):
        return 1
    return 0 if report["status"] in TERMINAL else 2


if __name__ == "__main__":
    raise SystemExit(main())
