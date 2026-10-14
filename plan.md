# IPC_CTFAgent 综合改进计划与验收状态

覆盖 `nextchange.md` 三项产品需求、`problem.md` B1–B6、D1/D2，以及覆盖率、PostgreSQL 隔离、浏览器 E2E 和真实 20 题监控。B7–B9、C 类观察项继续保留在 backlog。后端 secret 能力和旧 API 保留，前端 Workflow secret 人工入口移除。

## 当前完成度（2026-10-14 更新）

主要产品与架构改动已实现；Playwright 浏览器执行、覆盖率实跑与基线保存、PostgreSQL 集成测试、UI 排队原因、run 资源指标和完整 Ruff 已通过本机验证。GitHub required checks 已实际启用并验证对 main 生效。整体验收剩余 2 项：hosted CI 实跑、真实 20 题验收。具体步骤和完成条件见文末。

| 项目 | 当前状态 | 证据或剩余条件 |
| --- | --- | --- |
| Playwright compose E2E | 本机已通过 | 已使用系统 Chromium 执行浏览器流程；旧镜像下载阻塞已解决，见 `data/exports/e2e/` |
| 完整 pytest 与旧失败修复 | 本机已通过 | `769 passed, 4 skipped`，无 failure/error；旧容量、Proposed 名称、schema 隔离及 Windows 编码失败已修复 |
| 覆盖率门禁与基线 | 本机已通过 | Agent 81.33%、Competition 80.93%，基线保存于 `coverage-baseline.json` |
| PostgreSQL 集成与隔离 | 本机已通过 | 外部 PostgreSQL 全量回归、原生临时 server 25 项测试及 teardown 检查均通过 |
| UI 排队原因与 run 资源指标 | 已实现并纳入回归 | local/remote 数量、容量来源、使用量和剩余量已展示；真实平台观察纳入 20 题验收 |
| 完整 Ruff 规则集 | 本机已通过 | Windows 与 Linux 均执行 `backend scripts tests` 完整规则，未关闭规则；最终全量回归通过 |
| GitHub hosted CI | 待运行 | 需包含本轮改动的 revision 上三个 job 均通过 |
| GitHub required checks | 已启用并验证 | active ruleset `25067166` 对 main 要求 `test`、`e2e`、`task-image`，已回读适用分支规则 |
| 真实 20 题比赛 | 尚未启动，缺少目标 workflow | 需已确认 workflow 来源或目标比赛身份；fake E2E 不计入此验收 |

## 产品与架构范围

- Ops 会话绑定 `agent_workflow_id`；首次 proposal 创建 workflow，后续更新同一记录并使旧确认 token 失效。其他 workflow 的绑定请求返回冲突。清理重复 draft 时保留 run 引用和会话绑定记录。workflow 名称必须为实际比赛名，拒绝空名、`Proposed`、`probe` 等占位名。
- 问答使用紧凑的多行 textarea，支持中文 IME、换行、任意 Unicode 和自由输入；处理组合输入状态，展示原文，不使用 password 或 `******`。
- Member 确认 flag 后必须调用 `submit_flag`。`conclude` 中唯一 flag 候选生成一次幂等提交 follow-up；多候选只记录告警。
- 正确 verdict 经收尾事务补齐 synthetic completion edge、verified flag、solved 状态与 WP job。失败保留 submission 供重试，收尾成功后才释放席位。WP 指数退避、最多 5 次、退避上限 1 小时，最终失败释放席位。
- 普通题与容器题使用独立 local/remote capacity，Member 席位使用全局池。remote 上限依次取 run patch、workflow `remote_concurrency`、平台报告、旧 `remote_instance_limit`、生命周期平台默认 1。
- `/config` 更新 `max_concurrent_tasks`、`total_cpu`；`competition_runs.limits JSONB` 和 `PATCH /api/runs/{run_id}/limits` 支持 revision 控制。调度每个 tick 读取 limits，降低上限限制后续分配。
- preflight/run snapshot 展示普通题和容器题数量、上限、来源、使用量、剩余量与排队原因。UI 分组展示题目及平台提供的已解题分值。
- 容器不存在时标记 expired，已有席位的题目经退避进入 repair，重新发布入口和 generation。入口在 Member 启动前持久化给 primary/helper。控制操作等待 tick 锁最多 20 秒，超时返回冲突及 `Retry-After`；启动接管过期 run lease 并执行恢复 preflight。

## 本机已获得的证据

