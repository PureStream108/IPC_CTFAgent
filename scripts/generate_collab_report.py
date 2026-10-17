#!/usr/bin/env python3
from __future__ import annotations

import html as html_mod
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "data" / "artifacts" / "logs"
OUT_HTML = ROOT / "data" / "exports" / "multi_agent_collab_report.html"
OUT_JSON = ROOT / "data/exports/multi_agent_collab_report.json"
DSN = "postgresql+psycopg://ipc:ipc@127.0.0.1:15432/ipc"

RUN_LABELS = {
    "77a5173f5a044ef8b62ed2b2b9271cd4": "R1 启动尝试",
    "0793c69d65bd402eb5796551ae66cff2": "R2 启动尝试",
    "377b15dc874f4ee0b1ccee17c64fbbf7": "R3 主 run·65 题",
    "4a7f01c88a754466b79b6363c608eabe": "R4 PWN 专项·7 题",
    "27a2e167b61b4e9fafdce67faa698926": "R5 复测·20 题",
}


def ts(dt):
    return dt.timestamp() if dt else None


def extract():
    eng = create_engine(DSN)
    data = {"generated_at": datetime.now(timezone.utc).isoformat()}
    with eng.connect() as c:
        runs = []
        for r in c.execute(text(
            "select id, status, created_at, updated_at, config_snapshot from competition_runs order by created_at"
        )):
            cs = r[4] or {}
            mc = cs.get("member_config") or {}
            runs.append({
                "id": r[0], "label": RUN_LABELS.get(r[0], r[0][:8]),
                "status": r[1], "created_at": ts(r[2]), "updated_at": ts(r[3]),
                "model": mc.get("model"), "reasoning_effort": mc.get("reasoning_effort"),
                "n_selected": len(cs.get("selected_external_ids") or []),
            })
        data["runs"] = runs

        chals = {}
        for r in c.execute(text(
            "select id, run_id, external_id, state, metadata, first_assigned_at, "
            "wrong_count, instance_generation, project_id from competition_challenges"
        )):
            md = r[4] or {}
            chals[r[0]] = {
                "id": r[0], "run_id": r[1], "external_id": r[2], "state": r[3],
                "title": md.get("title") or r[2], "category": (md.get("category") or "misc").lower(),
                "remote": bool(md.get("remote")), "attachment_count": md.get("attachment_count") or 0,
                "first_assigned_at": ts(r[5]), "wrong_count": r[6] or 0,
                "instance_generation": r[7] or 0, "project_id": r[8],
            }
        data["challenges"] = chals

        subs = []
        for r in c.execute(text(
            "select run_id, challenge_id, member, status, created_at, submitted_at "
            "from competition_submissions order by created_at"
        )):
            subs.append({"run_id": r[0], "challenge_id": r[1], "member": r[2],
                         "status": r[3], "created_at": ts(r[4]), "submitted_at": ts(r[5])})
        data["submissions"] = subs

        assigns = defaultdict(list)
        seen = set()
        for r in c.execute(text(
            "select run_id, challenge_id, member, role from competition_assignments"
        )):
            key = (r[0], r[1], r[2], r[3])
            if key in seen:
                continue
            seen.add(key)
            assigns[r[0]].append({"challenge_id": r[1], "member": r[2], "role": r[3]})
        data["assignments"] = {k: v for k, v in assigns.items()}

        msgs = []
        for r in c.execute(text("select run_id, challenge_id, envelope, status, created_at, sequence from competition_messages order by sequence")):
            env = r[2] or {}
            payload = env.get("payload") or {}
            msgs.append({
                "run_id": r[0], "challenge_id": r[1], "status": r[3], "created_at": ts(r[4]),
                "sender": env.get("sender_member_id") or "competition-control",
                "receiver": env.get("receiver_member_id"),
                "message_type": env.get("message_type"),
                "entry": payload.get("entry"), "generation": payload.get("instance_generation"),
                "external_id": payload.get("external_id"), "deadline_at": env.get("deadline_at"),
                "sequence": r[5], "request_id": env.get("request_id"),
                "sender_session_id": env.get("sender_session_id"), "receiver_session_id": env.get("receiver_session_id"),
                "lease_epoch": env.get("lease_epoch"),
            })
        data["messages"] = msgs

        consumed = []
        for r in c.execute(text(
            "select payload, created_at from competition_run_events where kind='competition.message_consumed'"
        )):
            p = r[0] or {}
            consumed.append({"session_id": p.get("session_id"), "cursor": p.get("cursor"),
                             "previous": p.get("previous"), "t": ts(r[1])})
        data["message_consumed_events"] = consumed

        keep_kinds = {
            "run.created", "run.started", "run.running", "run.paused", "run.stopped",
            "run.finished", "run.blocked", "run.recovered", "run.draining", "run.sync_failed",
            "assignment.started", "assignment.helper_intent", "assignment.stalled", "assignment.recovered",
            "member.started", "submission.verdict",
            "wp.started", "wp.completed", "wp.failed", "wp.deferred",
            "instance.start_requested", "instance.started", "instance.rebuilt",
            "instance.rebuild_requested", "instance.rebuild_failed",
            "instance.renew_requested", "instance.renew_failed",
            "instance.stopped", "session.diagnose", "engine.conflict",
            "job.lost", "job.swept_lost",
        }
        timeline = defaultdict(list)
        expired_count = Counter()
        for r in c.execute(text(
            "select run_id, kind, payload, created_at from competition_run_events order by event_id"
        )):
            run_id, kind, payload, cat = r[0], r[1], r[2] or {}, ts(r[3])
            if kind == "challenge.expired":
                expired_count[run_id] += 1
                continue
            if kind not in keep_kinds:
                continue
            entry = {"t": cat, "kind": kind}
            for f in ("challenge_id", "external_id", "member", "role", "status", "verdict",
                      "correct", "error", "reason", "generation", "entry", "title", "category"):
                if f in payload and payload[f] is not None:
                    entry[f] = payload[f] if not isinstance(payload[f], str) else payload[f][:200]
            timeline[run_id].append(entry)
        data["timeline"] = dict(timeline)
        data["expired_events_per_run"] = dict(expired_count)

        sessions = {}
        for r in c.execute(text(
            "select id, run_id, challenge_id, member, state, created_at from competition_sessions"
        )):
            sessions[r[0]] = {"run_id": r[1], "challenge_id": r[2], "member": r[3],
                              "state": r[4], "created_at": ts(r[5])}
        data["sessions"] = sessions

        turns = []
        for r in c.execute(text(
            "select t.session_id, t.started_at, t.finished_at, t.prompt_tokens, "
            "t.output_tokens, t.reasoning_tokens, t.status from agent_turns t"
        )):
            turns.append({"session_id": r[0], "t0": ts(r[1]), "t1": ts(r[2]),
                          "prompt": r[3] or 0, "output": r[4] or 0, "reasoning": r[5] or 0,
                          "status": r[6]})
        data["turns"] = turns

        data["ops_session_count"] = c.execute(text(
            "select count(*) from agent_sessions where kind='ops'")).scalar()

        intents = []
        for r in c.execute(text(
            "select project_id, creator, worker, description, created_at, concluded_at, retry_count "
            "from intents order by created_at"
        )):
            intents.append({"project_id": r[0], "creator": r[1], "worker": r[2],
                            "description": (r[3] or "")[:160],
                            "created_at": ts(r[4]), "concluded_at": ts(r[5]),
                            "retry_count": r[6] or 0})
        data["intents"] = intents

        facts = []
        for r in c.execute(text("select project_id, description, created_at from facts order by created_at")):
            facts.append({"project_id": r[0], "description": (r[1] or "")[:160], "created_at": ts(r[2])})
        data["facts"] = facts
        mems = []
        for r in c.execute(text("select category, title, source, created_at from memories order by created_at")):
            mems.append({"category": r[0], "title": (r[1] or "")[:120], "source": r[2], "created_at": ts(r[3])})
        data["memories"] = mems

        wp = []
        for r in c.execute(text(
            "select run_id, challenge_id, member, status, attempts, last_error from competition_wp_jobs"
        )):
            wp.append({"run_id": r[0], "challenge_id": r[1], "member": r[2], "status": r[3],
                       "attempts": r[4] or 0, "last_error": (r[5] or "")[:120]})
        data["wp_jobs"] = wp

        flags = []
        for r in c.execute(text(
            "select project_id, source, status, created_at from flag_submissions order by created_at"
        )):
            flags.append({"project_id": r[0], "source": r[1], "status": r[2], "created_at": ts(r[3])})
        data["flag_submissions"] = flags

        jobs = []
        for r in c.execute(text(
            "select session_id, kind, command, state, exit_code, started_at, finished_at from agent_jobs"
        )):
            jobs.append({"session_id": r[0], "kind": r[1], "command": (r[2] or "")[:120],
                         "state": r[3], "exit_code": r[4], "t0": ts(r[5]), "t1": ts(r[6])})
        data["agent_jobs"] = jobs

        data["ui_event_kinds"] = {r[0]: r[1] for r in c.execute(text(
            "select kind, count(*) from events group by kind"))}
    return data


