# AGENTS.md — 本地 TSecBench 测评启动手册

本文件只描述 **本地任务镜像模式**：

- App / PostgreSQL / 题目容器都在本机 Docker Desktop 中运行
- 题目容器使用 `ipc-task:latest`
- OVPN 运行在 `ipc-vpn` sidecar 内，**不要**在 Windows 上直接开 OpenVPN Connect
- 适用于 Windows + PowerShell + Docker Desktop

仓库根目录：

```text
D:\Desktop\Codex\IPC_CTFAgent\IPC_CTFAgent
```

---

## 1. 启动前检查

```powershell
Set-Location D:\Desktop\Codex\IPC_CTFAgent\IPC_CTFAgent

# Docker Desktop 必须已经启动
docker info

# 本地镜像应存在
docker images | Select-String "ipc-(ctfagent|task|vpn)"

# 查看是否有残留容器 / 旧评测
docker ps -a
```

需要的输入：

| 输入 | 说明 |
|---|---|
| `.ovpn` | 平台为**当前 task** 下发的配置，不能跨 task 复用 |
| `BENCHMARK_TOKEN` | 当前 task 的 benchmark token |
| `BENCHMARK_BASE_URL` | 通常是 `https://tsecbench.zc.tencent.com` |
| `testconfigkey.txt` | 模型 key / Base URL / model / reasoning 配置 |

> `.ovpn`、token、`testconfigkey.txt` 都是敏感文件，不要提交到 git，也不要打印到日志。

如果本机没有 `ipc-vpn:latest`，先构建 sidecar 镜像：

```powershell
docker build -t ipc-vpn:latest D:\Desktop\Codex\IPC_CTFAgent\vpn-helper
```

---

## 2. 启动 OVPN sidecar

### 2.1 原理

`ipc-vpn` 使用：

```text
--network host
--cap-add NET_ADMIN
--device /dev/net/tun
```

Docker Desktop 下这里的 `host` 是 **Docker Desktop 的 Linux VM**，不是 Windows 主机。需要在 Docker Desktop 设置中开启 host networking（4.34+）。
因此：

- `tun0` 和 `10.x` 路由只存在于 Docker VM
- Windows 本机路由表 / DNS 不会被 VPN 改写
- bridge 网络中的 `ipc-app` / `ipc-task` 容器通过 VM 路由进入 `tun0`

不要同时启动 Windows 的 OpenVPN Connect，否则会抢 `10.254.1.0/24` 路由。

### 2.2 启动命令

```powershell
$ROOT = "D:\Desktop\Codex\IPC_CTFAgent\IPC_CTFAgent"
$OVPN = "C:\Users\Administrator\Downloads\task_xxx_vpn_config.ovpn"

Set-Location $ROOT

docker rm -f ipc-vpn 2>$null

docker run -d --name ipc-vpn --restart unless-stopped `
  --network host `
  --cap-add NET_ADMIN `
  --device /dev/net/tun `
  -v "${OVPN}:/config.ovpn:ro" `
  ipc-vpn --config /config.ovpn
```

等待初始化：

```powershell
$ok = $false
for ($i = 0; $i -lt 20; $i++) {
  Start-Sleep -Seconds 2
  $log = docker logs ipc-vpn 2>&1 | Out-String
  if ($log -match "Initialization Sequence Completed") { $ok = $true; break }
  if ($log -match "TLS handshake failed|AUTH_FAILED|Cannot open TUN") { break }
}

"vpn_ready=$ok"
docker logs ipc-vpn --tail 40
```

必须看到：

```text
TUN/TAP device tun0 opened
net_addr_v4_add: 10.254.1.x/24 dev tun0
net_route_v4_add: 10.0.128.0/18 via 10.254.1.1 dev [NULL]
Initialization Sequence Completed
```

检查路由：

```powershell
docker exec ipc-vpn sh -lc "ip route"
```

应包含：

```text
10.254.1.0/24 via 10.254.1.1 dev tun0
10.0.128.0/18 via 10.254.1.1 dev tun0
```

### 2.3 OVPN 失败处理

| 日志 | 原因 | 处理 |
|---|---|---|
| `TLS handshake failed` | 配置已过期，或对应 task 已结束 | 回平台重新下载当前 task 的 `.ovpn`，不要复用旧文件 |
| `AUTH_FAILED` | 证书 / 配置不匹配 | 重新下载当前 task 的 `.ovpn` |
| `Cannot open TUN` | 缺少 `/dev/net/tun` 或 `NET_ADMIN` | 检查 `docker run` 参数 |
| 容器 Up 但 `ip route` 没有 `10.x` | host networking 未生效 | 检查 Docker Desktop 是否开启 host networking；看 `best_gw` 是否为 `192.168.65.1` |

如果 OVPN 日志正常，但 task 容器连不上目标，再检查题目地址是否已被平台回收，不要反复重连 VPN。

---

## 3. 启动本地 App

```powershell
Set-Location $ROOT
docker compose up -d --no-build
docker compose ps -a
```

正常状态：

```text
ipc-app                         Up    0.0.0.0:8000->8000/tcp
ipc_ctfagent-ipc-postgres-1     Up (healthy)
ipc_ctfagent-ipc-task-image-1   Exited (0)   # 这是构建用的一次性服务，正常退出
```

检查 App：

```powershell
Invoke-WebRequest -Uri http://127.0.0.1:8000/config -UseBasicParsing
```

返回 200 即可。

### 代码更新后的镜像

`ipc-task` 的 Dockerfile 会把 `backend/` 复制进 `/opt/ipc/backend`，所以 **backend 代码更新后要重建 task 镜像**：

```powershell
# 只改前端 / app：重建 app
docker compose build ipc-app

