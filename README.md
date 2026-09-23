<h1 align="center">IPC CTF Agent</h1>

<div align="center">

<img src="frontend/ipc.png" alt="IPC CTF Agent logo" width="160" />

</div>


<div align="center">

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PostgreSQL 16](https://img.shields.io/badge/Runtime-PostgreSQL_16-4169E1.svg?logo=postgresql&logoColor=white)](#durable-runtime-state)
[![Docker Compose](https://img.shields.io/badge/Deploy-Docker_Compose-2496ED.svg?logo=docker&logoColor=white)](#quick-start)

</div>

<div align="center">
[🚀 快速开始](#quick-start) • [✨ 核心设计](#core-innovations) • [🖥️ 控制台](#agent-workbench) • [🏗️ 系统架构](#system-architecture) • [🧰 工具运行时](#tool-runtime) • [🧪 开发](#development)

</div>

## Current Reward

- 第九届西湖论剑·中国杭州网络安全技能大赛 Agent 17th
- 第二届 “湾区杯” 网络安全大赛 Agent 解出 9/10

## 📖 Introduction

IPC CTF Agent 是用于 **CTF** 的多智能体解题系统，包含两条运行路径：面向单题 Project 的 Diamond–Member 解题流程，以及面向平台比赛的 Workflow–CompetitionRun 协调流程。操作者通过 Web UI 或 IPC 行动代理创建项目、确认平台工作流并控制运行；Member 在隔离题目容器中调用工具，把事实、报告和检查点写回共享黑板。

系统将“解出 Flag”与“生成产物”拆开处理：验证后的 Flag 在数据库事务中立即提交为 `solved`，Writeup、Memory 和归档则作为可重试的异步后处理。这样即使生成文档或导出暂时失败，也不会丢失已经确认的解题结果。

IPC 的运行时状态以 PostgreSQL 为唯一事实库；workspace、附件、大型工具输出、实时日志、Writeup 与导出快照使用共享 Artifact 文件树。比赛的 run、题目状态、assignment、session、提交、远端实例、WP job 和事件也持久化在 PostgreSQL 中。每个重要结论都可以回溯到项目事实、报告、日志或产物。

---

## <a id="showcase"></a>🖥️ Showcase

IPC 的 Web 控制台将单题解题和比赛运行集中在同一界面：题目与附件、Member 状态、共享图谱、事实、报告、运行事件、实时日志、Writeup，以及比赛的席位、题目状态和提交状态都可查看。通过 **Config** 面板配置 Diamond、十个全局 Member 席位与可选 IPC 行动代理；未配置密钥的席位会被跳过，不会阻塞界面启动。

控制台提供三类工作面：

- **Project**：创建、启动、停止和恢复单题；查看图谱、事实、Member 和求解状态。
- **Competition**：确认 Workflow，Start/Refresh/Pause/Resume/Stop 比赛，查看十席位、题目同步、实例、提交、WP 和运行事件。
- **Logs / WP / Memory**：查看实时输出，并以 **Derive** 生成只增不覆盖的导出快照。
- **IPC**：持续对话、运行环境诊断、题目沙箱辅助操作，以及经人工确认的平台工作流。

---

## <a id="core-innovations"></a>🚀 Core Innovations

### 1️⃣ **Diamond–Member 协作调度** ⭐⭐⭐

IPC 将面向操作者的控制层与项目内的解题层分离：

- **IPC** 负责持续对话、环境诊断、平台接入和项目生命周期操作；它通过内部 MCP 访问项目，而不代替解题调度。
- **Diamond** 负责解释项目进展、创建差异化 Intent、避免重复方向，并在新报告出现后按需增援。
- **Member** 在题目沙箱中独立探索、执行工具、验证假设，并把事实、报告和进度提交给共享黑板。

系统固定提供 `amber`、`agate`、`topaz`、`sugilite`、`aventurine`、`pearl`、`sapphire`、`jade`、`obsidian`、`opal` 十个全局 Member 席位。单题 Diamond 和比赛 CompetitionService 都只调度已配置可用的席位，并以项目/比赛资源上限控制并发；WP 任务复用原作者 session 对应的席位。

### 2️⃣ **共享黑板与证据化协作** ⭐⭐⭐

项目黑板不依赖模型的隐藏历史。它持久化事实、Intent、图谱链接、Member 状态、报告、Flag 与运行事件，使后续 Agent 和重启后的服务都能继续从同一份项目事实推进。

- Intent 按方向去重；每个项目最多有一个终局 `goal` Intent。
- 报告会回写事实和图谱边，Diamond 据此选择新的探索方向或终止无效分支。
- 大型内容保存在 Artifact 文件树，黑板和模型上下文保留摘要、相对路径、哈希或 artifact ID。
- PostgreSQL 的事务和约束为多实例协调提供基础；比赛 run/平台边界仍需通过服务级 fencing 和对账保证，当前未完成项见下方架构边界。

### 3️⃣ **先确认解题结果，再产出文档** ⭐⭐⭐

单题 Project 找到候选 Flag 后，IPC 会在同一数据库事务内进行预检、写入 Verified Flag、设置 `solved`、记录完成边并投递后处理任务。比赛运行则先把候选交给平台适配器判题，只有 `correct` 才结束同题解题并创建原作者 WP job；两条路径都要求重复提交幂等、错误提交可对账。

- `solved` 是终局状态，Writeup、Memory、Archive 失败不会把项目降级回运行中。
- 后处理使用 PostgreSQL 持久队列和 lease；进程重启或执行器异常后可以安全重试。
- Writeup 先写入同目录临时文件并原子替换；数据库登记或后续步骤失败时会恢复旧文件。
- 项目、Intent 与后处理任务使用 lease/token fencing，过期执行者不能覆盖新执行者的结果。

### 4️⃣ **Workflow–CompetitionRun 比赛协调**

比赛配置以确认过的 `WorkflowProfile` 为入口。每次 Start 创建一个不可变配置快照和 `CompetitionRun`；`CompetitionService` 负责同步平台题目、导入 Project、计算席位、管理远端实例、恢复 assignment/session、提交候选 Flag、轮询异步 verdict，并在正确判题后调度 WP。

- 平台通过统一适配器接入 HTTP JSON、GZCTF 和 ret2shell；适配器声明题目、提交/查询 verdict、实例生命周期和附件能力。
- `CompetitionStore` 将 run、challenge、assignment、session event、submission、instance、WP job 和 run event 写入 PostgreSQL；网络、模型和容器操作在事务外执行。
- 单题协作使用持久 SessionRunner、共享黑板和本地 sandbox；同题 Member 通过 gRPC over UDS 控制通道与 ZeroMQ recon 通道协作，消息先落库再发布。
- 比赛 SSE 从持久 run event 读取，浏览器可用 event id 断线补流；Artifact 文件树保存附件、日志、脚本、截图和 Writeup。

### 5️⃣ PostgreSQL + Artifact ⭐⭐⭐

PostgreSQL 只保存运行时事实、协调状态和可查询元数据；文件系统只保存适合文件存储的工作区与产物。这个边界既让多实例协调有一致的事务语义，也避免把附件和大日志塞进数据库。

| 数据类型 | 存储位置 | 设计目的 |
| --- | --- | --- |
| 项目、黑板、Intent、报告、Memory、会话、租约、Flag、后处理任务 | PostgreSQL | 事务、并发协调、查询与恢复 |
| Workspace、附件、截图、实时日志、Writeup、导出快照 | Artifact 文件树 | 大文件、人工查看与可移植导出 |
| 工具检索缓存 | 有界进程内 TTL/LRU 缓存 | 可随时重建，不作为事实源 |

---

## 🧰 Core Capabilities

### Agent Control

- Diamond 根据项目报告创建、去重和调度 Intent，并按资源上限增援 Member。
- Member 使用结构化动作、事实、报告和 Flag 提交接口；失败按有界退避重试。
- 项目支持 `created`、`running`、`flag_found`、`solved`、`timeout`、`infra_error`、`failed`、`stopped` 状态。
- Flag 提交、goal 完成和后处理任务均有幂等、冲突检查与 lease fencing。
- IPC 可查询状态、暂停、恢复、归档项目，并保留操作历史。

### Competition Control

- Workflow 必须在当前 revision 确认后才能 Start；运行时冻结平台配置、队伍标识、提交规则和 Member 配置快照。
- CompetitionRun 状态为 `preflight`、`importing`、`running`、`paused`、`blocked`、`draining`、`finished` 或 `stopped`。
- 题目状态为 `discovered`、`preparing`、`ready`、`assigned`、`solving`、`solved`、`expired`、`withdrawn` 或 `cancelled`；提交状态独立记录 `queued`、`pending`、`correct`、`wrong`、`unknown` 等结果。
- 每个题目最多分配两个解题席位；WP 任务使用原作者 Member/session，远端实例与本地 sandbox 分开计量。
- `POST /api/workflows/{id}/preflight`、`POST /api/workflows/{id}/start` 和 `/api/runs/{id}/...` 提供平台预检、启动、同步、暂停、恢复、停止与事件查询。

### <a id="tool-runtime"></a>MCP

Member 在题目容器中使用按分类注册的 CTF 工具，并可通过 MCP 访问：

- `memory`：项目经验检索与工具目录。
- `tool_search` / `tools`：跨分类检索或暴露指定题目分类的工具。
- `browser`：基于 Playwright 的浏览器自动化、下载与截图 Artifact；任务镜像只提供 Python 包，浏览器运行时由部署环境按需提供。
- `reverse`：PyGhidra 与 radare2 逆向分析。
- `ret2shell`：ret2shell 平台的动态实例控制（`instance_start` / `instance_status` / `instance_renew` / `instance_stop` / `challenge_status`），仅在配置 `IPC_R2S_USERNAME` 或 `IPC_R2S_TOKEN` 后注册；ws:// 隧道由镜像内置的 wsrx 转发到本地端口。

分类覆盖 `web`、`pwn`、`reverse`、`crypto`、`misc`、`ai`、`osint`。浏览器下载、截图等内容以 Artifact 保存，避免将大型输出直接放入模型上下文。

### Sandbox Isolation

- 每个项目使用一个 Docker 题目容器，并在共享项目 workspace 中保留附件和分析产物。
- 容器工具运行时包含 Web、Pwn、Reverse、Crypto、Misc、AI、OSINT 常用依赖；首次构建会下载 Ghidra、SageMath、PyTorch 等较大组件。Playwright 浏览器二进制不在任务镜像内安装。
- Browser 与 Reverse MCP 运行在任务容器内。
- Docker 路径与网络边界由题目沙箱管理；IPC 行动代理的宿主机能力仅应授予可信操作者。

### <a id="durable-runtime-state"></a>Durable Runtime State

Docker Compose 将数据库放入命名卷，将需要人工访问或跨容器共享的文件保存在 `./data`：

| 路径 | 内容 |
| --- | --- |
| `ipc_postgres_data` Docker 卷 | 项目、黑板、Memory、Ops 会话、租约、Flag 提交和后处理队列 |
| `data/artifacts/projects/` | 共享题目 workspace、附件与截图 |
| `data/artifacts/writeups/` | 实时 Writeup |
| `data/artifacts/logs/` | 项目、LLM、工具与 Memory JSONL |
| `data/artifacts/exports/` | Writeup、日志和 Obsidian/Markdown Memory 导出快照 |
| `data/ops-agent/` | IPC 模型配置和工作流密钥等文件型机密 |
| `ipc_claude_home` Docker 卷 | `claudecode` 运行器的原生 JSONL 会话 |

导出采用无损编号命名；同名文件存在时会创建 `名称01`、`名称02` 等新文件，不覆盖历史导出。

---

## 📋 System Requirements

| 组件 | 要求 | 说明 |
| --- | --- | --- |
| Docker Engine | 必需 | 用于 IPC App、PostgreSQL 和题目工具镜像 |
| Docker Compose v2 | 必需 | 标准部署与服务编排 |
| Docker Socket | 必需 | App 需要创建题目容器；IPC Runner 同样依赖 Socket |
| Linux Docker 主机 | 推荐/已验证 | Compose 会挂载 Docker Socket 与 Compose 插件 |
| LLM 端点 | 开始解题时需要 | Diamond 与已启用 Member 使用 OpenAI、Anthropic、Claude Code、DeepSeek、Pi 或 Mock 适配器 |
| 浏览器 | 可选 | 只保留 Playwright Python 包；浏览器二进制不在任务镜像内，由部署环境显式提供路径 |

> [!WARNING]
> 沙箱降低风险，但不能替代隔离主机或虚拟机。请只将题目、附件、模型密钥和 Docker Socket 放入可信环境。

---

## <a id="quick-start"></a>🚀 Quick Start

### 1. Clone and start

```bash
git clone https://github.com/PureStream108/IPC_CTFAgent.git
cd IPC_CTFAgent
docker compose up -d --build
```

首次构建耗时和磁盘占用会明显增加。服务启动后检查状态：

```bash
docker compose ps
docker compose exec ipc-app ipc check
docker compose logs -f ipc-app
```

打开 <http://localhost:8000> 即可进入 Web UI。可信 Docker 内网中的 Web UI 不要求初始化管理员账号或登录；内部 runner MCP 仍由 `IPC_RUNNER_TOKEN` 保护。

停止服务：

```bash
docker compose down
```

此命令不会删除 `./data`、`ipc_postgres_data` 或 `ipc_claude_home`。只有主动删除它们或执行 `docker compose down -v` 才会清理命名卷。

### 2. Configure the LLM runtime

推荐在 Web UI 的 **Config** 面板配置 Diamond、十个共享 Member 席位和可选 IPC 行动代理。也可以从 [config.example.yml](backend/config/config.example.yml) 复制配置：

```yaml
diamond:
  api_format: openai
  api_surface: auto
  reasoning_effort: auto
  api_key: sk-...
  base_url: https://your-endpoint.example/v1
  model: your-model

members:
  - name: aventurine
    api_format: openai
    api_surface: auto
    reasoning_effort: auto
    api_key: sk-...
    base_url: https://your-endpoint.example/v1
    model: your-model
```

`api_surface: auto` 会自动尝试兼容的 Chat Completions 或 Responses 接口。未配置 API key 的 endpoint 会显示为 skipped；它不会阻塞其他已配置角色运行。

### 3. Start a project

1. 在 Web UI 中选择 **New Project**，填写题目名称、来源、目标和分类，上传附件并添加提示。
2. 点击 **Start**。Diamond 创建首个 Intent 并派发可用 Member。
3. 在项目页面观察事实、报告、图谱、事件和日志；可在非终局状态停止或恢复。
4. 找到 Flag 后，系统先原子验证并提交 `solved`，再异步生成 Writeup、Memory 和归档。
5. 在 Logs、WP、Memory 中选择 **Derive**，生成只增不覆盖的导出快照。

常用本地命令：

```bash
ipc check                    # 检查服务与解题配置
ipc health                   # 检查 Diamond 和 Member 模型端点
ipc serve --port 8000        # 直接启动 API 与 Web UI
```



---

## <a id="agent-workbench"></a>🖥️ Agent Workbench

### Web workbench

Web UI 是项目管理与可观测性界面。它展示项目状态、协作图、事实、Intent、报告、成员活动、日志、Writeup 与 Memory；同时提供新建项目、启动、停止、恢复、导出和配置入口。

UI 运行在操作者可信的 Docker 网络边界内，默认不增加浏览器登录门槛。不要将其映射到不受信任网络；如果部署环境需要公网或多租户访问，应在反向代理、网络策略和身份认证层补充访问控制。

### IPC action agent

IPC 是面向操作者的持续对话代理，可诊断运行环境、辅助题目沙箱、操作项目生命周期，并生成声明式平台工作流。工作流必须经人工确认才能执行。其对话、运行事件和工作流元数据存入 PostgreSQL，文件型密钥保存在 `data/ops-agent/`。

`claudecode` 运行器会持久化原生会话，以便在容器替换后通过 `--resume` 延续上下文。无论使用哪种 Runner，内部 MCP 请求都应通过 `IPC_RUNNER_TOKEN` 鉴权。

---

## <a id="system-architecture"></a>🏗️ System Architecture

### 两条运行路径

IPC 是面向操作者的控制层，负责持续对话、环境诊断、Workflow 管理和项目生命周期；它通过内部 MCP 调用后端服务。单题 Project 由 Diamond 负责意图分解和 Member 调度；比赛 Workflow 由 CompetitionService 负责平台同步、席位分配、提交判题和 WP 生命周期。两条路径共享 PostgreSQL、Artifact 和 Member/tool runtime，但不共享各自的调度状态机。

```mermaid
flowchart TB
    USER["操作者"] --> UI["Web UI / FastAPI"]
    UI --> STATE["AppState / 项目状态"]

    subgraph OPS["IPC 行动代理层"]
        IPC["IPC 对话代理"]
        RUNNER["Claude Code Runner\n或 OpenAI-compatible API"]
        IMCP["内部 IPC MCP\n项目生命周期与诊断工具"]
        IPC <--> RUNNER
        RUNNER --> IMCP
    end

    subgraph SOLVER["CTF 多智能体解题层（每个项目）"]
        DIAMOND["Diamond\n调度、意图与增援决策"]
        M1["Member A"]
        M2["Member B"]
        MN["Member N"]
        BOARD["共享黑板\n事实、意图、报告、图谱"]
        SANDBOX["Docker 题目沙箱\n共享工作区与 CTF 工具"]

        DIAMOND -->|"分配差异化意图"| M1
        DIAMOND -->|"分配差异化意图"| M2
        DIAMOND -->|"按需增援"| MN
        M1 -->|"读取 / 写入协作状态"| BOARD
        M2 -->|"读取 / 写入协作状态"| BOARD
        MN -->|"读取 / 写入协作状态"| BOARD
        BOARD -->|"报告与检查点"| DIAMOND
        M1 -->|"工具调用"| SANDBOX
        M2 -->|"工具调用"| SANDBOX
        MN -->|"工具调用"| SANDBOX
    end

    IMCP -->|"创建 / 启动 / 状态 / 停止"| STATE
    STATE --> DIAMOND
    STATE --> MEMORY["Memory、工具目录与导出"]
    SANDBOX --> CMCP["Browser / Reverse MCP"]
```

### 比赛运行时

```mermaid
flowchart TB
    OP["操作者 / Web UI"] --> API["FastAPI"]
    API --> WF["WorkflowProfile\n确认后的平台配置"]
    WF --> CS["CompetitionService\nrun lease + tick"]
    CS --> ADAPTER["CompetitionPlatform\nHTTP JSON / GZCTF / ret2shell"]
    ADAPTER <--> PLATFORM["外部比赛平台"]
    CS --> STORE["CompetitionStore"]
    STORE --> DB[("PostgreSQL")]
    CS --> IMPORT["题目导入 / Project 绑定"]
    CS --> POLICY["十席位调度\n远端实例容量"]
    POLICY --> ASSIGN["Assignment + lease epoch"]
    ASSIGN --> SESSION["持久 AgentSession\nSessionRunner"]
    SESSION --> MEMBER["Member runtime"]
    MEMBER --> SANDBOX["题目 sandbox / SharedWorkspace"]
    SESSION --> RECON["gRPC UDS + ZeroMQ\ndurable replay"]
    CS --> SUB["候选 Flag\nsubmission / verdict 对账"]
    SUB --> ADAPTER
    CS --> WP["原作者 session WP job"]
    STORE --> EVENTS["Run events / SSE after"]
    EVENTS --> OP
```

一次比赛运行的持久化边界如下：

- `WorkflowProfile` 保存平台映射、队伍/比赛标识、提交判定规则、附件限制和能力声明；Start 时写入 `CompetitionRun.config_snapshot`。
- `CompetitionRun` 保存状态、revision、同步时间、run lease 和错误；`competition_run_challenges` 将本次运行与平台题目关联。
- `CompetitionService` 在 tick 中完成同步、deadline、assignment 回收、实例续期、提交查询、WP dispatch 和观测；外部调用不放在数据库事务内。
- `CompetitionStore` 是 competition schema 的唯一写入口；`SessionRunner` 和 transport 层通过 assignment id、owner、epoch 校验写入权限。
- 当前实现仍有未完成的 run 隔离、平台队伍作用域、外部实例配额和 Flag 脱敏问题，详见 [`plan-progress.md`](plan-progress.md) 的 P1/P2 复审清单。

### 一次解题任务的工作流

```mermaid
flowchart LR
    A["创建题目\nWeb UI 或 IPC"] --> B["建立 Project\n状态 created"]
    B --> C["启动调度器\n状态 running"]
    C --> D["Diamond 创建首个 Intent\n并派发 Member"]
    D --> E["Member 在题目沙箱中\n分析、验证、执行工具"]
    E --> F["写入事实、报告、进度\n到共享黑板"]
    F --> G{"发现 Flag\n或新的难点？"}
    G -->|"新的难点"| H["Diamond 基于报告\n创建去重方向并增援"]
    H --> E
    G -->|"继续推进"| I["Diamond 创建下一 Intent"]
    I --> E
    G -->|"发现候选 Flag"| J["状态 flag_found"]
    J --> K["原子验证并持久化 Flag\n状态 solved"]
    K --> L["异步生成 Writeup、Memory、归档\n失败可重试且不降级 solved"]

    IPC["IPC 代理"] -. "可随时查询状态、记录活动、\n暂停 / 恢复或完成归档" .-> C
    IPC -. "运行环境诊断、\n题目沙箱辅助操作" .-> E
```

### Runtime invariants

- PostgreSQL 是唯一的运行时事实库；Artifact 文件树不是并发协调的真相源。
- 单题 Project 的 Verified Flag 与 `solved` 在同一事务中提交；比赛候选只有平台 `correct` verdict 才能生成本地 Verified Flag，平台明确报告的 external solved 则单独记录，不伪造本地 Flag/WP。
- 每个项目只有一个终局 `goal` Intent；历史重复记录会在迁移中归并并保存审计快照。
- 过期的项目、Intent 或后处理 lease 持有者不能写入新一代执行结果。
- Writeup、Memory、Archive 后处理可失败、可重试，但不撤销已提交的 `solved`。

### 当前架构边界

README 描述的是当前代码结构，不等同于全部计划验收已完成。比赛运行仍需补齐同一平台题目的 run 隔离、GZCTF 队伍作用域、完整 engine epoch fencing、远端实例配额/停止失败补偿、统一 Flag 脱敏和 Refresh/Stop 竞态处理；这些问题及复验条件记录在 [`plan-progress.md`](plan-progress.md) 中。

### Repository layout

```text
IPC_CTFAgent/
├── backend/
│   ├── api/          # FastAPI 路由
│   ├── auth/         # 认证兼容层与会话存储
│   ├── blackboard/   # 共享黑板、图谱和事务操作
│   ├── core/         # 调度、生命周期、后处理和配置
│   ├── mcp/          # MCP 客户端、服务端与逆向 Worker
│   ├── members/      # Member 与模型适配器
│   ├── memory/       # 经验记忆与工具目录
│   ├── ops/          # IPC 行动代理与平台工作流
│   ├── competition/  # Workflow/Run、调度、session、传输与比赛存储
│   ├── persistence/  # PostgreSQL schema 与 Alembic migrations
│   ├── platform/     # 平台适配层（HTTP JSON / GZCTF / ret2shell 客户端）
│   ├── sandbox/      # Docker/本地任务沙箱
│   └── tools/        # 工具注册表、目录与文档
├── frontend/         # 单页 Web UI
├── docker/           # 任务镜像和 IPC Runner 镜像
├── runner/           # IPC 运行器与宿主机执行辅助工具
├── scripts/          # 旧数据迁移、文档生成和验收脚本
└── tests/            # pytest 测试套件
```

---

## <a id="roadmap"></a>🗓️ Roadmap

- [x] Diamond–Member 共享黑板协作与题目容器运行时
- [x] PostgreSQL 运行时事实库与 Artifact 文件存储边界
- [x] 单题 Flag 原子提交与幂等后处理队列
- [x] SQLite 历史数据只读迁移与审计归并
- [ ] 比赛题目/提交/WP 的 run 隔离与完整 epoch fencing
- [ ] 平台队伍身份、外部实例配额、停止失败补偿和 Flag 脱敏闭环
- [ ] 基于固定题集的持续稳定性回归与故障注入基准
- [ ] 更细粒度的操作者访问控制与部署安全配置示例

---

## <a id="development"></a>🧪 Development

本地开发要求 Python 3.10+；CI 使用 Python 3.11。安装依赖并连接一个隔离的 PostgreSQL 实例：

```bash
python -m pip install -e ".[dev,docker]"
export IPC_TEST_DATABASE_URL=postgresql://ipc:ipc@127.0.0.1:5432/ipc
python -m pytest -q
```

常用校验：

```bash
python -m ruff check backend scripts tests
python -m compileall -q backend scripts tests
python -m alembic upgrade head --sql
docker compose config --quiet
```

工具目录文档由清单生成：

```bash
python scripts/generate_catalog_docs.py
```

构建 `ipc-task:latest` 后，可执行任务镜像验收：

```bash
docker run --rm \
  -v "$PWD/scripts:/acceptance:ro" \
  ipc-task:latest \
  bash /acceptance/c5_task_acceptance.sh
```

---

## 👥 Contributors

<p>
  <a href="https://github.com/PureStream108">
    <img src="https://github.com/PureStream108.png?size=80" width="80" height="80" style="border-radius: 50%;" alt="PureStream108" title="PureStream108" />
  </a>
  <a href="https://github.com/ecxwxz">
    <img src="https://github.com/ecxwxz.png?size=80" width="80" height="80" style="border-radius: 50%;" alt="xz w" title="xz w" />
  </a>
  <a href="https://github.com/springbot2025">
    <img src="https://github.com/springbot2025.png?size=80" width="80" height="80" style="border-radius: 50%;" alt="springbot" title="springbot" />
  </a>
</p>


## 🤝 Contribution

欢迎提交 bug 报告、稳定性测试、工具集成、文档和架构改进。

1. 先在 [Issues](https://github.com/PureStream108/IPC_CTFAgent/issues) 描述问题或设计提案。
2. 创建聚焦的分支，并为修改的运行时边界补充测试。
3. 在 Pull Request 中说明行为变化、迁移影响和验证结果。

---

## 📝 License

本项目采用 [GNU Affero General Public License v3.0](LICENSE)。
