# ret2shell 分支改造计划

目标：从当前工作区切出 `ret2shell` 分支，彻底移除 OpenHarmony 内容，适配西电 CTF 平台（ctf.xidian.edu.cn / game 37，ret2shell 开源平台，Rust+axum 后端）。

## 0. 已确认的关键平台契约（来自 ret2shell 源码调研）

- 认证：`POST /api/account/login`，token 在**响应头 `Set-Token`**（非 body），后续 `Authorization: Bearer`；响应中出现 `Set-Token` 时需滚动更新；也支持 HTTP Basic 兜底；密码错 5 次锁号 30 分钟
- 题目：`GET /api/game/{gid}/challenge` → `[items, total]` 二元组；分类在 `tag[0].name`；动态计分直接读 `score`
- 附件：`GET .../challenge/{cid}/file` → `[{folder, file}]`，下载带 query 参数
- **flag 提交是异步判题**：`POST .../submit` body `{"content": "flag{...}"}` → 返回 `solved:null` 的 Submission，需轮询 `GET .../submit?id=` 直到 `solved != null`；**5 分钟 10 次提交限额**（429）
- 靶机实例：`POST/PATCH/DELETE .../instance` 启动/续期/销毁，`GET /api/game/{gid}/instance` 轮询拿 `exposed_ports`（玩家实际连接地址）
- 错误响应是**纯文本** body + 状态码，不是 JSON

## 1. Git 操作

- 从当前工作区状态创建并切换到 `ret2shell` 分支（保留近期对通用模块的非 OpenHarmony 改进，如 orchestrator/adapters 的兼容性修复）
- 最终一次性提交

## 2. 删除 OpenHarmony（含数据目录，按你确认全部物理删除）

**整删文件：**
- `backend/openharmony/`（policy/cwe/git_evidence/models/workflow，6 文件）
- `backend/platform/openharmony.py`、`backend/platform/openharmonyctf_skill.py`
- `scripts/`：openharmony_setup / openharmony_source_index / openharmony_candidate_scan / openharmony_workflow / openharmony_batch_workflow / openharmony_submit / openharmonyctf_skill_check / pin_openharmony_repos / check_api_extra（含泄漏凭据）
- `tests/`：test_openharmony / test_openharmony_policy / test_openharmony_workflow / test_openharmonyctf_skill
- `backend/tools/registry/openharmony.yaml`、`backend/tools/catalog_docs/openharmony_*.md`（8 个）
- 数据目录：`openharmony/`、`to_gamer/`；垃圾文件：`tmp_baidu.html`、`tmp_wp_download.js`、`0`

**通用文件清理引用（删 import 顺序敏感：先解 `gzctf.py:9` 和 `base_member.py:25` 两处 import 再删包）：**
- `backend/core/config.py:13,22`：Category/CATEGORIES 去掉 `"openharmony"`
- `backend/platform/__init__.py`：去掉 4 个 openharmony 导出
- `backend/members/base_member.py`：删 :25-34 policy 导入、:1029/:1035-1044 `is_openharmony` 分支、:1064-1091 `competition_policy` 块
- `backend/members/adapters.py:153-169`：删 `_OPENHARMONY_SYSTEM_APPEND`
- `backend/tools/catalog.yaml`：删 :11 分组 + :487-583 的 8 个条目
- `backend/tools/member_tools.txt`：删 :183-212 整节
- `frontend/index.html:929`：类别数组去 `"openharmony"`
- `tests/test_frontend.py:94`、`tests/test_tools_mcp.py:44,49-52`：同步改断言

## 3. GZCTF 保留并解耦

- `backend/platform/gzctf.py`：删 `from backend.openharmony.policy import validate_nday_answer`（:9）、OpenHarmony 基线校验（:191-194）、L1 trackId 特判（:401-405，这是 harmonyctf 比赛的 ext 契约）；保留登录/认证流、通用 preflight（锁定/冷却/已解出校验）和基础提交
- `tests/test_gzctf.py`：删 L1-trackId 和 OpenHarmony 基线用例，保留通用用例
- `scripts/fetch_gzctf_targets.py`、`gzctf_platform_check.py` 改为输出到通用位置（不再写 `openharmony/targets.yaml`）

## 4. 新增 ret2shell 适配（完整集成）