### PostgreSQL 与回归

最新完整 PostgreSQL 覆盖率运行为 `769 passed, 4 skipped`，保存的通过基线为：

4 个 skip 分别是宿主机缺少 Playwright 浏览器，以及 Windows 不支持的 POSIX IPC、gRPC UDS 和 POSIX 权限检查。浏览器流程另由 Docker E2E 实际执行；这 4 项不代表 PostgreSQL 集成测试被跳过。

| 模块组 | 行覆盖率 | 已覆盖 / 总行数 |
| --- | ---: | ---: |
| Agent 核心 | 81.33% | 3124 / 3841 |
| Competition | 80.93% | 2559 / 3162 |

最终容器 repair/入口时序改动已包含在该次全量运行中。`check_coverage.py` 门禁通过后才保存新基线。另有 3 个覆盖率门禁回归测试通过：修复两位小数基线比较，保留原始 80% floor，并拒绝缺失模块组。

新增回归验证了：

- 同一 flag 事实重复检查只创建一个提交 intent。
- 正常应用运行时，测试 schema 排除 `public`，迁移不会复用应用表；handoff 和唯一索引断言通过。
- Competition 已分配的任务不再被 Orchestrator 的通用 task 上限再次拦截。在 local 上限为 1 时，真实 Orchestrator 同时启动一道普通题和一道容器题，并释放资源。
- 真实 Member 的 target recon 事件早于首个工具调用。
- 实例不存在后，在退避到期前不重建，到期后按需 repair 并重新发布入口。
- provider 首个 SSE 事件包含只有工具调用、没有文本的响应。
- detached job 启动、状态探测、日志读取和取消的临时故障不会伪造成功或丢失重试游标，恢复后能继续收敛。
- correct verdict 先持久化供重试；verified flag、solved 状态、WP job 仅在同一收尾事务内成功后落地。验证或 WP 写入失败均回滚，保留席位；恢复后只建一个 WP，不再次请求平台提交。
- 平台外部解出题目后释放本地席位和实例，不重复 stop，不把未知候选当作 verified flag；平台配额探测故障保留 workflow 的旧容量回退。

最终证据保存在 `data/exports/pytest-final-plan-verification.log` 和 `.xml`。PostgreSQL 测试每例独立 schema 和 migration，外部回退配置为 `IPC_TEST_DATABASE_URL`。

临时 PostgreSQL 自动启动已在非 root Linux 容器中实际验证：不提供外部数据库 URL，由 `pytest-postgresql` 创建临时 server，25 个隔离/迁移/流式问答测试通过。证据为 `data/exports/native-postgres-verification.log/.xml`。每例跟踪并关闭连接池，检查活动连接为零，再删除并检查 schema；迁移失败、测试异常和未跟踪连接泄漏均有回归覆盖。`test_ops_stream.py` 已纳入隔离，部署环境的共享 artifact 路径不会覆盖测试临时目录。

排除部署凭据后的应用镜像作为基础重新构建 `ipc-postgres-tests:latest`，再次使用 `--require-postgres` 运行同一组测试：`25 passed`，无 skip、failure 或 error。最新日志和 XML 已覆盖保存到上述路径。

CI 使用 `--require-postgres`：PostgreSQL 不可用会失败，而不会把集成测试跳过后仍显示绿色；另有临时 server 的独立验证步骤。

### Docker Compose 与 task image

- [x] `ipc-task:latest` 构建成功。Ghidra 下载经镜像源完成，并通过 Dockerfile 中固定的 SHA-256 校验。
- [x] 应用镜像构建成功；本机验收使用 `IPC_INSTALL_WSRX=0`。
- [x] 正常 Compose 栈启动：PostgreSQL healthy、task-image 初始化容器退出码 0、应用 `/health` 返回 `ok`。
- [x] 完整 C5 工具验收通过，包括 Ghidra headless/Reverse MCP、radare2、SageMath、CTF Python 工具及 Playwright 包。
- [x] task image 浏览器策略和 AI CPU smoke 通过。

证据：`data/exports/task-image-build.log`、`task-image-acceptance.log`、`app-image-build.log`、`ai-smoke.log`、`compose-verification.json`。正常栈仍在 8000 端口运行；E2E 专用栈与卷已清理。最终回归后应用镜像已刷新，健康状态已重验。

`.dockerignore` 排除本地模型配置、环境文件及本机验收凭据。应用/task image 已重建，并实际检查镜像内不存在部署模型配置；运行应用继续从既有配置挂载读取凭据。

