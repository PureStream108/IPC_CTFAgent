# ret2shell 平台适配

适配 [ret2shell](https://github.com/ret2shell/ret2shell)（Rust/axum 开源 CTF 平台），
默认指向西电平台 `https://ctf.xidian.edu.cn`（game 37）。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `IPC_R2S_BASE_URL` | `https://ctf.xidian.edu.cn` | 平台根地址 |
| `IPC_R2S_GAME_ID` | `37` | 比赛 id（数字） |
| `IPC_R2S_USERNAME` / `IPC_R2S_PASSWORD` | 空 | 参赛账号（登录换 JWT Bearer token） |
| `IPC_R2S_TOKEN` | 空 | 已有的 Bearer token（可替代账密） |

认证细节：登录 `POST /api/account/login` 后 token 在响应头 `Set-Token` 中返回，
客户端会自动滚动更新；密码错误 5 次账号冻结 30 分钟。

## 使用流程

```bash
export PYTHONPATH=.
export IPC_R2S_USERNAME=... IPC_R2S_PASSWORD=...

# 1) 只读冒烟：登录 + 比赛状态 + 题目列表
python scripts/ret2shell_platform_check.py

# 2) 拉取题目清单（含分值/分类/已解状态）到 platform_targets/ret2shell.yaml
python scripts/fetch_ret2shell_targets.py

# 3) 导入题目为项目（走通用平台导入 API，凭据只从环境变量读取）
curl -X POST http://127.0.0.1:8000/api/platform/import \
  -H 'Content-Type: application/json' \
  -b <auth cookie> \
  -d '{"mapping": {"platform": "ret2shell", "game_id": 37}}'

# 4) Member 在沙箱里通过 ret2shell MCP 管理动态靶机（pwn 题拿 host:port）

# 5) 提交 flag：默认 dry-run，--no-dry-run 才真正提交
python scripts/ret2shell_submit.py --challenge <id> --flag 'flag{...}'
```

## 关键契约（与 GZCTF 的差异）

- **异步判题**：`POST .../submit` 返回 `solved: null` 的 Submission，客户端轮询
  `GET .../submit?id=` 直到 `solved != null`（默认 1s × 7 次）。
- **提交限额**：每账号 5 分钟 10 次（HTTP 429）。客户端内置同额度限速器，
  提交前还会做已解出 preflight，避免浪费平台尝试次数。
- **动态靶机**：`POST/PATCH/DELETE .../instance` 启动/续期/销毁，
  `GET /api/game/{gid}/instance` 轮询 `exposed_ports` 拿实际连接地址。
- 列表接口返回 `[items, total]` 二元组；错误响应是纯文本 body。

## Member 侧能力（ret2shell MCP）

配置了 `IPC_R2S_USERNAME`（或 `IPC_R2S_TOKEN`）后，后端自动向所有 Member 注册
`ret2shell` MCP（凭据留在后端，不进沙箱）：

- `instance_start(challenge_id)`：启动并等待靶机可达，返回 endpoints
- `instance_status` / `instance_renew` / `instance_stop`
- `challenge_status(challenge_id)`：已解状态与全站解出数

**flag 提交不暴露给 Member**——共享限额只由操作者经 `scripts/ret2shell_submit.py`
消耗，避免 agent 误操作烧掉提交次数。
