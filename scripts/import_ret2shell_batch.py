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


def post(path, body, timeout=300):
    req = urllib.request.Request(
        f"http://127.0.0.1:8000{path}",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    r = urllib.request.urlopen(req, timeout=timeout)
    return json.load(r)


challenges = post("/api/platform/challenges", {"mapping": mapping})["challenges"]
ids = [c["external_id"] for c in challenges]
print("total:", len(ids), dict(Counter(c["category"] for c in challenges)), flush=True)

BATCH = 15
imported_total = 0
for start in range(0, len(ids), BATCH):
    batch = ids[start : start + BATCH]
    for attempt in range(3):
        try:
            result = post("/api/platform/import", {"mapping": mapping, "select": batch})
            imported_total += len(result["imported"])
            print(f"batch {start // BATCH + 1}: +{len(result['imported'])} (total {imported_total})", flush=True)
            break
        except Exception as exc:
            print(f"batch {start // BATCH + 1} attempt {attempt + 1} failed: {exc}", flush=True)
            time.sleep(5)
    else:
        print(f"batch {start // BATCH + 1}: giving up", flush=True)
print("DONE imported_total:", imported_total, flush=True)
