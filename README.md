<h1 align="center">IPC CTF Agent</h1>

<img src="frontend/ipc.png" alt="IPC CTF Agent logo" width="160" />

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

## 📖 Introduction

IPC CTF Agent 是用于**合法授权的 CTF、靶场、教学与安全研究**的多智能体解题系统。操作者通过 Web UI 或 IPC 行动代理创建项目；Diamond 按题目进展分解意图、派发与增援 Member；Member 在隔离题目容器中调用工具并把事实、报告和检查点写回共享黑板。

系统将“解出 Flag”与“生成产物”拆开处理：验证后的 Flag 在数据库事务中立即提交为 `solved`，Writeup、Memory 和归档则作为可重试的异步后处理。这样即使生成文档或导出暂时失败，也不会丢失已经确认的解题结果。

IPC 的运行时状态以 PostgreSQL 为唯一事实库；workspace、附件、大型工具输出、实时日志、Writeup 与导出快照使用共享 Artifact 文件树。每个重要结论都可以回溯到项目事实、报告、日志或产物。

> [!WARNING]
> 本项目会管理 Docker 容器，并可在启用 IPC 行动代理时通过 Docker Socket 执行宿主机级操作。请仅部署在可信、隔离且获得明确授权的环境中；绝不能直接暴露到公网。

---

## <a id="showcase"></a>🖥️ Showcase

IPC 的 Web 控制台将一个项目的解题过程集中在同一视图中：题目与附件、Member 状态、共享图谱、事实、报告、事件、实时日志和 Writeup 都可直接查看。通过 **Config** 面板配置 Diamond、四个内置 Member 与可选 IPC 行动代理；未配置密钥的端点会被跳过，不会阻塞界面启动。

控制台提供三类工作面：

- **Project**：创建、启动、停止和恢复题目；查看图谱、事实、Member 和求解状态。
- **Logs / WP / Memory**：查看实时输出，并以 **Derive** 生成只增不覆盖的导出快照。
- **IPC**：持续对话、运行环境诊断、题目沙箱辅助操作，以及经人工确认的平台工作流。

---

## <a id="core-innovations"></a>🚀 Core Innovations

### 1️⃣ **Diamond–Member 协作调度** ⭐⭐⭐

IPC 将面向操作者的控制层与项目内的解题层分离：

- **IPC** 负责持续对话、环境诊断、平台接入和项目生命周期操作；它通过内部 MCP 访问项目，而不代替解题调度。
- **Diamond** 负责解释项目进展、创建差异化 Intent、避免重复方向，并在新报告出现后按需增援。
- **Member** 在题目沙箱中独立探索、执行工具、验证假设，并把事实、报告和进度提交给共享黑板。

系统固定提供 `aventurine`、`pearl`、`jade`、`topaz` 四个 Member 配置槽。Diamond 只调度已配置可用的 Member，并以项目资源上限控制并发。

### 2️⃣ **共享黑板与证据化协作** ⭐⭐⭐

项目黑板不依赖模型的隐藏历史。它持久化事实、Intent、图谱链接、Member 状态、报告、Flag 与运行事件，使后续 Agent 和重启后的服务都能继续从同一份项目事实推进。

- Intent 按方向去重；每个项目最多有一个终局 `goal` Intent。
- 报告会回写事实和图谱边，Diamond 据此选择新的探索方向或终止无效分支。
- 大型内容保存在 Artifact 文件树，黑板和模型上下文保留摘要、相对路径、哈希或 artifact ID。
- PostgreSQL 的事务和约束确保多实例同时操作时不会产生相互矛盾的终局状态。

### 3️⃣ **先确认解题结果，再产出文档** ⭐⭐⭐

找到候选 Flag 后，IPC 会在同一数据库事务内进行预检、写入 Verified Flag、设置 `solved`、记录完成边并投递后处理任务。重复提交相同 Flag 是幂等的；提交不同 Flag 会明确报告冲突。

- `solved` 是终局状态，Writeup、Memory、Archive 失败不会把项目降级回运行中。
- 后处理使用 PostgreSQL 持久队列和 lease；进程重启或执行器异常后可以安全重试。
- Writeup 先写入同目录临时文件并原子替换；数据库登记或后续步骤失败时会恢复旧文件。
- 项目、Intent 与后处理任务使用 lease/token fencing，过期执行者不能覆盖新执行者的结果。

### 4️⃣ PostgreSQL + Artifact ⭐⭐⭐

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

### <a id="tool-runtime"></a>MCP

Member 在题目容器中使用按分类注册的 CTF 工具，并可通过 MCP 访问：