### Playwright 浏览器 E2E

- [x] fake platform、脚本化 mock Member、PostgreSQL 和系统 Chromium compose E2E 实际运行通过。
- [x] 浏览器选择已确认 workflow、核对名称，并真实点击 UI Preflight 和 Start。
- [x] Member 实际执行 sandbox 命令、MCP `memory_catalog` 和 `submit_flag`；平台 correct verdict、UI solved 题目行和 100 已解题分值可见。
- [x] WP done，3 个 assignment 全部释放，collaboration backlog 为 0。

通过 run：`27971df04cbf48d5912b0218ddad07fb`。证据在 `data/exports/e2e/result.json`、`trace.zip`、`completed.png` 和 `run-events.json`。已修复 Alpine 分组模板导致题目行不显示、空 preflight 引用和旧 favicon 404。fixture 创建/确认通过 API 准备，比赛选择和 Preflight/Start 通过可见 UI 操作。

```bash
docker compose build ipc-app
docker compose -f docker-compose.e2e.yml build e2e-runner
docker compose -f docker-compose.e2e.yml up -d e2e-postgres e2e-platform e2e-app
docker compose -f docker-compose.e2e.yml run --rm e2e-runner
docker compose -f docker-compose.e2e.yml down -v
```

## 真实 20 题监控

`monitor_real_competition.py` 已补齐观测能力；旧 `real_competition_acceptance.py` 委托同一入口，不再默认启动全部题目或主动 stop run。

- 精确选择 20 道唯一、未解出的题目，覆盖 web/misc/reverse/crypto/pwn/ai。支持 `--workflow-id`；多个已确认 workflow 时要求明确选择。
- Start 后只读 API、PostgreSQL 和 project/tool/LLM/memory 日志；不重试任务、不重启 Agent、不调 limits、不修改源码。
- 按 challenge/session/call/submission ID 配对延迟，记录导入到分配、分配到 Member 启动、provider 首响应、每次工具/MCP 调用和 verdict 延迟，输出 p50/p95/max。缺失指标为 null，不伪造零值。
- 记录 MCP 错误率、容器启动/续约/rebuild/stop 调用、remote 使用量及排队、席位分配/释放/残留、helper/成员消息/backlog、最终各状态和类别分布。
- 检查 remote 新分配是否超限、入口是否在首次工具调用前收到、是否存在 5 分钟无事件仍占用席位、终态 assignment/WP/backlog 清理、重复 stop/rebuild 风暴和 WP 重试上限。下调容量时保留已有任务。
- 区分恢复信号与恢复确认：恢复动作后，同一作用域出现成功工具结果才计为已观察到自恢复。真实模型耗时只监控，不设置固定性能失败阈值。
- 默认观察至 workflow deadline 加有限收尾窗口；`--max-duration` 结束观察，不停止仍在运行的比赛。非终态报告不视为完整验收。
- 持续原子写入脱敏 JSON/Markdown，包括 `real_competition_<run_id>.json/.md` 和 `_monitor.json/.md`。重连监控数据库不会操作 Agent。

Docker fixture 监控已得到 27 条日志、19 个 agent event、匹配的启动/MCP/verdict 延迟、3 个已释放席位和零 backlog；报告标明 `model_mode: mock`。该证据用于验证监控器，不计为真实 20 题验收。

当前运行数据库/API 的 workflow 数为 0。模型配置和一条 GZCTF 凭据记录已存在；仍需要指定已确认 workflow 的保存来源，或目标比赛 URL、比赛 ID、队伍 ID。凭据不需要发到聊天中。真实 20 题尚未启动。

```bash
python scripts/monitor_real_competition.py \
  --live-run --workflow-id <confirmed-workflow-id> \
  --max-duration 18000 --database-url "$IPC_DATABASE_URL"
```

## CI 与剩余检查

`.github/workflows/ci.yml` 包含 PostgreSQL tests/coverage、静态和迁移检查、task-image/C5/AI 验收，以及独立 Playwright E2E 和失败诊断上传。

