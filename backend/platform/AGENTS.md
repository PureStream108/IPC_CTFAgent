# AGENTS.md — backend/platform 平台适配约束

本目录是**唯一**允许写入平台个性化逻辑的地方。任何平台（gzctf / tsecbench / ret2shell /
ichunqiu，以及以后新增的平台）的适配都必须遵守下面的硬性规则。

## 1. 允许的改动

- 新建 `backend/platform/<platform>/`：`__init__.py`、`adapter.py`、`workflow.py`、
  `browser_setup.py` 以及该平台需要的 client / 工具模块。平台的一切个性化（URL、签名、
  登录、题型、容器语义、字段名、分类别名）都写在这里。
- 在共享注册处各加**一行**：
  - `backend/platform/presets.py`：`from backend.platform import <platform>`，并把
    `<platform>` 加进 `_MODULES`（以及该 preset 需要导出的 input 类型）。
  - `backend/platform/factory.py`：一个 `if mapping.platform == "<platform>":` 分支，
    返回本平台的 adapter。
- 新增 `tests/test_<platform>.py`；共享测试里只允许把平台名加进"已注册平台集合"。

## 2. 禁止的改动

- 不许把平台判断、平台 URL、签名算法、字段名、分类映射写进 `backend/platform/adapter.py`、
  `backend/platform/mapping.py`、`backend/ops/*`、`backend/competition/*` 等共享模块。
- 不许修改其他平台的目录、测试或既有行为。
- 不许让共享代码 import 平台私有实现（注册与 factory 分支除外）。
- 不许在平台包里靠环境变量夹带默认值：凭据一律走 workflow secret store。

## 3. 确实需要"通用能力"时

跨平台能力（例如 `attachments_only`、`exclude` 这类开关）只允许做成**增量、默认关闭、
平台无关**的字段：默认值必须让所有既有平台行为完全不变，语义实现放在各自平台包里，
共享侧只做字段透传与校验。动手前先说明为什么必须动共享模型，并在回复里列出被改的共享
文件与理由。

## 4. 提交前自检

- [ ] 新增逻辑是否全部落在 `backend/platform/<platform>/` 内？
- [ ] 共享文件是否只有注册行或透传字段的增量改动？
- [ ] 其他平台测试是否仍然全绿（`python -m pytest tests -q`）？
- [ ] 是否有平台名/平台凭据硬编码泄漏到共享分支或日志？

## 5. 运行期边界

- 竞赛 Member 容器只挂 project workspace 卷（`/workspace`），看不到也改不了本目录；
  镜像里的 backend 是构建时 COPY 进去的。
- 只有 IPC 的 Ops 会话（host_exec）能改宿主机源码；改完必须
  `docker compose build ipc-app ipc-task-image && docker compose up -d --no-build` 才生效。
