# CTF 模式（统一为比赛模式）

CTF 只有**一条执行路径**：`CompetitionRun`。远程比赛通过 Workflow 产生 run，本地单题在第一次 Start 时被包装成
**one-challenge local run**，两者共用同一套调度、席位、提交账本和停止/恢复语义。Project 仍然是每道题的工作区与
只读历史视图，不是第二套运行模式。

~~~
远程 Workflow 或本地草稿 Project
  → CompetitionRun（远程 run / 本地 one-challenge run）
  → competition_challenges
  → assignment / session / instance / submission
  → CompetitionService 调度
  → Project 展示图谱、事实、附件、Member、WP 和历史
~~~

| | 本地单题（one-challenge run） | 远程比赛 run |
| --- | --- | --- |
| 创建方式 | `POST /projects/{id}/start` 自动 adopt | Workflow 确认后 Preflight → Start |
| 调度者 | CompetitionService（同一循环） | CompetitionService |
| 题目来源 | 已存在的 Project | 平台同步 |
| 判题 | 本地证据（goal 边 + `verify_flag`/accept_verified_flag） | 平台 verdict |
| 实例 | 无（`remote=false`，不占平台配额） | 按平台能力 |
| 结束 | 解出 / 手动停止 run | 解出全部 / 窗口结束 / 手动停止 |

**ownership 投影**（`GET /projects`、`GET /projects/{id}`、日志目录共用同一语义）：

| ownership | 判定 | 控制入口 |
| --- | --- | --- |
| `competition` | `competition_challenges.project_id` 命中（含已结束的 run） | `/api/runs/{run_id}/{pause,resume,stop,refresh}` |
| `legacy_import` | 仅 `workflow_challenges.project_id` 命中 | 首次 Start 时 adopt 成本地 run |
| `standalone` | 两者都未命中 | `POST /projects/{id}/start` 建本地 run |

旧的项目级入口保留为兼容适配器，不会产生第二条调度循环：

- `/projects/{id}/start|resume`：非 run-owned 时创建/复活本地 run；run-owned 且 run 活跃时返回 **409**，
  `detail` 带 `run_id`、`challenge_id` 与 `control`。已停止的本地 run 会被 `resume_local` 复活（保留 run id 与历史）。
- `/projects/{id}/pause|stop`：run-owned 一律 **409** 指向 run 控制，绝不只改 `projects.status`。
- `/projects/stop-all`：先停所有活动 run，再停独立 Pentest 项目。
- `DELETE /projects/{id}`：run-owned 返回 **409**（保留比赛历史）；legacy_import 先清理 `workflow_challenges` 关联再删。
- 前端删除 `Import all` / `Submit all found flags` 旁路；控制面收敛到 **Runs** 弹窗（列表、详情、pause/resume/refresh/stop，带 revision）。

IPC 工具与 Ops 执行入口同样遵守 run 归属：

- `ipc_start_challenge` / `ipc_start_solver`：standalone 项目走原 orchestrator 兼容路径；run-owned 项目不再直接启动，
  改由 `resume_local` 复活本地 run，远程 run 返回带 `run_id`/`control` 的错误对象。
- `ipc_stop_challenge`：run-owned 直接返回 `control: /api/runs/{run_id}/stop`，不再只改 `projects.status`。
- `ipc_finalize_challenge`：run-owned 的 flag 经 `submit_flag_candidate` 进入 `competition_submissions` 台账，由
  `LocalPlatform.submit` 用 goal 边 + `accept_verified_flag` 判题；standalone 保持立即 verified。
- `POST /api/ops/workflows/{id}/execute`：`import` 在该 workflow 有活动 run 时拒绝；`submit` 对 run-owned 项目拒绝，
  指向 `/api/challenges/{challenge_id}/candidates`；`preview` 保持只读可用。
- `source='local'` 的内部 run workflow 不出现在 `/api/ops/workflows` 列表里。

---

## 一、一次 CTF 解题的流程

~~~
建项目（题目 + 附件 / 目标地址）
   → Diamond 读题，拆成 Intent（分析 / 利用 / 验证…）
   → Member 领 Intent，在隔离沙箱里调工具
   → 事实、报告、检查点写回共享黑板（其他 Member 可见）
   → 验证到 flag → 提交，事务内立刻置 solved
   → 后处理（Writeup / Memory / 归档）异步重试，不影响已确认的结果
