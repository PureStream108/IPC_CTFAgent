# IPC_CTFAgent

~~Interastral_Peace_Corporation~~ CTF Agent
Based on https://github.com/oritera/Cairn & https://github.com/verialabs/ctf-agent

## Highlights

- 基于 Cairn 信息共享机制与 CTF-Agent 智能体动态分配机制，新增动态难度反馈机制，优化单、多 Agent 之间的协同通信，实现交互流程流畅化、交互信息可视化清晰化

- 增加了通用性，并不局限于 CTFD 平台的 CTF 挑战
- 内置 Chrome/Ghidra/ZAP，解决 “LLM 缺少真实浏览器环境验证/借助objdump还原不出完整逻辑” 的问题
- 初始将多种常用工具包装成 MCP 内置进容器，支持运行中动态安装新工具
- 支持运行过程中动态输出日志（思考/工具调用/协同过程），支持针对日志对 Agent 进行优化
- 支持 Memory 功能，包含Exp、涉及知识、试错点、工具调用

## Results

见[Example](./docs/Example.md)

测试中ing

## Limitations

- [ ] 目前四个 Member 同时运行占用内存过大
- [ ] 目前难度反馈机制尚有不足

## Quick Start

将 IPC_CTFAgent\backend\config\config.example.yml 填写好后替换为 config.yaml

```python
docker compose up -d
```

## About

```python
CREATED
    ↓

RUNNING
    ↓

FLAG_FOUND（Goal Completed）
    ↓

WP_WRITING
    ↓

MEMORY_WRITING
    ↓

COMPLETED
```

## 关于Wp和日志

当 Member 解出 FLAG 的时候，Diamond 负责沿着已确定的解题步骤撰写 Wp 和 Exp

文件为 markdown 文件，流程为：

题目信息 --> 解题过程 --> Exp。运行时 Wp 默认保存在容器内 `/app/wp`。

日志会记录 Diamond 和 Member 间的调用/反馈记录，工具调用记录，Member思考过程

主界面 WP -> Derive 会把已完成项目的 Wp 导出到 Docker 启动目录下的 `/Wp` 文件夹；主界面 Logs -> Derive 会把项目日志导出到 Docker 启动目录下的 `/logs` 文件夹

删除并重建容器会清空容器内运行记录