实现已提交到独立分支 `codex/competition-reliability-20261014`，首个 commit `05893ab23421beac43e37727de7b3c1f01276472`。草稿 [PR #4](https://github.com/PureStream108/IPC_CTFAgent/pull/4) 已触发 [CI run 38545171757](https://github.com/PureStream108/IPC_CTFAgent/actions/runs/38545171757)：hosted 浏览器 E2E 通过，但 pytest 捕获同一 flag 重复 complete 与后台 postprocess 的 PostgreSQL 死锁，整体 CI 尚未通过。已统一项目/任务的加锁顺序，并为领取与过期恢复增加跳过已锁项目的并发回归；本机相关 128 项测试通过。修复将更新到同一草稿 PR，要求新 revision 的三个 check 全部通过。状态快照保存于 `data/exports/github-plan-ci-verification.json`，锁回归证据为 `pytest-postprocess-lock-verification.log/.xml`。

- [x] 最新全量 pytest 与覆盖率门禁通过，并保存 81.33%/80.93% 新基线。
- [x] 最终回归后刷新正常栈应用镜像并重验健康状态。
- [x] 完整 `python -m ruff check backend scripts tests` 通过，Windows/Linux 实跑均为零诊断；新增故障回归和完整 pytest 通过，覆盖率未下降。
- [ ] hosted CI 在包含当前改动的 revision 上通过。
- [x] GitHub active ruleset `25067166` 将 `test`、`e2e`、`task-image` 设为 required checks，并回读确认对 main 生效。证据为 `data/exports/github-required-checks-verification.json`、`github-main-rules-verification.json`。
- [ ] 完成真实 20 题运行和日志监控，导出脱敏报告，检查终态席位、WP、消息清零并记录自恢复证据。
- [x] 验证临时 PostgreSQL 自动发现/启动和每例连接池 teardown，测试后活动连接及 schema 残留均为零；异常路径回归通过。

完整 Ruff 已通过，证据为 `data/exports/ruff-plan-verification.json`（空诊断数组）和 `ruff-linux-verification.log`。FastAPI 声明标记复用、回调变量绑定、异常诊断和明确的输入校验错误保持既有接口/恢复语义。已修正 `.gitignore` 的 memory 路径，防止意外忽略 `backend/memory`；迁移入口脚本保留 shebang 并设置 Git executable bit。

CI 已改用完整 Ruff，并在成功或失败时上传 pytest JUnit 和 coverage JSON；本机通过不代表 hosted CI 已通过。

### 待完成工作与完成条件

1. **取得 hosted CI 通过证据。** 在包含本轮改动的同一 revision 上运行 `.github/workflows/ci.yml`，确认 `test`（完整 pytest、覆盖率、临时 PostgreSQL、完整 Ruff/迁移检查）、`e2e`（真实浏览器容器）和 `task-image`（C5/AI）全部通过。保存 commit SHA、CI run 链接和诊断产物；本机结果不能替代 GitHub runner 的结果。采用独立修复分支和草稿 PR 触发验收，完成前不合并。
2. **完成真实 20 题及终态检查。** 前置条件是取得目标平台的已确认 workflow，确认对应比赛、队伍、deadline 和并发限制，且 CI 验证通过。当前数据库/API 没有 workflow，不应根据旧凭据记录中的 workflow ID 猜测比赛。准备完成后，通过显式 `--live-run` 创建新的 run，选择 20 道未解题并覆盖六类；运行期间只观察和记录 bug，不修改代码、重启 Agent、手工重试或调整 limits。观察至比赛终态并完成收尾，核对 remote 上限、首次工具调用前入口可见、静默占席、实例操作/提交/WP 重试、active/WP assignment 和 collaboration backlog 清零。保存 p50/p95/max、类别状态分布、异常与实际自恢复证据，产物为 `data/exports/real_competition_<run_id>.json/.md`。若 `--max-duration` 到期时 run 仍未终结，报告保持未完成并继续只读观察；没有发生故障时如实注明未观察到自恢复。

真实 20 题目前缺少的是已确认 workflow 来源或比赛 URL/比赛 ID/队伍 ID，无需向聊天补发 secret。B7–B9、C 类仍属于后续 backlog，不作为本轮新增待办。

```bash
python -m pytest -q --require-postgres --cov=backend --cov-branch --cov-report=json
python scripts/check_coverage.py
python -m ruff check backend scripts tests
python -m compileall -q backend scripts tests
python -m alembic upgrade head --sql
docker compose config --quiet
docker compose -f docker-compose.e2e.yml config --quiet
bash scripts/c5_task_acceptance.sh
```

真实测试期间只记录 bug 和异常，不进行人工修复；比赛后的修改另行开展。缺少真实 20 题和 hosted required-check 证据时，整体目标保持未完成。