### 4.1 核心客户端 `backend/platform/ret2shell.py`
`Ret2ShellClient`（仿 GZCTFClient 结构，可注入 mock session 便于测试）：
- 配置走环境变量：`IPC_R2S_BASE_URL`（默认 `https://ctf.xidian.edu.cn`）、`IPC_R2S_GAME_ID`（默认 `37`）、`IPC_R2S_USERNAME`/`IPC_R2S_PASSWORD`/`IPC_R2S_TOKEN`
- 认证：login 捕获 `Set-Token` → Bearer；所有响应检查 `Set-Token` 滚动更新；401 时重登录一次；Basic 兜底
- `list_challenges()`：解析 `[items, total]`，归一化 id/name/tag→分类/score/solved 状态
- 附件：`list_files()` + 下载（folder/file query 参数）
- 靶机：`start_instance()`/`renew_instance()`/`destroy_instance()`/`list_instances()`，含等待 `exposed_ports` 就绪的轮询
- `submit_flag()`：**提交前 preflight**（已解出拒绝 + 客户端限速器保护 5min/10 次共享限额）→ POST → 轮询判题结果（1s×最多 7 次）→ 返回 `solved`/`result`
- 错误处理按纯文本 body + 状态码分支（429 限速、412 未开始/无靶机、409 已启动等）

### 4.2 题目导入集成（走现有通用机制）
- `mapping.py`：`FieldMapping` 增加可选 `platform: str = "http_json"` 与 `game_id: int | None`
- `api/platform.py`：`_fetch()` 按 `platform == "ret2shell"` 构造 `Ret2ShellAdapter(PlatformAdapter)`（凭据只从环境变量取，绝不进请求体）
- `ops/models.py` 的 `PlatformWorkflowSpec` 透传 `platform`/`game_id`，IPC 运维代理即可驱动导入
- `tests/test_platform.py` 补 ret2shell 导入路径用例（mock）

### 4.3 Member 平台 MCP（凭据留在后端，不进沙箱）
- 新建 `backend/platform/ret2shell_mcp.py`：`build_ret2shell_mcp(client)` 暴露 `instance_start` / `instance_status` / `instance_renew` / `instance_stop` / `challenge_status`（含解出状态查询），仿 `tool_mcp.py` 的工厂模式
- Member 按项目接入该 MCP（接线方式与现有 browser/reverse MCP 相同，在成员 MCP 装配处注册）；pwn 题成员可全自动拿靶机地址开始攻击
- flag 提交**不**给 Member（防误耗共享限额），由 operator 走脚本确认提交

### 4.4 脚本与文档
- `scripts/fetch_ret2shell_targets.py`：拉 game 37 题目列表（含分值/分类/已解状态）输出 YAML
- `scripts/ret2shell_platform_check.py`：登录/比赛状态只读冒烟
- `scripts/ret2shell_submit.py`：flag 提交，**默认 dry-run**，`--no-dry-run` 才真交，自动轮询判题结果并打印
- `docs/ret2shell.md`：环境变量配置、使用流程（拉题→导入→解题→提交）
- `member_tools.txt` 新增 ret2shell 章节：MCP 工具用法、靶机生命周期、提交限额纪律

## 5. 测试

- 新建 `tests/test_ret2shell.py`（仿 test_gzctf.py 的 mock 模式）：登录/Set-Token 刷新/401 重登、`[items,total]` 解析与分类映射、附件列举下载、实例生命周期与 exposed_ports 等待、异步提交轮询（solved null→true）、429 限速、客户端限速器、已解出 preflight 拒绝、适配器→PlatformChallenge 转换
- 修正 test_frontend / test_tools_mcp / test_gzctf；test_catalog 因三件套同步删除自然通过
- 全量 `pytest` 绿（基线 238 通过，删 openharmony 测试后约 210+新增 ~20）

## 6. 验证顺序

1. 删除+清理引用 → 确认 `python -c "import backend.main"`（或等价入口）可导入
2. 写 ret2shell 客户端+MCP+脚本+测试
3. 全量 pytest
4. （可选，需要凭据时）用真实账号对 game 37 做只读冒烟（仅 GET，不提交）

**风险提示**：test_gzctf.py 中部分用例测的正是将被删除的 L1-trackId 行为，删除后需同步移除；registry/catalog/catalog_docs 三件套必须一起删否则 test_catalog 挂。`data/auth.json` 等运行时产物不动。