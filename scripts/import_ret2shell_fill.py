import json
import sys
import time
import urllib.request
from collections import Counter

session = sys.argv[1]
mapping = {
    "platform": "ret2shell",
    "game_id": 37,
    "category_map": {
        "Web 安全与渗透测试": "web",
        "取证与安全杂项": "misc",
        "现代密码学": "crypto",
        "二进制漏洞审计": "pwn",
        "Python 沙箱逃逸": "pwn",
        "软件逆向工程": "reverse",
        "从此开始": "misc",
        "开发与运维基础": "misc",
        "策略与博弈": "misc",
        "大语言模型应用安全": "ai",
        "美工设计": "misc",
    },
}
headers = {"Content-Type": "application/json", "Cookie": f"ipc_session={session}"}


def api(path, body=None, method="GET", timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:8000{path}",
        data=data,
        headers=headers,
        method=method if body is not None else method,
    )
    r = urllib.request.urlopen(req, timeout=timeout)
    raw = r.read()
    return json.loads(raw) if raw else {}


# 当前 DB 内的 external_id 集合
existing = set()
for p in api("/projects"):
    if p.get("external_id"):
        existing.add(p["external_id"])
print("existing:", len(existing), flush=True)

challenges = api("/api/platform/challenges", {"mapping": mapping}, method="POST")["challenges"]
missing = [c["external_id"] for c in challenges if c["external_id"] not in existing]
print("missing:", len(missing), missing, flush=True)

imported, failed = 0, []
for ext in missing:
    try:
        result = api(
            "/api/platform/import",
            {"mapping": mapping, "select": [ext]},
            method="POST",
            timeout=180,
        )
        imported += len(result["imported"])
        print(f"+{ext} ok (total {imported})", flush=True)
    except Exception as exc:
        failed.append(ext)
        print(f"{ext} FAILED: {type(exc).__name__} {exc}", flush=True)
print("DONE imported:", imported, "failed:", failed, flush=True)

# 去重：同一 external_id 保留最早的
projects = api("/projects")
by_ext = {}
dups = []
for p in sorted(projects, key=lambda x: x["id"]):
    ext = p.get("external_id")
    if ext in by_ext:
        dups.append(p["id"])
    else:
        by_ext[ext] = p["id"]
print("unique:", len(by_ext), "dups:", len(dups), flush=True)
for pid in dups:
    try:
        api(f"/projects/{pid}", method="DELETE")
    except Exception as exc:
        print("del fail", pid, exc, flush=True)
print("dedup done", flush=True)
