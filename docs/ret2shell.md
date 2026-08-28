# ret2shell 平台适配

适配 [ret2shell](https://github.com/ret2shell/ret2shell)（Rust/axum 开源 CTF 平台）。
**已对真实平台验证通过**：西电 `https://ctf.xidian.edu.cn`（MoeCTF 2026，game 37，125 题），
端到端冒烟（导入 → 解题 → flag → writeup）已跑通。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `IPC_R2S_BASE_URL` | `https://ctf.xidian.edu.cn` | 平台根地址 |
| `IPC_R2S_GAME_ID` | `37` | 比赛 id（数字） |
| `IPC_R2S_USERNAME` / `IPC_R2S_PASSWORD` | 空 | 参赛账号（登录换 JWT Bearer token） |
| `IPC_R2S_TOKEN` | 空 | 已有的 Bearer token（可替代账密） |

Docker Compose 部署时把这些写进项目根目录的 `.env`（compose 自动读取并注入
ipc-app 容器；注意 `.env` 不能带 UTF-8 BOM，PowerShell `Set-Content` 后需检查）。
**认证细节**：登录需要验证码时客户端自动走 `GET /api/account/captcha/cli` 的
PoW（难度 4：答案以 challenge 随机串开头、其 SHA-256 十六进制前 4 位为 0），
无需人工介入；token 在响应头 `Set-Token` 返回并自动滚动更新。
**密码连续错 5 次账号冻结 30 分钟**。

## 使用流程

```bash
export PYTHONPATH=.
export IPC_R2S_USERNAME=... IPC_R2S_PASSWORD=...

# 1) 只读冒烟：登录 + 比赛状态 + 题目列表
python scripts/ret2shell_platform_check.py

# 2) 拉取题目清单（含分值/分类/已解状态）到 platform_targets/ret2shell.yaml
python scripts/fetch_ret2shell_targets.py

# 3) 批量导入题目为项目。附件下载与数据库写入已分离（两阶段），
#    但大括量仍建议分批（脚本每批 15 题、自动重试）：
docker cp scripts/import_ret2shell_batch.py ipc-app:/tmp/
docker exec ipc-app python /tmp/import_batch.py <admin_session_cookie>
#    或直接对运行中的后端调 POST /api/platform/import（mapping 见下）。

# 4) Member 在沙箱里通过 ret2shell MCP 管理动态靶机（pwn 题拿 host:port）

# 5) 提交 flag：默认 dry-run，--no-dry-run 才真正提交
python scripts/ret2shell_submit.py --challenge <id> --flag 'flag{...}'
```

**运维代理（IPC 对话）路径**：ops agent 的平台工作流也支持
`challenges.platform: "ret2shell"`（backend/ops/service.py 的 `_adapter` 分支），
在 IPC 对话里提交含 `"challenges": {"platform": "ret2shell", "game_id": 37}`
的 workflow 描述并确认后，agent 可自行拉题导入。凭据始终只从环境变量读取。

**中文分类映射**（MoeCTF 2026 实测）：Web 安全与渗透测试→web、
现代密码学→crypto、二进制漏洞审计 / Python 沙箱逃逸→pwn、
软件逆向工程→reverse、大语言模型应用安全→ai、其余（取证杂项/从此开始/
开发运维基础/策略博弈/美工设计）→misc。

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

## 已知限制与运维注意

- **RAM 黑板**：解题图谱/项目列表在内存里，`docker compose restart` 会清空
  项目（附件文件保留在 `projects/`，重新导入即恢复，external_id 对应关系不变）。
  比赛期间尽量避免重启容器。
- **SQLite 锁**：memdb 不支持 WAL，长事务与 UI 轮询互斥。导入已改为
  两阶段（先下载后写库），但请勿在导入进行时并行发起第二个导入。
- **附件去重**：导入脚本超时重试可能产生重复项目（同一 external_id 多个
  project），可按 external_id 保留最早一个、DELETE 其余。