- `memory`：项目经验检索与工具目录。
- `tool_search` / `tools`：跨分类检索或暴露指定题目分类的工具。
- `browser`：基于 Chromium 的浏览器自动化、下载与截图 Artifact。
- `reverse`：PyGhidra 与 radare2 逆向分析。
- `zap`：可选 OWASP ZAP 服务，必须同时开启运行配置和 Compose profile。
- `ret2shell`：ret2shell 平台的动态实例控制（`instance_start` / `instance_status` / `instance_renew` / `instance_stop` / `challenge_status`），仅在配置 `IPC_R2S_USERNAME` 或 `IPC_R2S_TOKEN` 后注册；ws:// 隧道由镜像内置的 wsrx 转发到本地端口。

分类覆盖 `web`、`pwn`、`reverse`、`crypto`、`misc`、`ai`、`osint`。浏览器下载、截图等内容以 Artifact 保存，避免将大型输出直接放入模型上下文。

### Sandbox Isolation

- 每个项目使用一个 Docker 题目容器，并在共享项目 workspace 中保留附件和分析产物。
- 容器工具运行时包含 Web、Pwn、Reverse、Crypto、Misc、AI、OSINT 常用依赖；首次构建会下载 Ghidra、Chromium、SageMath、PyTorch 等较大组件。
- Browser 与 Reverse MCP 运行在任务容器内；ZAP 是可选的共享服务，不会在默认部署中启用。
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
| Docker Engine | 必需 | 用于 IPC App、PostgreSQL、题目工具镜像和可选服务 |
| Docker Compose v2 | 必需 | 标准部署与服务编排 |
| Docker Socket | 必需 | App 需要创建题目容器；IPC Runner 同样依赖 Socket |
| Linux Docker 主机 | 推荐/已验证 | Compose 会挂载 Docker Socket 与 Compose 插件 |
| LLM 端点 | 开始解题时需要 | Diamond 与已启用 Member 使用 OpenAI、Anthropic、Claude Code、DeepSeek、Pi 或 Mock 适配器 |
| 浏览器 | 可选 | 用于打开本地浏览器 |

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

推荐在 Web UI 的 **Config** 面板配置 Diamond、四个内置 Member 和可选 IPC 行动代理。也可以从 [config.example.yml](backend/config/config.example.yml) 复制配置：

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

### 4. Enable optional ZAP

先在 Web UI 运行配置中启用 **Optional OWASP ZAP**，再启动 Compose profile：

```bash
docker compose --profile zap up -d
```

两个条件必须同时满足，ZAP 才会注入到 Member 可用的 MCP 工具中。

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

### IPC、Diamond 与 Member 的关系

IPC 和 Diamond 是两个协作层：IPC 负责面向操作者的持续对话、环境诊断与平台工作流；Diamond 负责一个 CTF 项目内的任务分派和 Member 调度。IPC 通过内部 MCP 创建、启动、观察或停止项目，而不会直接替代 Diamond 的调度职责。

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
    STATE -. "可选" .-> ZAP["OWASP ZAP"]
```

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
- 已验证 Flag 与 `solved` 在同一事务中提交；`solved` 项目必须有 Verified Flag。
- 每个项目只有一个终局 `goal` Intent；历史重复记录会在迁移中归并并保存审计快照。
- 过期的项目、Intent 或后处理 lease 持有者不能写入新一代执行结果。
- Writeup、Memory、Archive 后处理可失败、可重试，但不撤销已提交的 `solved`。

### Legacy data migration

旧版本的 SQLite graph/memory/Ops 数据库、JSONL 日志、Markdown Memory 和 workspace 可以离线导入 PostgreSQL。SQLite 仅作为迁移器的只读输入，不是运行时依赖；每次导入都会在 `migration_runs` 记录源清单、导入数量、冲突数量和错误。

迁移器会先升级到基础 PostgreSQL schema，导入历史数据，再升级到最新 revision。若旧库包含同一项目的多个 `goal` Intent，最终升级会保留最早记录作为 canonical Intent，归并来源、图谱链接和报告标签，并在 `audit_events` 保存被移除记录的完整快照。导入期间请停止 IPC 应用，避免与在线写入竞争。

```bash
python scripts/migrate_legacy_data.py \
  --legacy-root /path/to/old/IPC_CTFAgent \
  --artifact-root ./data/artifacts \
  --database-url "$IPC_DATABASE_URL"
```

先使用 `--dry-run` 检查源清单。旧 `completed` 项目会导入为 `flag_found`，需要通过当前 Verified Flag 流程重新确认；没有 Flag 的项目不会被标记为已解决。

直接在主机运行服务时，请先升级数据库。Docker 镜像会在启动时自动完成这一步：

```bash
alembic upgrade head
ipc serve
```

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
- [x] Flag 原子提交、lease fencing、幂等后处理队列
- [x] SQLite 历史数据只读迁移与审计归并
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
    <img src="https://github.com/springbot2025.png?size=80" width="80" height="80" style="border-radius: 50%;" alt="xz w" title="xz w" />
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