~~~

项目状态：`created`、`running`、`flag_found`、`solved`、`timeout`、`infra_error`、`failed`、`stopped`。

**关键设计**：解出 flag 与生成文档是两条路。Writeup 或导出失败不会回滚已确认的解题结果。

---

## 二、配置

沿用「本地任务镜像模式」的配置（见 [运行模式与配置](tsecbench-modes.md)），CTF 模式额外看这几项：

| 项 | 作用 | 建议 |
| --- | --- | --- |
| `members[]` | Member 席位与模型；没配密钥的席位自动跳过 | 至少配 3 个，Diamond 才有增援余地 |
| `runtime.sandbox_backend` | `docker`（每题一容器）或 `local` | 有 Docker 主机就用 `docker` |
| `runtime.max_member_steps` | 单个 Member 的动作上限 | 默认 60，长题可提到 120 |
| `runtime.provider_*_timeout` | 模型调用的读/轮/进度超时 | 默认 120/300/120 秒，别用 SDK 默认的 300/900 |
| `IPC_WP_JOBS` | `0` 关闭 Writeup 席位 | 只求 flag 时关掉，省时间 |
| 分类映射 | 决定 Member 拿到哪套工具（web/pwn/reverse/crypto/misc/ai/osint） | 题目分类填错会拿不到对应 CLI |

---

## 三、Member 能用的东西

- **工具**：按分类注册的 CTF CLI（nmap/ffuf/sqlmap、radare2/gdb、pwntools、z3、capstone/unicorn…），托管镜像还带 php 与 qemu-user。
- **MCP**：`memory`（项目经验检索）、`tool_search` / `tools`（跨分类查工具）、`ret2shell`（配了 token 才注册）。
- **技能**：镜像内置的上游技能包（web/pwn/reverse/crypto/misc/forensics/malware/osint + writeup）会按题目分类提示路径，Member 用 `cat` 读。
- **黑板**：事实（fact）、报告、检查点；同一项目的所有 Member 共享，换人后新席位能读到前人的结论。
- **后台作业**：`job_start` 起长命令，`job_wait` 等结果，`job_logs` 读输出；作业结束会有摘要回到上下文。

---

## 四、怎么跑

Web 控制台的 **Project** 页：新建项目 → 填题目描述、附件或目标 → 启动。

API 等价操作：

~~~bash
# 建项目
curl -X POST -H "Content-Type: application/json" -d @project.json http://127.0.0.1:8000/api/projects
# 启动 / 停止 / 恢复
curl -X POST http://127.0.0.1:8000/api/projects/<id>/start
curl -X POST http://127.0.0.1:8000/api/projects/<id>/stop
# 查看状态与事实
curl http://127.0.0.1:8000/api/projects/<id>
~~~

---

## 五、排错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| Member 连续两次无效动作后停住 | 模型给了空参数或非法工具名，运行时按契约打断 | 看「member_error / invalid_action」日志，换模型或降低推理等级 |
| 沙箱起不来 | Docker socket 不可用或镜像缺失 | `docker images` 确认 `ipc-task:latest`，检查 Compose 的 socket 挂载 |
| flag 提交被判 wrong | 格式不符或题目有多个 flag | 先确认题目要求的格式；多 flag 题看 `correct_flag_count/total_flag_count` |
| 长命令被 60 秒掐断 | 前台 `bash` 有超时 | 改用 `job_start` + `job_wait` |
| 上下文被压缩后继续打转 | 同一思路反复重试 | 一次尝试内压缩到上限会换人；也可手动停止后重新分配 |

---

## 六、代码位置

| 位置 | 内容 |
| --- | --- |
| `backend/core/diamond.py` | 意图分解、增援、报告 |
| `backend/members/base_member.py` | Member 主循环、上下文构建、工具调度 |
| `backend/agent/runtime.py` | 会话运行时、压缩、事件 |
| `backend/sandbox/` | 题目容器与本地沙箱 |
| `backend/skills/store.py` | 技能包安装与按分类分发 |
| `backend/tools/` | 工具注册表、按分类暴露 |