def scan_artifact_logs():
    """Aggregate per-challenge JSONL logs: event counts by type/member."""
    out = {"files": {}, "tool_events": Counter(), "tool_events_by_member": Counter(),
           "llm_calls": 0, "total_lines": 0}
    for sub in ("tool_logs", "llm_logs", "memory_logs", "project_logs"):
        d = LOGS_DIR / sub
        files = list(d.glob("*.jsonl")) if d.is_dir() else []
        out["files"][sub] = len(files)
    d = LOGS_DIR / "tool_logs"
    if d.is_dir():
        for f in d.glob("*.jsonl"):
            try:
                with f.open(encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        out["total_lines"] += 1
                        try:
                            rec = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        ev = rec.get("event")
                        if ev:
                            out["tool_events"][ev] += 1
                            m = rec.get("member")
                            if m:
                                out["tool_events_by_member"][m] += 1
            except OSError:
                continue
    out["tool_events"] = dict(out["tool_events"].most_common(25))
    out["tool_events_by_member"] = dict(out["tool_events_by_member"].most_common())
    return out


def main_extract():
    print("extracting from db ...", flush=True)
    data = extract()
    print("scanning artifact logs ...", flush=True)
    data["artifact_logs"] = scan_artifact_logs()
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {OUT_JSON} ({OUT_JSON.stat().st_size} bytes)")


if __name__ == "__main__" and "--render" not in sys.argv:
    main_extract()



CAT_COLORS = {
    "pwn": "#cf222e", "crypto": "#8250df", "web": "#0969da", "misc": "#1a7f37",
    "reverse": "#9a6700", "ai": "#bf3989", "blockchain": "#1b7c83",
    "forensics": "#0a7ea4", "osint": "#bc4c00", "eth": "#1b7c83",
}
MEMBER_PALETTE = ["#0969da", "#cf222e", "#1a7f37", "#9a6700", "#8250df",
                  "#bf3989", "#1b7c83", "#bc4c00", "#2da44e", "#6e40c9", "#0a7ea4", "#8c4600"]


def fmt_dur(s):
    if s is None:
        return "-"
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    m = round(s / 60)
    return f"{m // 60}h{m % 60:02d}m"


def fmt_ts(t):
    if t is None:
        return "-"
    return datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%M:%S")


def pct(vals, q):
    if not vals:
        return None
    vals = sorted(vals)
    k = (len(vals) - 1) * q
    f, cc = math.floor(k), math.ceil(k)
    if f == cc:
        return vals[int(k)]
    return vals[f] + (vals[cc] - vals[f]) * (k - f)


def hist_buckets(vals, edges):
    buckets = [0] * (len(edges) + 1)
    for v in vals:
        for i, e in enumerate(edges):
            if v < e:
                buckets[i] += 1
                break
        else:
            buckets[-1] += 1
    labels = [f"<{edges[0]}s"] + [f"{edges[i]}-{edges[i+1]}s" for i in range(len(edges) - 1)] + [f">={edges[-1]}s"]
    return {"labels": labels, "counts": buckets}


def analyze(d):
    A = {}
    runs = {r["id"]: r for r in d["runs"]}
    chals = d["challenges"]
    sessions = d["sessions"]

    solved_at, solved_by = {}, {}
    for s in d["submissions"]:
        if s["status"] == "correct" and s["challenge_id"] not in solved_at:
            solved_at[s["challenge_id"]] = s["submitted_at"] or s["created_at"]
            solved_by[s["challenge_id"]] = s["member"]

    ch_members = defaultdict(list)
    for items in d["assignments"].values():
        for a in items:
            ch_members[a["challenge_id"]].append(a)

    for cid, ch in chals.items():
        ch["solved_at"] = solved_at.get(cid)
        ch["solved_by"] = solved_by.get(cid)
        ch["solve_seconds"] = (ch["solved_at"] - ch["first_assigned_at"]) if (
            ch["solved_at"] and ch["first_assigned_at"]) else None
        mems = ch_members.get(cid, [])
        ch["members"] = mems
        prim = [m["member"] for m in mems if m["role"] == "primary"]
        ch["primary"] = prim[0] if prim else None

    run_stats = {}
    for rid, run in runs.items():
        rch = [c for c in chals.values() if c["run_id"] == rid]
        solved = [c for c in rch if c["state"] == "solved"]
        subs = [s for s in d["submissions"] if s["run_id"] == rid]
        cats = Counter(c["category"] for c in rch)
        t0, t1 = run["created_at"], run["updated_at"]
        members = set()
        for a in d["assignments"].get(rid, []):
            members.add(a["member"])
        rebuilds = sum(1 for e in d["timeline"].get(rid, []) if e["kind"] == "instance.rebuilt")
        renew_fail = sum(1 for e in d["timeline"].get(rid, []) if e["kind"] == "instance.renew_failed")
        solve_times = [c["solve_seconds"] for c in solved if c["solve_seconds"]]
        run_stats[rid] = {
            "n_challenges": len(rch), "n_solved": len(solved),
            "n_correct_subs": sum(1 for s in subs if s["status"] == "correct"),
            "n_wrong_subs": sum(1 for s in subs if s["status"] == "wrong"),
            "cats": dict(cats), "duration_s": (t1 - t0) if (t0 and t1) else None,
            "members": sorted(members), "rebuilds": rebuilds, "renew_failed": renew_fail,
            "solve_t_avg": statistics.mean(solve_times) if solve_times else None,
            "solve_t_p50": pct(solve_times, 0.5),
            "solve_t_min": min(solve_times) if solve_times else None,
            "solve_t_max": max(solve_times) if solve_times else None,
            "expired_events": d["expired_events_per_run"].get(rid, 0),
        }
    A["run_stats"] = run_stats

    member_stats = defaultdict(lambda: {"primary": 0, "helper": 0, "wp": 0, "flags": 0,
                                        "turns": 0, "prompt": 0, "output": 0, "reasoning": 0,
                                        "turn_time_s": 0.0, "tool_events": 0})
    for items in d["assignments"].values():
        for a in items:
            member_stats[a["member"]][a["role"]] += 1
    for s in d["submissions"]:
        if s["status"] == "correct" and s["member"]:
            member_stats[s["member"]]["flags"] += 1
    sess_member = {sid: s["member"] for sid, s in sessions.items()}
    for t in d["turns"]:
        m = sess_member.get(t["session_id"])
        if not m:
            continue
        ms = member_stats[m]
        ms["turns"] += 1
        ms["prompt"] += t["prompt"]
        ms["output"] += t["output"]
        ms["reasoning"] += t["reasoning"]
        if t["t0"] and t["t1"]:
            ms["turn_time_s"] += t["t1"] - t["t0"]
    for m, n in d["artifact_logs"].get("tool_events_by_member", {}).items():
        member_stats[m]["tool_events"] = n
    member_stats = defaultdict(dict, {m: st for m, st in member_stats.items()
                                            if st["primary"] or st["helper"] or st["wp"] or st["flags"]})
    A["member_stats"] = dict(member_stats)

    members_sorted = sorted(member_stats, key=lambda m: -member_stats[m]["flags"])
    A["member_colors"] = {m: MEMBER_PALETTE[i % len(MEMBER_PALETTE)] for i, m in enumerate(members_sorted)}
    A["members_sorted"] = members_sorted

    intents = d["intents"]
    cross = [i for i in intents if i["worker"] and i["creator"] != i["worker"]]
    self_i = [i for i in intents if i["worker"] and i["creator"] == i["worker"]]
    open_i = [i for i in intents if not i["worker"]]
    dur = [i["concluded_at"] - i["created_at"] for i in intents if i["concluded_at"] and i["created_at"]]
    A["intents"] = {
        "total": len(intents), "cross": len(cross), "self": len(self_i), "open_pool": len(open_i),
        "concluded": sum(1 for i in intents if i["concluded_at"]),
        "dur_avg": statistics.mean(dur) if dur else None, "dur_p50": pct(dur, 0.5),
        "dur_p90": pct(dur, 0.9), "dur_max": max(dur) if dur else None,
        "cross_pairs": Counter((i["creator"], i["worker"]) for i in cross),
        "self_counts": Counter(i["creator"] for i in self_i),
        "diamond_out": Counter(i["worker"] for i in cross if i["creator"] == "diamond"),
        "dur_hist": hist_buckets(dur, [10, 30, 60, 180, 300, 600, 1800]),
    }

    tdurs = [t["t1"] - t["t0"] for t in d["turns"] if t["t0"] and t["t1"]]
    A["turns"] = {
        "total": len(d["turns"]),
        "prompt": sum(t["prompt"] for t in d["turns"]),
        "output": sum(t["output"] for t in d["turns"]),
        "reasoning": sum(t["reasoning"] for t in d["turns"]),
        "dur_avg": statistics.mean(tdurs) if tdurs else None,
        "dur_p50": pct(tdurs, 0.5), "dur_p90": pct(tdurs, 0.9),
        "dur_max": max(tdurs) if tdurs else None,
        "dur_hist": hist_buckets(tdurs, [5, 15, 30, 60, 120, 300, 600]),
    }

    msgs_by_run = defaultdict(list)
    ttls = []
    for m in d["messages"]:
        msgs_by_run[m["run_id"]].append(m)
        if m.get("deadline_at") and m.get("created_at"):
            try:
                dl = datetime.fromisoformat(str(m["deadline_at"]).replace("Z", "+00:00")).timestamp()
                ttls.append(dl - m["created_at"])
            except (ValueError, TypeError):
                pass
    A["messages"] = {
        "total": len(d["messages"]),
        "per_run": {rid: len(v) for rid, v in msgs_by_run.items()},
        "per_receiver": Counter(m["receiver"] for m in d["messages"]),
        "ttl_s": statistics.median(ttls) if ttls else None,
        "consumed_events": len(d["message_consumed_events"]),
    }

    pair_counts = Counter()
    for cid, mems in ch_members.items():
        ms = sorted({m["member"] for m in mems})
        for i in range(len(ms)):
            for j in range(i + 1, len(ms)):
                pair_counts[(ms[i], ms[j])] += 1
    A["cochallenge_pairs"] = pair_counts

    cat_solve = defaultdict(list)
    for c in chals.values():
        if c["solve_seconds"]:
            cat_solve[c["category"]].append(c["solve_seconds"])
    A["solve_by_cat"] = {k: {"n": len(v), "avg": statistics.mean(v), "p50": pct(v, 0.5)}
                         for k, v in cat_solve.items()}

    wp_start, wp_lat = {}, []
    for rid, evs in d["timeline"].items():
        for e in evs:
            key = (rid, e.get("challenge_id"))
            if e["kind"] == "wp.started":
                wp_start[key] = e["t"]
            elif e["kind"] == "wp.completed" and key in wp_start:
                wp_lat.append(e["t"] - wp_start[key])
    A["wp"] = {
        "lat_avg": statistics.mean(wp_lat) if wp_lat else None,
        "lat_p50": pct(wp_lat, 0.5), "lat_max": max(wp_lat) if wp_lat else None, "n": len(wp_lat),
        "jobs": Counter(j["status"] for j in d["wp_jobs"]),
    }

    A["facts"] = {"total": len(d["facts"]),
                  "per_project_top": Counter(f["project_id"] for f in d["facts"]).most_common(10)}
    A["memories"] = {"total": len(d["memories"]),
                     "by_cat": Counter(m["category"] for m in d["memories"])}
    A["ui_event_kinds"] = d["ui_event_kinds"]
    A["ops_session_count"] = d["ops_session_count"]
    return A



def esc(s):
    return html_mod.escape(str(s), quote=True)


def cat_color(cat):
    return CAT_COLORS.get(cat, "#57606a")


def svg_hist(hist, title, width=460, height=210, color="#0969da"):
    labels, counts = hist["labels"], hist["counts"]
    mx = max(counts) if counts else 1
    pad_l, pad_b, pad_t = 34, 40, 44
    cw = (width - pad_l - 10) / max(len(labels), 1)
    ch = height - pad_b - pad_t
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{esc(title)}">']
    parts.append(f'<text x="{pad_l}" y="16" class="ct">{esc(title)}</text>')
    for i, (lb, n) in enumerate(zip(labels, counts)):
        bh = ch * n / mx if mx else 0
        x = pad_l + i * cw + 5
        y = pad_t + ch - bh
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{cw - 10:.1f}" height="{bh:.1f}" fill="{color}" opacity="0.85" rx="2"/>')
        if n:
            parts.append(f'<text x="{x + (cw - 10) / 2:.1f}" y="{y - 7:.1f}" class="cv">{n}</text>')
        parts.append(f'<text x="{x + (cw - 10) / 2:.1f}" y="{height - pad_b + 16}" class="cl">{esc(lb)}</text>')
    parts.append(f'<line x1="{pad_l}" y1="{pad_t + ch}" x2="{width - 8}" y2="{pad_t + ch}" class="axis"/>')
    parts.append("</svg>")
    return "".join(parts)


def svg_hbar(items, title, width=470, row_h=26, color_fn=None, value_fmt=str):
    """items: list of (label, value)"""
    mx = max((v for _, v in items), default=1) or 1
    label_w = 165
    height = 34 + row_h * len(items) + 6
    bar_w = width - label_w - 70
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{esc(title)}">']
    parts.append(f'<text x="6" y="18" class="ct">{esc(title)}</text>')
    for i, (lb, v) in enumerate(items):
        y = 32 + i * row_h
        w = bar_w * v / mx
        color = color_fn(lb) if color_fn else "#0969da"
        parts.append(f'<text x="{label_w - 8}" y="{y + row_h * 0.68:.1f}" class="cl" text-anchor="end">{esc(lb)}</text>')
        parts.append(f'<rect x="{label_w}" y="{y + 3}" width="{max(w, 1):.1f}" height="{row_h - 9}" fill="{color}" opacity="0.85" rx="2"/>')
        parts.append(f'<text x="{label_w + max(w, 1) + 8:.1f}" y="{y + row_h * 0.68:.1f}" class="cv" text-anchor="start">{esc(value_fmt(v))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def nice_tick(span_s):
    for t in (60, 120, 300, 600, 900, 1800, 3600, 7200):
        if span_s / t <= 14:
            return t
    return 14400


def svg_gantt(rows, t_start, t_end, title, member_colors, width=1180, label_w=250):
    """rows: list of dicts with keys: title, cat, t0, t1, solved, member, wrong_marks, rebuild_marks, note"""
    row_h = 21
    pad_t, pad_b = 34, 30
    height = pad_t + row_h * len(rows) + pad_b
    tl_w = width - label_w - 90
    span = max(t_end - t_start, 1)

    def X(t):
        return label_w + (t - t_start) / span * tl_w

    tick = nice_tick(span)
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart gantt" role="img" aria-label="{esc(title)}">']
    parts.append(f'<text x="4" y="18" class="ct">{esc(title)}</text>')
    tt = t_start - (t_start % tick)
    while tt <= t_end + tick:
        if tt >= t_start:
            x = X(tt)
            parts.append(f'<line x1="{x:.1f}" y1="{pad_t - 6}" x2="{x:.1f}" y2="{height - pad_b + 4}" class="grid"/>')
            lbl = datetime.fromtimestamp(tt, timezone.utc).strftime("%H:%M")
            parts.append(f'<text x="{x:.1f}" y="{height - pad_b + 18}" class="cl" text-anchor="middle">{lbl}</text>')
        tt += tick
    for i, r in enumerate(rows):
        y = pad_t + i * row_h
        cy = y + row_h / 2
        parts.append(f'<text x="{label_w - 8}" y="{cy + 4:.1f}" class="cl row-label" text-anchor="end">{esc(r["title"][:30])}</text>')
        if r["t0"] is None:
            parts.append(f'<text x="{label_w + 6}" y="{cy + 4:.1f}" class="cdim">未分配</text>')
            continue
        t1 = r["t1"] if r["t1"] else t_end
        x0, x1 = X(r["t0"]), X(max(t1, r["t0"] + span * 0.004))
        color = cat_color(r["cat"])
        if r["solved"]:
            parts.append(f'<rect x="{x0:.1f}" y="{cy - 6:.1f}" width="{x1 - x0:.1f}" height="12" fill="{color}" opacity="0.9" rx="3"/>')
            parts.append(f'<circle cx="{x1:.1f}" cy="{cy:.1f}" r="4" fill="{color}" stroke="#1f2328" stroke-width="0.8"/>')
        else:
            parts.append(f'<rect x="{x0:.1f}" y="{cy - 6:.1f}" width="{x1 - x0:.1f}" height="12" fill="#ffffff" stroke="{color}" stroke-width="1.4" stroke-dasharray="4 3" rx="3"/>')
        for wm in r["wrong_marks"]:
            x = X(wm)
            parts.append(f'<path d="M {x:.1f} {cy + 10:.1f} l 4 6 l -8 0 z" fill="#cf222e"/>')
        for rm in r["rebuild_marks"]:
            x = X(rm)
            parts.append(f'<rect x="{x - 3:.1f}" y="{cy - 13:.1f}" width="6" height="6" fill="#9a6700" transform="rotate(45 {x:.1f} {cy - 10:.1f})"/>')
        if r["member"]:
            mc = member_colors.get(r["member"], "#57606a")
            label = r["member"] + (f' · {r["note"]}' if r.get("note") else "")
            if x1 + 8 + len(label) * 6.2 > width - 4:
                parts.append(f'<text x="{x1 - 8:.1f}" y="{cy + 4:.1f}" class="cm" text-anchor="end" fill="{mc}">{esc(label)}</text>')
            else:
                parts.append(f'<text x="{x1 + 7:.1f}" y="{cy + 4:.1f}" class="cm" fill="{mc}">{esc(label)}</text>')
        elif r.get("note"):
            parts.append(f'<text x="{x1 + 7:.1f}" y="{cy + 4:.1f}" class="cdim">{esc(r["note"])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def svg_network(A, width=1180, height=700):
    """Radial communication graph: hubs at center, members evenly on a circle.

    Edges are drawn from hub centers / to member centers and then covered by the
    node boxes, so lines never pile up at a single visible point.
    Edge counts live on the member labels instead of along the lines.
    """
    members = A["members_sorted"]
    mc = A["member_colors"]
    cx, cy, R = width / 2, 330, 212
    n = len(members)
    pos = {}
    for i, m in enumerate(members):
        ang = -math.pi / 2 + i * 2 * math.pi / n
        pos[m] = (cx + R * math.cos(ang), cy + R * math.sin(ang), ang)
    ctrl = (cx - 82, cy)
    dia = (cx + 82, cy)
    gz = (width - 150, 54)
    bb = (width - 180, height - 48)

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="communication graph">']
    parts.append('<defs><marker id="arrB" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#0969da"/></marker>'
                 '<marker id="arrG" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#1a7f37"/></marker>'
                 '<marker id="arrO" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#bc4c00"/></marker>'
                 '<marker id="arrX" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#57606a"/></marker></defs>')

    for (a, b), n_ in A["cochallenge_pairs"].items():
        if a in pos and b in pos and (a, b) not in A["intents"]["cross_pairs"]:
            x1, y1, _ = pos[a]
            x2, y2, _ = pos[b]
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            qx, qy = mx + (cx - mx) * 0.45, my + (cy - my) * 0.45
            parts.append(f'<path d="M {x1:.0f} {y1:.0f} Q {qx:.0f} {qy:.0f} {x2:.0f} {y2:.0f}" stroke="#9a6700" stroke-width="1" fill="none" stroke-dasharray="3 3" opacity="0.5"/>')

    def spoke(hub, m, color, w, marker):
        x2, y2, _ = pos[m]
        parts.append(f'<line x1="{hub[0]}" y1="{hub[1]}" x2="{x2:.0f}" y2="{y2:.0f}" stroke="{color}" stroke-width="{w:.1f}" opacity="0.75" marker-end="url(#{marker})"/>')

    for m, n_ in A["messages"]["per_receiver"].items():
        if m in pos:
            spoke(ctrl, m, "#0969da", 1 + 1.6 * math.log1p(n_), "arrB")
    for m, n_ in A["intents"]["diamond_out"].items():
        if m in pos:
            spoke(dia, m, "#1a7f37", 1 + 1.4 * math.log1p(n_), "arrG")
    for (a, b), n_ in A["intents"]["cross_pairs"].items():
        if a == "diamond" or a not in pos or b not in pos:
            continue
        x1, y1, _ = pos[a]
        x2, y2, _ = pos[b]
        parts.append(f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" stroke="#bc4c00" stroke-width="{1 + 1.2 * math.log1p(n_):.1f}" opacity="0.8" marker-end="url(#arrO)"/>')

    parts.append(f'<line x1="{ctrl[0]}" y1="{ctrl[1]}" x2="{gz[0]}" y2="{gz[1]}" stroke="#57606a" stroke-width="1.3" stroke-dasharray="5 4" marker-end="url(#arrX)"/>')
    parts.append(f'<text x="{(ctrl[0] + gz[0]) / 2 + 60:.0f}" y="{(ctrl[1] + gz[1]) / 2 - 40:.0f}" class="cl" text-anchor="middle">题目/实例同步 · flag 提交</text>')
    parts.append(f'<line x1="{dia[0]}" y1="{dia[1]}" x2="{bb[0]}" y2="{bb[1]}" stroke="#1a7f37" stroke-width="1.3" stroke-dasharray="5 4" marker-end="url(#arrG)"/>')

    def hub_box(center, w, h, title, sub, stroke):
        x, y = center
        parts.append(f'<rect x="{x - w / 2:.0f}" y="{y - h / 2:.0f}" width="{w}" height="{h}" rx="9" fill="#ffffff" stroke="{stroke}" stroke-width="1.6"/>')
        parts.append(f'<text x="{x:.0f}" y="{y - 2:.0f}" class="cn" text-anchor="middle">{esc(title)}</text>')
        parts.append(f'<text x="{x:.0f}" y="{y + 13:.0f}" class="cdim" text-anchor="middle">{esc(sub)}</text>')

    hub_box(ctrl, 164, 46, "competition-control", "编排服务 · 调度/实例/消息", "#57606a")
    hub_box(dia, 148, 46, "diamond", "编排成员 · 规划 intent", "#1f2328")
    hub_box(gz, 200, 46, "GZCTF 平台", "题目同步 · 靶机 · 判题", "#9a6700")
    hub_box(bb, 280, 40, "黑板（PostgreSQL）", f'intents×{A["intents"]["total"]} · facts×{A["facts"]["total"]} · memories×{A["memories"]["total"]}', "#1a7f37")

    for m in members:
        x, y, ang = pos[m]
        st = A["member_stats"].get(m, {})
        color = mc.get(m, "#57606a")
        parts.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="19" fill="#ffffff" stroke="{color}" stroke-width="2.2"/>')
        parts.append(f'<text x="{x:.0f}" y="{y + 4:.0f}" class="cn" text-anchor="middle" fill="{color}">{esc(m[:2])}</text>')
        ca, sa = math.cos(ang), math.sin(ang)
        lx = cx + (R + 46) * ca
        ly = cy + (R + 46) * sa
        if sa < 0:
            dy_name, dy_stats, dy_counts = 15, 0, -15
        else:
            dy_name, dy_stats, dy_counts = -11, 4, 19
        if ca < -0.45:
            tanchor = "end"
        elif ca > 0.45:
            tanchor = "start"
        else:
            tanchor = "middle"
        parts.append(f'<text x="{lx:.0f}" y="{ly + dy_name:.0f}" class="cn" text-anchor="{tanchor}" fill="{color}">{esc(m)}</text>')
        parts.append(f'<text x="{lx:.0f}" y="{ly + dy_stats:.0f}" class="cdim" text-anchor="{tanchor}">P{st.get("primary", 0)} H{st.get("helper", 0)} W{st.get("wp", 0)} · flag×{st.get("flags", 0)}</text>')
        bits = []
        rc = A["messages"]["per_receiver"].get(m, 0)
        ic = A["intents"]["diamond_out"].get(m, 0)
        sc = A["intents"]["self_counts"].get(m, 0)
        if rc:
            bits.append(f"recon×{rc}")
        if ic:
            bits.append(f"intent×{ic}")
        if sc:
            bits.append(f"自建×{sc}")
        if bits:
            parts.append(f'<text x="{lx:.0f}" y="{ly + dy_counts:.0f}" class="cl" text-anchor="{tanchor}">{esc(" · ".join(bits))}</text>')

    lx0, ly0 = 40, 36
    parts.append(f'<line x1="{lx0}" y1="{ly0}" x2="{lx0 + 26}" y2="{ly0}" stroke="#0969da" stroke-width="2.4"/><text x="{lx0 + 32}" y="{ly0 + 4}" class="cl">recon 靶机推送（服务→成员）</text>')
    parts.append(f'<line x1="{lx0}" y1="{ly0 + 18}" x2="{lx0 + 26}" y2="{ly0 + 18}" stroke="#1a7f37" stroke-width="2.4"/><text x="{lx0 + 32}" y="{ly0 + 22}" class="cl">intent 指派（diamond→成员）</text>')
    parts.append(f'<line x1="{lx0}" y1="{ly0 + 36}" x2="{lx0 + 26}" y2="{ly0 + 36}" stroke="#9a6700" stroke-width="1.4" stroke-dasharray="3 3"/><text x="{lx0 + 32}" y="{ly0 + 40}" class="cl">同题 primary+helper（共享容器）</text>')
    parts.append("</svg>")
    return "".join(parts)


def _box(x, y, w, h, title, lines, color="#0969da", title_color=None, dash=None):
    dashattr = f' stroke-dasharray="{dash}"' if dash else ""
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="#f6f8fa" stroke="{color}" stroke-width="1.6"{dashattr}/>']
    out.append(f'<text x="{x + w / 2}" y="{y + 20}" class="bn" text-anchor="middle" fill="{title_color or color}">{esc(title)}</text>')
    for i, ln in enumerate(lines):
        out.append(f'<text x="{x + w / 2}" y="{y + 38 + i * 15}" class="cdim" text-anchor="middle">{esc(ln)}</text>')
    return "".join(out)


def _arrow(x1, y1, x2, y2, color="#57606a", label=None, dash=None, w=1.5, lpos=0.5, ldy=-4):
    dashattr = f' stroke-dasharray="{dash}"' if dash else ""
    out = [f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{w}"{dashattr} marker-end="url(#arrM)"/>']
    if label:
        mx, my = x1 + (x2 - x1) * lpos, y1 + (y2 - y1) * lpos + ldy
        out.append(f'<text x="{mx}" y="{my}" class="cdim" text-anchor="middle" fill="{color}">{esc(label)}</text>')
    return "".join(out)


def _path(points, color="#57606a", label=None, dash=None, w=1.5, label_at=None):
    """Polyline with arrowhead at the end. points: [(x,y), ...]"""
    dashattr = f' stroke-dasharray="{dash}"' if dash else ""
    dstr = "M " + " L ".join(f"{x} {y}" for x, y in points)
    out = [f'<path d="{dstr}" stroke="{color}" stroke-width="{w}" fill="none"{dashattr} marker-end="url(#arrM)"/>']
    if label and label_at:
        out.append(f'<text x="{label_at[0]}" y="{label_at[1]}" class="cdim" text-anchor="middle" fill="{color}">{esc(label)}</text>')
    return "".join(out)


def svg_mechanism(A, d, width=1240, height=730):
    p = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="mechanism diagram">']
    p.append('<defs><marker id="arrM" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#57606a"/></marker></defs>')
    msg_n = A["messages"]["total"]
    it = A["intents"]
    tn = A["turns"]
    subs_c = sum(A["run_stats"][r["id"]]["n_correct_subs"] for r in d["runs"])
    subs_w = sum(A["run_stats"][r["id"]]["n_wrong_subs"] for r in d["runs"])
    tools = d["artifact_logs"]["tool_events"]

    p.append(_box(40, 20, 220, 62, "操作者 / 看门狗脚本", ["HTTP API: start / pause / stop", "SSE 事件流订阅"], "#1f2328"))
    p.append('<rect x="40" y="120" width="700" height="150" rx="12" fill="#ffffff" stroke="#d0d7de" stroke-width="1.4"/>')
    p.append('<text x="390" y="142" class="bn" text-anchor="middle" fill="#1f2328">competition-control 编排服务（异步引擎 + 租约）</text>')
    p.append(_box(60, 156, 150, 96, "调度器", ["tick 驱动", "题目队列", "席位分配"], "#0969da"))
    p.append(_box(225, 156, 150, 96, "实例管理器", ["创建 / 续约 / 重建", "端口随代际漂移"], "#9a6700"))
    p.append(_box(390, 156, 150, 96, "消息总线", ["competition_messages", "游标消费"], "#0969da"))
    p.append(_box(555, 156, 170, 96, "WP 派发器", ["写题解 job", "质量门禁 + 退避"], "#8250df"))
    p.append('<rect x="40" y="305" width="700" height="88" rx="12" fill="#ffffff" stroke="#1a7f37" stroke-width="1.4"/>')
    p.append('<text x="390" y="327" class="bn" text-anchor="middle" fill="#1a7f37">黑板（PostgreSQL 共享状态）</text>')
    p.append(f'<text x="390" y="350" class="cdim" text-anchor="middle">intents 任务 ×{it["total"]}（指派 {it["cross"]} · 自建 {it["self"]} · 池 {it["open_pool"]}）　'
             f'facts 事实 ×{A["facts"]["total"]}　memories 经验 ×{A["memories"]["total"]}　projects 图谱 ×92</text>')
    p.append('<text x="390" y="372" class="cdim" text-anchor="middle">intents 由 diamond 规划、Member 认领执行；facts/memories 全局可读，按 project 聚合</text>')
    p.append(_box(790, 120, 200, 88, "diamond（编排成员）", ["LLM 规划循环", "读黑板 → 写 intent", "不直接解题"], "#1f2328"))
    p.append('<rect x="40" y="440" width="1150" height="240" rx="12" fill="#ffffff" stroke="#d0d7de" stroke-width="1.4"/>')
    p.append('<text x="615" y="462" class="bn" text-anchor="middle" fill="#1f2328">Member 池（10 个宝石代号 Agent，deepseek-v4.1-flash / reasoning=max）</text>')
    mx0 = 70
    for i, name in enumerate(("amber", "sugilite", "agate", "… 共 10 个")):
        x = mx0 + i * 285
        if name.startswith("…"):
            p.append(_box(x, 480, 240, 180, name, ["角色：primary 主攻", "helper 交叉验证", "wp 写题解"], "#d0d7de"))
        else:
            p.append(_box(x, 480, 240, 180, f"member: {name}", [
                "LLM turn 循环（观测→思考→工具）",
                "工具: bash · job_* · 技能 · MCP",
                "submit_flag / conclude / report",
                "独立任务容器（docker）",
                "/workspace/shared 共享目录"], A["member_colors"].get(name, "#57606a")))
    p.append(_box(1010, 20, 190, 66, "GZCTF 平台", ["题目 / 靶机实例", "flag 判题"], "#9a6700"))
    p.append(_box(790, 240, 200, 66, "LLM Provider", [f'{tn["total"]} turns', f'token {tn["prompt"] / 1e6:.1f}M↓ {tn["output"] / 1e6:.2f}M↑'], "#8250df"))
    p.append(_box(1010, 120, 190, 88, "MCP reverse / browser", ["恒失败：镜像缺 docker CLI", "回退容器内 objdump/ghidra"], "#cf222e", dash="5 4"))
    p.append(_box(1010, 240, 190, 66, "任务容器（每题一个）", [f'bash×{tools.get("bash", 0)} job×{tools.get("job_start", 0)}', "helper 与 primary 共享"], "#1a7f37"))
    p.append(_arrow(150, 82, 150, 118, label="控制指令"))
    p.append(_arrow(740, 165, 788, 160, "#57606a", "席位/租约", ldy=-10))
    p.append(_arrow(390, 270, 390, 303, "#1a7f37"))
    p.append(_arrow(890, 208, 890, 238, "#8250df", "LLM 调用"))
    p.append(_path([(540, 204), (764, 330), (800, 438)], "#0969da",
                   f"recon 消息 ×{msg_n}（单向推送 target_instance）", label_at=(658, 296)))
    p.append(_arrow(840, 208, 760, 440, "#1a7f37", f"intent 指派 ×{it['cross']}", lpos=0.45, ldy=-8))
    p.append(_arrow(615, 440, 615, 395, "#1a7f37", "读写黑板", lpos=0.5, ldy=12))
    p.append(_arrow(1090, 306, 1090, 440, "#1a7f37", "exec"))
    p.append(_path([(300, 156), (300, 100), (1010, 55)], "#9a6700",
                   "题目同步 · 实例创建/续约/重建", label_at=(600, 92)))
    p.append(_arrow(700, 120, 1012, 58, "#9a6700", f"flag 提交 correct×{subs_c} wrong×{subs_w}", lpos=0.62, ldy=16))
    p.append(_path([(725, 230), (782, 342), (880, 438)], "#8250df",
                   f"wp 派发（jobs {sum(A['wp']['jobs'].values())}）", label_at=(836, 400)))
    p.append("</svg>")
    return "".join(p)


def svg_lifecycle(width=1240, height=420):
    p = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="challenge lifecycle">']
    p.append('<defs><marker id="arrL" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#57606a"/></marker></defs>')

    def state(x, y, w, h, title, sub, color, **kw):
        return _box(x, y, w, h, title, [sub] if sub else [], color, **kw)

    def arr(x1, y1, x2, y2, color="#57606a", label=None, dash=None, ldy=-6):
        dashattr = f' stroke-dasharray="{dash}"' if dash else ""
        p.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="1.6"{dashattr} marker-end="url(#arrL)"/>')
        if label:
            p.append(f'<text x="{(x1 + x2) / 2}" y="{(y1 + y2) / 2 + ldy}" class="cdim" text-anchor="middle" fill="{color}">{esc(label)}</text>')

    y = 90
    p.append(state(30, y, 130, 64, "discovered", "run.synchronized", "#57606a"))
    p.append(state(200, y, 130, 64, "ready", "排队等席位", "#57606a"))
    p.append(state(370, y, 140, 64, "assigned", "primary 占席", "#0969da"))
    p.append(state(550, y, 150, 64, "solving", "intent 循环驱动", "#0969da"))
    p.append(state(740, y, 150, 64, "flag 发现", "submit_flag 工具", "#1a7f37"))
    p.append(state(930, y, 130, 64, "solved", "verdict=correct", "#1a7f37"))
    p.append(state(1090, y, 120, 64, "WP 撰写", "wp 成员", "#8250df"))
    arr(160, y + 32, 198, y + 32)
    arr(330, y + 32, 368, y + 32, label="调度 tick")
    arr(510, y + 32, 548, y + 32, label="spawn 会话")
    arr(700, y + 32, 738, y + 32)
    arr(890, y + 32, 928, y + 32, label="平台判题")
    arr(1060, y + 32, 1088, y + 32)
    p.append(state(740, 220, 150, 56, "wrong", "冷却+错误计数", "#cf222e"))
    arr(815, 154, 815, 218, "#cf222e", "verdict=wrong")
    arr(740, 248, 625, 156, "#cf222e", "重试", dash="4 3", ldy=14)
    p.append(state(370, 220, 190, 56, "实例重建风暴", "续约失败→rebuild→换端口", "#9a6700"))
    arr(625, 154, 500, 218, "#9a6700", "远程实例题", dash="4 3")
    arr(460, 220, 445, 156, "#9a6700", "进度归零", dash="4 3", ldy=16)
    p.append(state(200, 220, 130, 56, "stalled", "超时收席", "#57606a", dash="4 3"))
    p.append(state(30, 220, 130, 56, "expired", "放弃/跳过", "#57606a", dash="4 3"))
    arr(435, 154, 300, 218, "#57606a", None, dash="4 3")
    arr(265, 276, 130, 90 + 64, "#57606a", None, dash="4 3")
    p.append('<text x="620" y="350" class="cdim" text-anchor="middle">helper 可由 cross_check 模板加入同题（共享容器）；solved 后释放席位，调度器分配下一题</text>')
    p.append("</svg>")
    return "".join(p)



CSS = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:#ffffff;color:#1f2328;font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;font-size:14px;line-height:1.6}
.wrap{max-width:1280px;margin:0 auto;padding:28px 32px 80px}
h1{font-size:26px;margin:8px 0 2px}
h2{font-size:19px;margin:38px 0 10px;padding-top:14px;border-top:1px solid #d0d7de;color:#0550ae}
h3{font-size:15px;margin:22px 0 8px;color:#0550ae}
.sub{color:#57606a;font-size:13px}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin:14px 0}
.card{background:#f6f8fa;border:1px solid #d0d7de;border-radius:10px;padding:12px 16px;min-width:200px;flex:1}
.card .t{font-size:13px;color:#57606a;margin-bottom:4px}
.card .v{font-size:22px;font-weight:600}
.card .s{font-size:12px;color:#57606a;margin-top:4px}
table{border-collapse:collapse;width:100%;margin:10px 0;font-size:12.5px}
th,td{border:1px solid #d0d7de;padding:5px 9px;text-align:left;vertical-align:top}
th{background:#f6f8fa;color:#0550ae;white-space:nowrap}
tr:nth-child(even) td{background:#f6f8fa}
.chip{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.chart{width:100%;height:auto;background:#ffffff;border:1px solid #d0d7de;border-radius:10px;margin:8px 0}
.chart text{font-family:"Segoe UI","Microsoft YaHei",sans-serif}
.ct{fill:#1f2328;font-size:13px;font-weight:600}
.cl{fill:#57606a;font-size:10px}
.cv{fill:#1f2328;font-size:10px;text-anchor:middle}
.cdim{fill:#57606a;font-size:10.5px}
.cm{fill:#57606a;font-size:10px}
.cn{fill:#1f2328;font-size:11.5px;font-weight:600}
.bn{fill:#1f2328;font-size:13px;font-weight:600}
.axis{stroke:#afb8c1;stroke-width:1}
.grid{stroke:#eaeef2;stroke-width:1}
.row-label{fill:#1f2328;font-size:10px}
pre{background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;padding:10px 14px;overflow:auto;font-size:12px;color:#0550ae}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:1000px){.two{grid-template-columns:1fr}}
.note{background:#fff8e6;border-left:3px solid #9a6700;padding:8px 14px;margin:10px 0;border-radius:0 8px 8px 0;color:#1f2328;font-size:13px}
.good{border-left-color:#1a7f37}
.bad{border-left-color:#cf222e}
ul{margin:6px 0 6px 20px;padding:0}
li{margin:3px 0}
.legend span{display:inline-block;margin-right:16px;font-size:12px;color:#1f2328}
.legend i{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.kbd{background:#eaeef2;border:1px solid #d0d7de;border-radius:4px;padding:0 5px;font-family:monospace;font-size:12px}
"""


def legend_cat(cats):
    items = "".join(f'<span><i style="background:{cat_color(c)}"></i>{esc(c)}</span>' for c in sorted(cats))
    return ('<div class="legend">' + items +
            '<span><i style="background:#cf222e"></i>▲ 错误提交</span>'
            '<span><i style="background:#9a6700"></i>◆ 实例重建</span>'
            '<span><i style="border:1px dashed #57606a;background:transparent"></i>未解决(至 run 结束)</span></div>')


def build_gantts(d, A):
    out = []
    for run in d["runs"]:
        rid = run["id"]
        rch = [c for c in d["challenges"].values() if c["run_id"] == rid]
        if not rch:
            out.append(f'<div class="note">{esc(run["label"])}：未进入解题阶段（启动即停止，无题目分配）。</div>')
            continue
        t_start = run["created_at"]
        t_end = run["updated_at"]
        evs = d["timeline"].get(rid, [])
        wrong_marks = defaultdict(list)
        for s in d["submissions"]:
            if s["run_id"] == rid and s["status"] == "wrong":
                wrong_marks[s["challenge_id"]].append(s["submitted_at"] or s["created_at"])
        rebuild_marks = defaultdict(list)
        for e in evs:
            if e["kind"] == "instance.rebuilt" and e.get("challenge_id"):
                rebuild_marks[e["challenge_id"]].append(e["t"])
        rows = []
        for c in sorted(rch, key=lambda x: (x["first_assigned_at"] is None, x["first_assigned_at"] or 0)):
            rows.append({
                "title": f'{c["title"]}',
                "cat": c["category"],
                "t0": c["first_assigned_at"],
                "t1": c["solved_at"],
                "solved": c["state"] == "solved",
                "member": c["solved_by"] or c["primary"],
                "wrong_marks": wrong_marks.get(c["id"], []),
                "rebuild_marks": rebuild_marks.get(c["id"], []),
                "note": fmt_dur(c["solve_seconds"]) if c["solve_seconds"] else (
                    c["state"] if c["state"] != "ready" else ""),
            })
        st = A["run_stats"][rid]
        title = (f'{run["label"]} — {st["n_solved"]}/{st["n_challenges"]} solved · '
                 f'时长 {fmt_dur(st["duration_s"])} · 状态 {run["status"]}')
        out.append(f"<h3>{esc(title)}</h3>")
        out.append(legend_cat(st["cats"]))
        out.append(svg_gantt(rows, t_start, t_end, "", A["member_colors"]))
    return "\n".join(out)


def build_rebuild_storm(d, A):
    """PWN run R4: instance generations (ports) per challenge over time."""
    rid = "4a7f01c88a754466b79b6363c608eabe"
    msgs = defaultdict(list)
    for m in d["messages"]:
        if m["run_id"] == rid and m["challenge_id"]:
            msgs[m["challenge_id"]].append(m)
    rows = []
    for cid, ms in sorted(msgs.items(), key=lambda kv: len(kv[1]), reverse=True):
        ch = d["challenges"].get(cid, {})
        segs = []
        prev = None
        n_gen = len({m["generation"] for m in ms})
        for m in sorted(ms, key=lambda x: x["created_at"]):
            port = (m["entry"] or "?").split(":")[-1]
            gen_key = (m["generation"], port)
            if gen_key == prev:
                continue
            prev = gen_key
            segs.append(f'g{m["generation"]} :{port} @{datetime.fromtimestamp(m["created_at"], timezone.utc).strftime("%H:%M")}')
        rows.append(f'<tr><td>{esc(ch.get("title", cid[:8]))}</td><td>{len(ms)} 条 / {n_gen} 代</td>'
                    f'<td>{esc(ch.get("state", "?"))}</td><td style="font-family:monospace;font-size:11.5px">{esc(" → ".join(segs))}</td></tr>')
    if not rows:
        return '<div class="note">无数据</div>'
    return ('<table><tr><th>题目</th><th>recon 推送 / 代际</th><th>最终状态</th><th>端口漂移轨迹（generation :port @时间，已去重）</th></tr>'
            + "".join(rows) + "</table>")


def build_html(d, A):
    runs = d["runs"]
    rs = A["run_stats"]
    tot_solved = sum(rs[r["id"]]["n_solved"] for r in runs)
    tot_ch = sum(rs[r["id"]]["n_challenges"] for r in runs)
    tn = A["turns"]

    cards = []
    for r in runs:
        st = rs[r["id"]]
        cards.append(
            f'<div class="card"><div class="t">{esc(r["label"])} · {esc(r["status"])}</div>'
            f'<div class="v">{st["n_solved"]}<span style="font-size:14px;color:#57606a"> / {st["n_challenges"]} solved</span></div>'
            f'<div class="s">{fmt_ts(r["created_at"])} → {fmt_ts(r["updated_at"])}（{fmt_dur(st["duration_s"])}）<br>'
            f'提交 correct×{st["n_correct_subs"]} wrong×{st["n_wrong_subs"]} · 实例重建×{st["rebuilds"]} · 续约失败×{st["renew_failed"]}<br>'
            f'解题耗时 中位 {fmt_dur(st["solve_t_p50"])} · 最快 {fmt_dur(st["solve_t_min"])} · 最慢 {fmt_dur(st["solve_t_max"])}</div></div>')
    cards_html = '<div class="cards">' + "".join(cards) + "</div>"

    agg_html = f'''<div class="cards">
<div class="card"><div class="t">总解题数</div><div class="v">{tot_solved} / {tot_ch}</div><div class="s">5 轮 run 合计</div></div>
<div class="card"><div class="t">LLM turns</div><div class="v">{tn["total"]}</div><div class="s">prompt {tn["prompt"] / 1e6:.2f}M · output {tn["output"] / 1e6:.2f}M · reasoning {tn["reasoning"] / 1e6:.2f}M tokens</div></div>
<div class="card"><div class="t">协同消息</div><div class="v">{A["messages"]["total"]}</div><div class="s">全部为目标实例 recon 推送（服务→成员单向）；成员→成员业务消息 0 条</div></div>
<div class="card"><div class="t">黑板 intents</div><div class="v">{A["intents"]["total"]}</div><div class="s">diamond 指派 {A["intents"]["cross"]} · 成员自建 {A["intents"]["self"]} · 完成 {A["intents"]["concluded"]}</div></div>
<div class="card"><div class="t">工具调用（日志）</div><div class="v">{sum(d["artifact_logs"]["tool_events"].values())}</div><div class="s">bash×{d["artifact_logs"]["tool_events"].get("bash", 0)} · mcp×{d["artifact_logs"]["tool_events"].get("mcp_call", 0)} · job×{d["artifact_logs"]["tool_events"].get("job_start", 0)}</div></div>
</div>'''

    mrows = []
    for m in A["members_sorted"]:
        st = A["member_stats"][m]
        tok = st["prompt"] + st["output"] + st["reasoning"]
        mrows.append(
            f'<tr><td><span class="chip" style="background:{A["member_colors"][m]}"></span>{esc(m)}</td>'
            f'<td>{st["primary"]}</td><td>{st["helper"]}</td><td>{st["wp"]}</td>'
            f'<td><b>{st["flags"]}</b></td><td>{st["turns"]}</td>'
            f'<td>{fmt_dur(st["turn_time_s"])}</td><td>{tok / 1e6:.2f}M</td><td>{st["tool_events"]}</td></tr>')
    member_table = ('<table><tr><th>成员</th><th>primary</th><th>helper</th><th>wp</th><th>verified flag</th>'
                    '<th>turns</th><th>turn 占用时长</th><th>tokens</th><th>工具事件</th></tr>'
                    + "".join(mrows) + "</table>")

    im, mm, wm = A["intents"], A["messages"], A["wp"]
    timing_table = f'''<table>
<tr><th>协同环节</th><th>样本</th><th>均值</th><th>p50</th><th>p90</th><th>最大</th><th>说明</th></tr>
<tr><td>消息投递（服务→成员 recon）</td><td>{mm["total"]}</td><td colspan="3">持久化推送，TTL {fmt_dur(mm["ttl_s"])}（deadline−created）</td><td>—</td><td>游标消费无积压；落库消费事件 {mm["consumed_events"]} 条，不足以测算精确延迟</td></tr>
<tr><td>intent 生命周期（创建→完结）</td><td>{im["concluded"]}</td><td>{fmt_dur(im["dur_avg"])}</td><td>{fmt_dur(im["dur_p50"])}</td><td>{fmt_dur(im["dur_p90"])}</td><td>{fmt_dur(im["dur_max"])}</td><td>含等待认领时间</td></tr>
<tr><td>LLM turn（一次 思考→工具 往返）</td><td>{tn["total"]}</td><td>{fmt_dur(tn["dur_avg"])}</td><td>{fmt_dur(tn["dur_p50"])}</td><td>{fmt_dur(tn["dur_p90"])}</td><td>{fmt_dur(tn["dur_max"])}</td><td>最大 turn 为 P5 流式悬挂</td></tr>
<tr><td>WP 撰写（started→completed）</td><td>{wm["n"]}</td><td>{fmt_dur(wm["lat_avg"])}</td><td>{fmt_dur(wm["lat_p50"])}</td><td>—</td><td>{fmt_dur(wm["lat_max"])}</td><td>不含 deferred 重试</td></tr>
</table>'''

    cat_items = [(f'{k} (n={v["n"]})', round(v["avg"])) for k, v in sorted(A["solve_by_cat"].items(), key=lambda kv: -kv[1]["avg"])]
    cat_chart = svg_hbar(cat_items, "平均解题耗时（秒，按类别）", color_fn=lambda lb: cat_color(lb.split(" ")[0]), value_fmt=fmt_dur)

    tool_items = list(d["artifact_logs"]["tool_events"].items())[:12]
    tool_chart = svg_hbar(tool_items, "工具调用频次（artifact tool_logs）", color_fn=lambda lb: "#1a7f37" if lb in ("bash",) else "#0969da")

    turn_hist = svg_hist(tn["dur_hist"], "LLM turn 时长分布", color="#8250df")
    intent_hist = svg_hist(im["dur_hist"], "intent 生命周期分布", color="#1a7f37")

    its = [i for i in d["intents"] if i["concluded_at"] and i["created_at"]]
    its_sorted = sorted(its, key=lambda i: -(i["concluded_at"] - i["created_at"]))[:14]
    irows = []
    for i in its_sorted:
        dur_i = i["concluded_at"] - i["created_at"]
        kind = "指派" if i["worker"] and i["creator"] != i["worker"] else ("自建" if i["worker"] else "池")
        irows.append(f'<tr><td>{esc(i["creator"])} → {esc(i["worker"] or "(未认领)")}</td><td>{kind}</td>'
                     f'<td>{fmt_dur(dur_i)}</td><td>{esc(i["description"])}</td></tr>')
    intent_table = ('<table><tr><th>创建者 → 执行者</th><th>类型</th><th>耗时</th><th>内容（截断）</th></tr>'
                    + "".join(irows) + "</table>")

    frows = []
    for f in d["facts"][:10]:
        frows.append(f'<tr><td>{esc(f["project_id"])}</td><td>{fmt_ts(f["created_at"])}</td><td>{esc(f["description"])}</td></tr>')
    facts_table = ('<table><tr><th>project</th><th>时间</th><th>内容（截断）</th></tr>' + "".join(frows) + "</table>")

    sample_msg = next((m for m in d["messages"] if m["entry"]), None)
    msg_sample = json.dumps({k: v for k, v in sample_msg.items() if v is not None}, ensure_ascii=False, indent=2) if sample_msg else "{}"

    mem_items = list(A["memories"]["by_cat"].most_common())
    mem_chart = svg_hbar(mem_items, "memories 经验条目（按类别）", color_fn=lambda lb: "#9a6700") if mem_items else ""

    logs_cov = d["artifact_logs"]["files"]
    coverage_note = (
        f'数据源：PostgreSQL 竞赛库 16 张表（run_events {sum(len(v) for v in d["timeline"].values())} 条采样内事件 + '
        f'challenge.expired {sum(d["expired_events_per_run"].values())} 条聚合计数、agent_turns {tn["total"]}、'
        f'messages {A["messages"]["total"]}、intents {A["intents"]["total"]}、facts {A["facts"]["total"]}）＋ '
        f'artifact JSONL 日志 {sum(logs_cov.values())} 个文件 / {d["artifact_logs"]["total_lines"]} 行'
        f'（tool_logs {logs_cov.get("tool_logs", 0)}、llm_logs {logs_cov.get("llm_logs", 0)}、'
        f'memory_logs {logs_cov.get("memory_logs", 0)}、project_logs {logs_cov.get("project_logs", 0)}）。'
        f'另有 ops 会话 {A["ops_session_count"]} 个（运维问答，不计入解题协同）。')

    wp_jobs_str = " · ".join(f"{k}×{v}" for k, v in A["wp"]["jobs"].most_common())

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>IPC_CTFAgent 多 Agent 协同分析报告</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{CSS}</style></head><body><div class="wrap">

<h1>IPC_CTFAgent 多 Agent 协同分析报告</h1>
<div class="sub">生成时间 {esc(d["generated_at"])} ｜ 覆盖 5 轮真实平台测试（2026-10-14 ~ 10-15，GZCTF games/33 · PCTF 练习场）｜ 模型 deepseek-v4.1-flash / reasoning=max<br>{esc(coverage_note)}</div>

<h2>1. 总览：五轮测试战绩</h2>
{cards_html}
{agg_html}
<div class="note">R1/R2 为启动尝试（附件 origin 白名单阻断，未进入解题）；R3 主 run 65 题 solved 23（HTTP 控制面卡死约 25 分钟期间仍推进 3 题）；R4 PWN 专项 7 题 solved 6，历时 13.7 小时（实例重建风暴是主要阻力）；R5 复测 20 题 solved 17，成功率显著提升。</div>

<h2>2. 协同机制图（架构与数据流）</h2>
{svg_mechanism(A, d)}
<ul>
<li><b>三层结构</b>：competition-control 编排服务（调度/实例/消息/WP）→ 黑板（PostgreSQL: intents/facts/memories/projects）→ Member 池（10 个 LLM Agent 会话 + 每题独立任务容器）。</li>
<li><b>diamond</b> 是编排成员：只读黑板、写 intent，不直接解题；Member 认领 intent 执行，产出 facts/memories 回流黑板。</li>
<li><b>消息总线</b>仅承载服务→成员的 <span class="kbd">target_instance</span> recon 推送（共 {A["messages"]["total"]} 条），授权层允许的成员互发在工具层无入口，实测 0 条。</li>
<li><b>MCP reverse/browser 全程不可用</b>（app 镜像缺 docker 客户端，错误 {d["artifact_logs"]["tool_events"].get("mcp_call", 0)} 次调用大多失败），Member 回退容器内工具。</li>
</ul>

<h2>3. 题目生命周期工作流</h2>
{svg_lifecycle()}

<h2>4. 各轮解题工作流时间线（甘特）</h2>
<div class="sub">横轴为 UTC 时间；每行一题，条带从首次分配画到 solved（或 run 结束）。颜色=类别，右注=解题成员与耗时。</div>
{build_gantts(d, A)}

<h2>5. 通信交流图</h2>
{svg_network(A)}
<div class="two">
<div>
<h3>消息层统计</h3>
<table>
<tr><th>指标</th><th>值</th></tr>
<tr><td>总消息数</td><td>{A["messages"]["total"]}（全部 kind=recon / target_instance）</td></tr>
<tr><td>发送方</td><td>competition-control ×{A["messages"]["total"]}（100%）</td></tr>
<tr><td>成员→成员业务消息</td><td><b>0</b>（授权层放行但工具层无发送入口）</td></tr>
<tr><td>按 run 分布</td><td>{esc(" · ".join(f"{RUN_LABELS.get(k, k[:8])}:{v}" for k, v in sorted(A["messages"]["per_run"].items())))}</td></tr>
<tr><td>消费</td><td>游标消费正常，记录的 message_consumed 事件 {A["messages"]["consumed_events"]} 条</td></tr>
</table>
</div>
<div>
<h3>黑板 intent 统计</h3>
<table>
<tr><th>指标</th><th>值</th></tr>
<tr><td>总 intents</td><td>{im["total"]}（完结 {im["concluded"]}）</td></tr>
<tr><td>diamond → 成员指派</td><td>{im["cross"]} 条；对象分布 {esc(" · ".join(f"{m}×{n}" for m, n in im["diamond_out"].most_common(8)))}</td></tr>
<tr><td>成员自建（自我拆解子任务）</td><td>{im["self"]} 条</td></tr>
<tr><td>开放池（未指定执行者）</td><td>{im["open_pool"]} 条</td></tr>
<tr><td>跨成员（非 diamond）intent</td><td>{sum(n for (a, b), n in im["cross_pairs"].items() if a != "diamond")} 条</td></tr>
</table>
</div>
</div>
<div class="note">同题 primary+helper 共 {sum(A["cochallenge_pairs"].values())} 对（虚线）；helper 由 cross_check 模板加入，共享同一任务容器，但实测两成员各自独立解题（重复劳动），未通过任何消息通道分工。</div>

<h2>6. 协同时间分析</h2>
{timing_table}
<div class="two">
<div>{turn_hist}</div>
<div>{intent_hist}</div>
</div>
<div class="two">
<div>{cat_chart}</div>
<div>{tool_chart}</div>
</div>
<h3>成员工作负载</h3>
{member_table}
<div class="note">turn 占用时长 = 该成员所有 LLM turn 的墙上时间总和（含 provider 流式等待）；tokens 为 prompt+output+reasoning 合计。</div>

<h2>7. 专题：PWN 专项（R4）实例重建风暴</h2>
<div class="sub">GZCTF closeTime 毫秒时间戳解析失败 → 默认 300s 续约 → 中文报错「容器时间尚不可延长」未归类 → 销毁重建 → 端口漂移。每次重建向成员推送新一代 target_instance。</div>
{build_rebuild_storm(d, A)}

<h2>8. 协同内容样本</h2>
<h3>recon 消息样本（服务 → 成员）</h3>
<pre>{esc(msg_sample)}</pre>
<h3>耗时最长的 14 条 intent</h3>
{intent_table}
<h3>facts 样本（前 10 条）</h3>
{facts_table}
<h3>memories 经验沉淀</h3>
{mem_chart}
<h3>WP 题解流水线</h3>
<div class="note">wp jobs 状态分布：{esc(wp_jobs_str)}。WP 成员在题目 solved 后被派发撰写题解，质量门禁（缺代码块/占位符/空内容）拒收后按有界退避重试。</div>

<h2>9. 结论</h2>
<ul>
<li><b>协同拓扑为星型单向</b>：服务→成员 recon 推送 + diamond→成员 intent 指派构成全部跨 Agent 通信；成员→成员业务消息为 0，黑板 facts 以静态题目信息为主，共享工作区几乎未用。</li>
<li><b>并行度来自席位制而非消息协作</b>：R3 同时推进 65 题靠 primary 席位并行单干 + WP 流水线；helper 交叉验证机制存在但退化为冗余备份。</li>
<li><b>协同延迟整体健康</b>：intent 生命周期 p50 {fmt_dur(im["dur_p50"])}、LLM turn p50 {fmt_dur(tn["dur_p50"])}、WP 撰写 p50 {fmt_dur(wm["lat_p50"])}；主要时间风险是 provider 流式悬挂（最大 turn {fmt_dur(tn["dur_max"])}）与实例重建风暴（R4/R5 端口漂移见第 4、7 节）。</li>
<li><b>效率演进明显</b>：R3 23/65（35%）→ R5 17/20（85%），PWN 专项在重建风暴下仍 6/7；说明核心链路（同步→分配→解题→提交→WP）已稳定，剩余瓶颈在基础设施（HTTP 控制面、实例续约、MCP）。</li>
<li><b>改进优先级</b>：① 修 P2 实例续约（根因已定位）；② 给 Member 补消息发送工具（授权层已就绪）；③ helper 模板携带分工契约 + 引导 /workspace/shared 产物交换；④ HTTP 控制面与引擎隔离（P1）。</li>
</ul>

</div></body></html>"""
    return html


if __name__ == "__main__":
    data = json.loads(OUT_JSON.read_text(encoding="utf-8"))
    print("analyzing ...")
    A = analyze(data)
    print("building html ...")
    html_doc = build_html(data, A)
    OUT_HTML.write_text(html_doc, encoding="utf-8")
    print(f"wrote {OUT_HTML} ({OUT_HTML.stat().st_size} bytes)")
