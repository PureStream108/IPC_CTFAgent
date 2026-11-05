from __future__ import annotations

import json
import re
from typing import Any

_FLAG_RE = re.compile(
    r"(?i)\b(?:flag|ctf|htb|picoctf|gctf|0ctf|n1ctf|rwctf)\{[^}\r\n]{1,240}\}"
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_IP_PORT_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b")
_HTTP_RE = re.compile(r"\bHTTP/\d(?:\.\d)?\s+\d{3}\b")
_ERROR_RE = re.compile(
    r"(?i)(traceback|exception|error|failed|failure|denied|refused|timed? ?out|not found|no such file|"
    r"segmentation fault|killed|cannot|unable to)"
)
_CRED_RE = re.compile(
    r"(?i)\b(password|passwd|passphrase|token|api[_-]?key|secret|cookie)\b\s*[:=]\s*([^\s,;]{3,200})"
)
_MAX_LINE = 240
_MAX_EXCERPT = 2200


def _redact_text(text: str) -> str:
    return _CRED_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", str(text or ""))


def _clip(value: Any, limit: int = _MAX_LINE) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _line_hits(text: str, pattern: re.Pattern[str], *, limit: int = 12) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    seen: set[str] = set()
    for number, line in enumerate(text.splitlines(), 1):
        if not pattern.search(line):
            continue
        value = _clip(line)
        if not value or value in seen:
            continue
        seen.add(value)
        hits.append((number, value))
        if len(hits) >= limit:
            break
    return hits


def _unique(values: list[str], limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def build_deterministic_digest(
    job: dict[str, Any],
    stdout: str = "",
    stderr: str = "",
    *,
    output_truncated: bool = False,
    error: str = "",
) -> dict[str, Any]:
    """Extract a compact, evidence-preserving digest of one finished job.

    This layer is intentionally model-free: it must work when the curator LLM
    is unavailable, rate-limited, or returns invalid output. It never invents
    a result and never drops the verified flag when one is present.
    """

    text = "\n".join(part for part in (stdout, stderr) if part)
    flags = _unique(_FLAG_RE.findall(text), 5)
    urls = _unique(_URL_RE.findall(text), 8)
    ips = _unique(_IP_PORT_RE.findall(text), 8)
    http = _unique(_HTTP_RE.findall(text), 6)
    errors = [(number, _redact_text(line)) for number, line in _line_hits(text, _ERROR_RE, limit=8)]
    credentials = [
        (number, f"{match.group(1)}=[REDACTED]")
        for number, line in enumerate(text.splitlines(), 1)
        for match in [_CRED_RE.search(line)]
        if match is not None
    ][:5]

    exit_code = job.get("exit_code")
    state = str(job.get("state") or "unknown")
    command = _clip(_redact_text(job.get("command") or ""), 500)
    byte_count = len(text.encode("utf-8", "replace"))
    if state == "lost":
        verdict = "unknown"
        summary = "后台进程已丢失，未记录退出状态；不要把该结果当作成功。"
    elif state == "cancelled":
        verdict = "cancelled"
        summary = "后台任务在执行中被取消，输出可能不完整。"
    elif exit_code in (124, 137):
        verdict = "unknown"
        summary = "后台任务超时或被强制终止，输出可能不完整。"
    elif exit_code == 0:
        verdict = "success"
        summary = "后台任务执行完成，退出码为 0。"
    else:
        verdict = "failed"
        summary = f"后台任务执行结束，退出码为 {exit_code}。"

    findings: list[dict[str, Any]] = []
    for flag in flags:
        findings.append({"type": "flag", "value": flag, "evidence": "stdout/stderr", "confidence": 0.98})
    for url in urls[:4]:
        findings.append({"type": "endpoint", "value": url, "evidence": "stdout/stderr", "confidence": 0.8})
    for address in ips[:4]:
        findings.append({"type": "endpoint", "value": address, "evidence": "stdout/stderr", "confidence": 0.7})
    for status in http[:3]:
        findings.append({"type": "http_status", "value": status, "evidence": "stdout/stderr", "confidence": 0.8})
    for number, line in errors[:6]:
        findings.append({"type": "error", "value": line, "evidence": f"line {number}", "confidence": 0.7})
    for number, value in credentials:
        findings.append({"type": "credential", "value": value, "evidence": f"line {number}", "confidence": 0.6})

    if output_truncated:
        summary += " 输出超过阅读上限，已按头尾采样；完整原文在 job_logs/artifact 中。"
    if error:
        summary += f" Curator 错误：{_clip(error, 300)}"

    excerpt = _redact_text(text)
    if len(excerpt) > _MAX_EXCERPT:
        head = _MAX_EXCERPT // 2
        tail = _MAX_EXCERPT - head
        excerpt = excerpt[:head] + "\n...[excerpt clipped]...\n" + excerpt[-tail:]
    if not excerpt.strip():
        excerpt = "（无 stdout/stderr 输出）"

    suggestions: list[str] = []
    if flags:
        suggestions.append("如果该 flag 尚未验证，先用实际证据提交/验证。")
    if errors:
        suggestions.append("检查错误行是否表示环境、网络或权限问题，避免原样重跑。")
    if verdict == "unknown":
        suggestions.append("读取 job_status/job_logs 确认最终状态后再决定是否重试。")
    if output_truncated:
        suggestions.append("需要更多上下文时用 job_logs offset=… 分段读取，不要把全文塞回上下文。")

    return {
        "source": "deterministic",
        "summary_zh": summary,
        "verdict": verdict,
        "command": command,
        "member": job.get("member") or "",
        "job_id": job.get("id") or "",
        "exit_code": exit_code,
        "state": state,
        "output_bytes": byte_count,
        "output_truncated": bool(output_truncated),
        "key_findings": findings,
        "useful_commands": [],
        "suggested_next": suggestions,
        "needs_raw_output": bool(output_truncated or byte_count > 4000),
        "excerpt": excerpt,
        "error": _clip(error, 500),
    }


def parse_curator_response(raw: Any) -> dict[str, Any] | None:
    """Parse the curator's JSON reply, tolerating a Markdown code fence."""

    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    summary = value.get("summary_zh") or value.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    findings = value.get("key_findings")
    if not isinstance(findings, list):
        findings = []
    cleaned_findings = []
    for item in findings[:12]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "other")[:40]
        finding_value = item.get("value")
        if finding_value in (None, ""):
            continue
        cleaned_findings.append(
            {
                "type": kind,
                "value": str(finding_value)[:1000],
                "evidence": str(item.get("evidence") or "")[:500],
                "confidence": item.get("confidence"),
            }
        )
    return {
        "summary_zh": str(summary).strip()[:2000],
        "verdict": str(value.get("verdict") or "unknown")[:40],
        "key_findings": cleaned_findings,
        "useful_commands": [str(item)[:500] for item in (value.get("useful_commands") or [])[:8] if str(item).strip()],
        "suggested_next": [str(item)[:500] for item in (value.get("suggested_next") or [])[:8] if str(item).strip()],
        "needs_raw_output": bool(value.get("needs_raw_output", False)),
        "risk_flags": [str(item)[:200] for item in (value.get("risk_flags") or [])[:5] if str(item).strip()],
    }


def _merge_findings(deterministic: list[Any], curated: list[Any]) -> list[Any]:
    """Keep deterministic flags/errors even when the curator omits them."""

    result: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for item in [*deterministic, *curated]:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("type") or ""), str(item.get("value") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result[:16]


def merge_curator_digest(
    deterministic: dict[str, Any],
    curator: dict[str, Any] | None,
) -> dict[str, Any]:
    """Put curator text over deterministic facts without losing evidence."""

    if not curator:
        return deterministic
    merged = dict(deterministic)
    merged["source"] = "sugilite"
    merged["summary_zh"] = str(curator.get("summary_zh") or deterministic["summary_zh"])
    merged["verdict"] = str(curator.get("verdict") or deterministic["verdict"])
    deterministic_findings = deterministic.get("key_findings") or []
    allowed_flags = {
        str(item.get("value") or "")
        for item in deterministic_findings
        if isinstance(item, dict) and item.get("type") == "flag"
    }
    curated_findings = [
        item for item in (curator.get("key_findings") or [])
        if not (
            isinstance(item, dict)
            and item.get("type") == "flag"
            and str(item.get("value") or "") not in allowed_flags
        )
    ]
    merged["key_findings"] = _merge_findings(
        deterministic_findings,
        curated_findings,
    )
    merged["useful_commands"] = list(curator.get("useful_commands") or [])
    merged["suggested_next"] = list(curator.get("suggested_next") or deterministic["suggested_next"])
    merged["needs_raw_output"] = bool(curator.get("needs_raw_output", deterministic["needs_raw_output"]))
    merged["risk_flags"] = list(curator.get("risk_flags") or [])
    return merged


def format_job_digest(job_or_digest: dict[str, Any], *, max_chars: int = 2600) -> str:
    """Render one digest for a Member's context or a bump."""

    job = job_or_digest or {}
    digest = job.get("digest_json") if isinstance(job.get("digest_json"), dict) else job
    job_id = str(job.get("id") or digest.get("job_id") or "")
    member = str(job.get("member") or digest.get("member") or "")
    command = _clip(_redact_text(job.get("command") or digest.get("command") or ""), 320)
    exit_code = job.get("exit_code", digest.get("exit_code"))
    state = str(job.get("state") or digest.get("state") or "unknown")
    lines = [
        f"[Sugilite 任务摘要] {job_id}" + (f" / {member}" if member else ""),
        f"命令：{command}",
        f"结果：{digest.get('summary_zh') or '（无摘要）'}",
        f"状态：{state}，退出码：{exit_code if exit_code is not None else '未知'}",
    ]
    findings = digest.get("key_findings") or []
    if findings:
        lines.append("关键线索：")
        for item in findings[:10]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "other")
            value = _clip(item.get("value") or "", 500)
            evidence = _clip(item.get("evidence") or "", 120)
            lines.append(f"- [{kind}] {value}" + (f"（{evidence}）" if evidence else ""))
    suggested = digest.get("suggested_next") or []
    if suggested:
        lines.append("建议：")
        for item in suggested[:6]:
            lines.append(f"- {_clip(item, 300)}")
    lines.append(f"原始输出：job_logs job_id={job_id} offset=0（必要时分段读取）")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max(1, max_chars - 1)] + "…"
    return text


__all__ = [
    "build_deterministic_digest",
    "format_job_digest",
    "merge_curator_digest",
    "parse_curator_response",
]