# 改了 backend / docker/member/Dockerfile：两个都重建
docker compose build ipc-app ipc-task-image
```

---

## 4. 配置模型

模型配置来源是仓库根目录的 `testconfigkey.txt`。

文件里通常有三把 key：

| 用途 | 说明 |
|---|---|
| Member | 十个 Member 席位共用 |
| IPC | Web / Ops 行动代理 |
| Diamond | Diamond 规划器 |

配置项：

```text
api_format:       openai
base_url:         https://opencode.ai/zen/go/v1/
model:            deepseek-v4.1-flash
reasoning_effort: max
```

### 4.1 通过 API 配置

不要把 key 写进命令历史或输出。使用环境变量占位：

```powershell
$MEMBER_KEY = "<Member key>"
$DIAMOND_KEY = "<Diamond key>"
$BASE = "https://opencode.ai/zen/go/v1/"
```

配置 App 的 Member / Diamond：

```powershell
$body = @{
  member = @{
    api_format = "openai"; api_surface = "auto"; reasoning_effort = "max"
    api_key = $MEMBER_KEY; base_url = $BASE; model = "deepseek-v4.1-flash"
  }
  diamond = @{
    api_format = "openai"; api_surface = "auto"; reasoning_effort = "max"
    api_key = $DIAMOND_KEY; base_url = $BASE; model = "deepseek-v4.1-flash"
  }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:8000/config" `
  -ContentType "application/json" -Body $body
```

配置 Ops / IPC：

```powershell
$ops = @{
  api_format = "openai"; api_surface = "auto"; reasoning_effort = "max"
  api_key = $MEMBER_KEY; base_url = $BASE; model = "deepseek-v4.1-flash"
} | ConvertTo-Json

Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:8000/api/ops/config" `
  -ContentType "application/json" -Body $ops
```

健康检查：

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/config/health"
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/ops/config/health"
```

两个都必须返回 `ok: true`。如果 401/402，先查 key 余额和模型名，不要启动评测。

---

## 5. 创建 Workflow 并启动

### 5.1 创建

```powershell
$TOKEN = "<BENCHMARK_TOKEN>"
$BASE_URL = "https://tsecbench.zc.tencent.com"

$wfBody = @{
  base_url = $BASE_URL
  name = "TSecBench Local <date>"
  remote_concurrency = 3
  allow_private_networks = $true
  allow_hints = $false
  secrets = @{ benchmark_token = $TOKEN }
} | ConvertTo-Json -Depth 5

$wf = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/ops/workflows/tsecbench" `
  -ContentType "application/json" -Body $wfBody

$wf.id
$wf.confirmation_phrase
```

### 5.2 确认

```powershell
$confirm = @{ confirmation_phrase = $wf.confirmation_phrase } | ConvertTo-Json
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/ops/workflows/$($wf.id)/confirm" `
  -ContentType "application/json" -Body $confirm
```

### 5.3 预检

```powershell
$pre = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/workflows/$($wf.id)/preflight"
$pre.challenge_count
$pre.network
```

要求：

- `challenge_count > 0`
- `network.api_reachable = true`
- token 对应的题目全部出现（未完成）

### 5.4 启动

```powershell
$startBody = @{
  idempotency_key = "tsecbench-local-" + [guid]::NewGuid().ToString("N").Substring(0,12)
} | ConvertTo-Json

$run = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/workflows/$($wf.id)/start" `
  -ContentType "application/json" -Body $startBody

$run.id
```

如果报：

```text
another competition is active
```

说明旧 run 还占着 competition 锁。先 stop 旧 run：

```powershell
$old = Invoke-RestMethod "http://127.0.0.1:8000/api/runs/<OLD_RUN_ID>"
$body = @{ revision = $old.run.revision } | ConvertTo-Json
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/runs/<OLD_RUN_ID>/stop" `
  -ContentType "application/json" -Body $body
```

不要通过删除数据库或 volume 来解决锁冲突。

---

## 6. 启动正确性检查

启动后至少确认以下四项，再让评测自行运行。

### 6.1 Run 状态

```powershell
$runId = "<RUN_ID>"
$state = Invoke-RestMethod "http://127.0.0.1:8000/api/runs/$runId"
$state.run.status
$state.run.last_error
($state.challenges | Group-Object state | Select-Object Name,Count)
```

要求：

- `status = running`
- `last_error = null`
- 至少一个 `solving`

### 6.2 题目实例

```powershell
docker exec ipc_ctfagent-ipc-postgres-1 psql -U ipc -d ipc -c `
"SELECT c.external_id,i.state,i.metadata->>'entry'
 FROM competition_challenges c
 JOIN competition_instances i ON i.challenge_id=c.id
 WHERE c.run_id='$runId' ORDER BY c.external_id;"
```

看到 `ready/running` 和 `10.0.x.x:port` 即正常。

### 6.3 从 task 容器访问靶场

先找到 task 容器：

```powershell
docker ps --format "{{.Names}}|{{.Status}}" | Select-String "ipc-task"
```

测试：

```powershell
docker exec ipc-task-proj_XXX sh -lc "timeout 5 bash -c '</dev/tcp/10.0.x.x/80' && echo OK || echo FAIL"
```

必须是 `OK`。如果 VPN 容器内可以访问而 task 容器不行，检查 target 是否已过期/回收，不要先怀疑模型。

### 6.4 SugiliteCurator

competition 项目应出现：

```text
event=curator_started
member=sugilite
role=curator
dedicated_seat=false
```

同时 `sugilite` 仍可作为 Member 被分配；这两者不冲突。

---

## 7. 十分钟监测

用户要求“启动后监测十分钟，没问题就停”，按此执行：

- 每 30–60 秒查询一次 `GET /api/runs/{id}`
- 记录 `status`、`last_error`、`solved`、`solving`
- 检查 `ipc-vpn` 日志没有 TLS 重连/失败
- 检查 `ipc-app` 仍为 Up
- 满 10 分钟且无错误后，停止监控，**不要**继续轮询

建议最小指标：

```text
status      = running
last_error  = None
solved      > 0
solving     >= 1
VPN         = Initialization Sequence Completed，无新的 TLS handshake failed
task TCP    = OK
```

满足后让 run 自行运行，不暂停、不重启容器。

---

## 8. 暂停 / 恢复

暂停所有运行中的容器：

```powershell
docker ps -q | ForEach-Object { docker pause $_ }
```

恢复：

```powershell
docker ps -q | ForEach-Object { docker unpause $_ }
```

这是冻结，不是删除；容器和进程状态保留。

如需停止整轮评测，优先调用 run API：

```powershell
$r = Invoke-RestMethod "http://127.0.0.1:8000/api/runs/$runId"
$body = @{ revision = $r.run.revision } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/runs/$runId/stop" `
  -ContentType "application/json" -Body $body
```

不要执行 `docker compose down -v`，会删除 PostgreSQL volume 和历史数据。

---

## 9. 常见问题

| 现象 | 处理 |
|---|---|
| `docker ... pipe ... not found` | Docker Desktop 未启动；启动后等 `docker info` 成功 |
| `ipc-app` 起不来 | `docker compose ps -a`；看 `docker logs ipc-app`；确认端口 8000 未被占用 |
| `ipc-task-image` 状态 Exited(0) | 正常，它只是构建服务 |
| VPN 一直 `TLS handshake failed` | 重新下载当前 task 的 `.ovpn`；旧 task 的配置不能用 |
| task 容器连不上 target | 先查平台实例是否 `ready`、地址是否已回收；再测 VPN 容器自身 |
| `another competition is active` | stop 旧 run，释放 competition 锁 |
| `the benchmark token is not configured` | 重新创建 workflow 并写入当前 token；旧 workflow secret 已丢 |
| 模型 401/402 | 检查 `testconfigkey.txt` 的 key、余额、model 名 |
| 评测中途没有新题 | 检查 run 是否 `running`、remote concurrency、平台实例数量；不要重启 App 解决 |

---

## 10. 禁止事项

- 不要在 Windows 上同时运行 OpenVPN Connect 和 `ipc-vpn`。
- 不要跨 task 复用 `.ovpn` 或 `BENCHMARK_TOKEN`。
- 不要把 `.ovpn`、token、`testconfigkey.txt` 写入 git、镜像或最终回复。
- 不要删除 PostgreSQL volume 来“重置”评测。
- 不要在平台地址上做扫描或提交测试；只访问平台下发的 `container_addr`。
- 不要为了排查而无限重连 VPN；先确认 task 是否仍有效。
