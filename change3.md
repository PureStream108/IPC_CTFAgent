# Browser MCP 能力补全方案

## 1. 背景与现状

当前 Browser MCP 位于 `backend/mcp/shared.py`，由 Member 在 Docker 题目容器内按需通过 stdio 启动：

```text
Member
  -> MCPRegistrySession（首次调用时建立连接）
  -> docker exec -i -w /workspace/<member> ipc-task-<project>
       python3 -m backend.mcp.mcp_server browser
  -> Browser MCP
  -> Playwright async API / headless Chromium
```

现有 Browser MCP 已具备以下有状态工具：

| 工具 | 现有能力 |
| --- | --- |
| `navigate` | 访问 URL，返回状态、最终 URL、标题、可见文本与 Content-Type |
| `click` / `fill` / `press` | 基础页面交互 |
| `eval_js` | 在当前页面上下文执行 JavaScript |
| `get_content` | 读取渲染后的 HTML 与文本 |
| `screenshot` | 保存页面截图 |
| `cookies` / `set_cookie` | 读取和写入 Browser Context Cookie |

浏览器进程、Context 和 Page 在一次 Member 解题会话中复用，因此 Cookie 与页面状态能够延续；Member 会话结束后，对应 MCP stdio 进程退出，浏览器状态也随之释放。

当前实现适合基础页面浏览与单页交互，但存在四类缺口：

1. **可观测性不足**：没有结构化网络记录、控制台日志、页面错误、Trace 或 HAR。
2. **交互覆盖不足**：缺少等待、上传下载、表单选择、悬停、多页面和 iframe 操作。
3. **产物管理不足**：截图仅返回容器内路径；没有统一的项目级浏览器产物目录和元数据。
4. **模型调用稳定性不足**：通用 `eval_js` 可以绕过部分限制，但难以复现、难以审计，也不适合作为常规交互接口。

## 2. 目标与非目标

### 2.1 目标

- 为授权 CTF/靶场任务提供可复现、可审计的浏览器自动化与页面诊断能力。
- 保持当前“每个 Member 的浏览器会话独立”的隔离模型，不共享 Cookie、页面或临时文件。
- 将常用操作提供为结构化 MCP 工具，降低模型依赖任意 JavaScript 的比例。
- 将截图、下载、HAR 与 Trace 作为项目产物保存，可通过日志、Writeup 或 Derive 导出追溯。
- 保持 MCP 结果有界：摘要直接返回，大型内容以产物引用返回。

### 2.2 非目标

- 不将 Browser MCP 变为用户桌面 Chrome 的远程控制接口。
- 不复用用户本机浏览器 Profile、账号或扩展。
- 不默认记录全部响应体、认证信息或 Cookie。
- 不提供突破授权范围、绕过访问控制或隐蔽采集凭据的功能。
- 不替代 ZAP：Browser MCP 负责真实页面交互与浏览器侧观测；ZAP 仍是可选的共享爬虫/主动扫描服务。

## 3. 总体设计

### 3.1 会话模型

扩展 `_BrowserSession`，使其显式维护：

```text
BrowserSession
├── Playwright
├── Browser (Chromium, headless)
├── BrowserContext (单 Member、单次 solve)
├── pages: {page_id -> Page}
├── active_page_id
├── network_events: 有界环形缓冲区
├── console_events: 有界环形缓冲区
├── page_errors: 有界环形缓冲区
├── artifact_root: /workspace/<member>/browser-artifacts/
└── trace / HAR 状态
```

设计原则：

- 默认创建一个 `main` Page；导航、点击、填写等旧工具继续操作当前活动 Page，保持兼容。
- 每个新打开的页面赋予稳定的 `page_id`，例如 `page_01`。
- 所有事件以单调递增 `event_id` 记录；查询工具支持按 `after_id` 增量读取，避免模型重复接收历史。
- 内存事件采用环形缓冲区；默认最多保留 200 条网络事件、100 条控制台事件和 50 条页面错误。溢出时记录 `dropped_count`。
- 会话关闭时停止录制、关闭页面/Context/Browser；不在容器内遗留常驻浏览器进程。

### 3.2 产物目录与导出

Browser MCP 在 Member 工作目录下创建：

```text
/workspace/<member>/browser-artifacts/
├── screenshots/
├── downloads/
├── traces/
├── har/
└── metadata.jsonl
```

每次产生文件时写入一行 `metadata.jsonl`，至少包含：时间、`project_id`、Member 名称、工具名、产物类型、相对路径、大小、SHA-256、关联 URL 与脱敏后的请求摘要。

后续可在项目导出阶段，将 `browser-artifacts/` 复制到 `data/logs/` 对应项目的快照目录。第一阶段允许仍以容器路径返回，但必须同时返回相对产物 ID；第二阶段再增加 API/Derive 浏览和下载入口。

### 3.3 URL 与敏感数据边界

- Browser MCP 只允许访问项目任务容器当前可访问的网络；它不新增宿主机网络能力。
- 新增可选运行时配置 `browser_allowed_origins`；非空时仅允许 `scheme + host + port` 在白名单内的 URL。CTF 题目可由创建项目时写入初始 origin。
- 禁止或拒绝 `file:`、`javascript:`、`data:` 等非 HTTP(S) 导航协议；`about:blank` 仅用于新建页面。
- 网络日志默认不记录 `Cookie`、`Authorization`、`Proxy-Authorization`、`Set-Cookie`、表单密码字段及名称含 `token`、`secret`、`key` 的值。
- 响应体默认不写入网络日志。仅通过显式的、长度受限的 `network_body` 工具读取文本响应预览。
- Cookie 和 Storage 工具的值默认脱敏；只有显式 `include_values=true` 时才返回值，并在日志中标记为敏感操作。该选项应仅向已经具备任务工具权限的 Member 暴露。

## 4. MCP 接口设计

所有工具都遵循现有返回约定：成功时返回 `available: true`，异常时返回 `available: false`、`tool` 与可读 `error`；绝不抛出未处理异常到 MCP transport。

### 4.1 P0：稳定交互与调试闭环

#### `wait_for`

```text
wait_for(
  selector?: str,
  url?: str,
  state: "attached" | "detached" | "visible" | "hidden" = "visible",
  load_state?: "domcontentloaded" | "load" | "networkidle",
  timeout_ms: int = 10000,
  page_id?: str
)
```

- 必须至少指定 `selector`、`url` 或 `load_state` 之一。
- `selector` 使用 `page.wait_for_selector`；`url` 使用 `page.wait_for_url`；`load_state` 使用 `page.wait_for_load_state`。
- 返回等待条件、耗时、当前 URL、标题和简短页面快照。
- 用途：等待 SPA 渲染、提交后的跳转、异步验证码/提示信息出现。

#### `network_log_start` / `network_log_list` / `network_log_stop`

```text
network_log_start(
  include_resources: bool = false,
  capture_response_preview: bool = false,
  preview_limit: int = 4096,
  page_id?: str
)

network_log_list(
  after_id?: int,
  limit: int = 50,
  url_contains?: str,
  methods?: list[str],
  statuses?: list[int]
)

network_log_stop()
```

- 使用 Playwright 的 `request`、`response`、`requestfailed` 事件，并通过请求对象关联请求与响应。
- 单条事件包含：`event_id`、时间、类型、方法、脱敏 URL、资源类型、状态、Content-Type、耗时、失败原因、重定向来源和可选文本预览。
- `include_resources=false` 时过滤 image/font/media 等静态资源，默认聚焦 document、xhr、fetch、script。
- 响应预览仅限 `text/*`、JSON、JavaScript、XML 等文本型响应；最多 4 KiB，二进制永不内联。
- 不记录完整请求体、认证头和 Cookie。

#### `console_logs` / `page_errors`

```text
console_logs(after_id?: int, limit: int = 50, levels?: list[str])
page_errors(after_id?: int, limit: int = 50)
```

- `console_logs` 监听 `page.on("console")`，保存级别、文本、位置（可用时）和事件时间。
- `page_errors` 监听 `page.on("pageerror")`，保存异常名称、消息和有限栈信息。
- 两者都返回 `next_after_id`、`dropped_count` 和当前 Page 标识。

#### `upload_file`

```text
upload_file(
  selector: str,
  paths: list[str],
  page_id?: str
)
```

- 仅接受 Member 工作区及共享工作区内的真实路径；通过 `Path.resolve()` 校验必须位于允许根目录。
- 使用 `locator.set_input_files`，不允许由模型写入任意宿主路径。
- 返回已上传文件的基名、大小和页面 URL，不回传文件内容。

#### `download`

```text
download(
  selector?: str,
  url?: str,
  timeout_ms: int = 30000,
  page_id?: str
)
```

- 必须指定 `selector` 或 `url`。selector 模式使用 `page.expect_download()` 包裹点击；URL 模式先导航，只有下载事件发生才算成功。
- 文件保存到 `browser-artifacts/downloads/`，使用随机安全文件名，并写入 metadata。
- 返回 `artifact_id`、建议文件名、大小、SHA-256 与相对路径。

### 4.2 P1：表单、页面与 Frame

#### `select_option`、`check`、`uncheck`、`hover`

```text
select_option(selector: str, values: list[str] | str, page_id?: str)
check(selector: str, page_id?: str)
uncheck(selector: str, page_id?: str)
hover(selector: str, page_id?: str)
```

- 全部基于 Playwright Locator，保留页面原生事件语义。
- 成功后返回选择器、当前 URL 和简短快照；不返回完整 HTML。

#### `tabs_list`、`tab_new`、`tab_switch`、`tab_close`

```text
tabs_list()
tab_new(url?: str)
tab_switch(page_id: str)
tab_close(page_id: str)
```

- `tab_new` 创建 Browser Context 内的新 Page；可选 URL 只能是 HTTP(S) 或 `about:blank`。
- 监听 Context 的 `page` 事件；当点击触发新页时自动登记 Page。
- 关闭活动页时自动选择最近仍存活的 Page；全部关闭时自动创建新的 `main` 页。

#### `frames_list`、`frame_get_content`、`frame_eval_js`

```text
frames_list(page_id?: str)
frame_get_content(frame_id: str, max_chars: int = 20000)
frame_eval_js(frame_id: str, script: str)
```

- `frames_list` 返回稳定 `frame_id`、URL、名称和是否主 Frame。
- 不使用临时数组下标作为 ID；采用 `id(frame)` 映射并在每次列表请求中刷新。
- 第一阶段仅提供读取与 JS 求值。Frame 内点击/填写可在 P2 再补充为 Locator 工具。

#### `storage_get` / `storage_set`

```text
storage_get(kind: "local" | "session", keys?: list[str], include_values: bool = false, page_id?: str)
storage_set(kind: "local" | "session", values: dict[str, str], page_id?: str)
```

- 通过 `page.evaluate` 读取或修改当前 origin 的 Storage。
- 默认仅返回 key、长度和是否存在；`include_values=true` 时必须进行敏感日志标记与长度限制。
- 不实现跨 origin Storage 读取。

### 4.3 P2：可复盘产物与环境模拟

#### `trace_start` / `trace_stop`

```text
trace_start(screenshots: bool = true, snapshots: bool = true, sources: bool = false)
trace_stop()
```

- 通过 `context.tracing.start` / `stop(path=...)` 写入 `browser-artifacts/traces/`。
- 同一 Context 同时只允许一份 Trace；重复 start 返回当前状态而非覆盖。
- `trace_stop` 返回 artifact ID。Trace 文件不可直接放入模型上下文。

#### `har_start` / `har_stop`

- Playwright 的 HAR 推荐在创建 Context 时配置 `record_har_path`，无法对已创建 Context 完整补录。
- 因此实现方案为：在 `har_start` 前创建一个新的、独立的观察 Context 与 Page，返回其 `page_id`；`har_stop` 关闭观察 Context 后生成 HAR artifact。
- 若用户需要保留登录状态，可在新 Context 创建前使用 `storage_state` 导出、再导入已脱敏的会话状态；默认不携带 Cookie 值，须显式确认。
- 如果该复杂度影响 P2 交付，可先仅实现 `trace_*`，HAR 作为后续独立变更。

#### `emulate`

```text
emulate(
  viewport?: {width: int, height: int},
  user_agent?: str,
  locale?: str,
  timezone_id?: str,
  color_scheme?: "light" | "dark" | "no-preference"
)
```

- Playwright 中多个 Context 属性不能在现有 Context 上安全修改。
- 该工具应创建新 Context，迁移当前 URL；Cookie/Storage 迁移必须显式选择，默认不迁移敏感状态。
- 不支持任意地理位置伪造，除非项目单独定义授权需求与精确边界。

#### `route_intercept`（暂不列入首批交付）

- 路由拦截、修改响应或 mock 请求对 Web 调试有价值，但容易让自动化行为脱离真实服务。
- 如后续引入，应只支持项目 origin 白名单内的精确 URL pattern，记录每次改写，并默认只读的 `route_observe` 模式。

## 5. 代码改造范围

| 文件/模块 | 改造内容 |
| --- | --- |
| `backend/mcp/shared.py` | 扩展 `_BrowserSession`；注册事件监听；实现新增 Browser MCP 工具；统一输出和异常转换。 |
| `backend/mcp/mcp_server.py` | 无需改动 server 名称；必要时添加 Browser 运行时配置参数。 |
| `backend/core/orchestrator.py` | 向 Browser MCP 注入 `project_id`、Member 工作目录、共享目录、允许 origin 和产物根目录环境变量。 |
| `backend/members/base_member.py` | 在 Member 上下文中描述新增工具；对大型产物只记录 ID/摘要。 |
| `backend/core/config.py` | 增加有默认值的 Browser 运行时配置，如事件上限、响应预览上限、origin 白名单开关。 |
| `backend/api/config.py` 与 `frontend/index.html` | 暴露受限的 Browser 运行配置；不在 UI 显示或回显敏感 Storage/Cookie。 |
| `backend/core/archive.py` / 导出逻辑 | 将 Browser artifacts 纳入项目归档与 Derive 快照。 |
| `tests/test_tools_mcp.py` | 补充 Fake Page/Context 的工具契约测试。 |
| `tests/test_orchestration.py` | 验证 Browser MCP 的 Docker 参数、路径和项目级隔离。 |
| `docker/member/Dockerfile` | Playwright 与 Chromium 已安装；只需在需要 Trace/HAR 的版本兼容性测试失败时调整版本固定策略。 |

### 5.1 运行时参数建议

```yaml
runtime:
  browser_event_limit: 200
  browser_console_limit: 100
  browser_error_limit: 50
  browser_response_preview_bytes: 4096
  browser_allowed_origins: []  # 空数组表示沿用当前题目容器网络边界
  browser_artifact_max_bytes: 52428800
```

配置必须有上限校验；例如事件上限不超过 1,000、响应预览不超过 16 KiB、单个产物不超过 50 MiB。

## 6. 分阶段实施计划

### Phase 0：基础重构与兼容性（先行）

1. 为 `_BrowserSession` 增加 `project_id`、Member、工作目录与 artifact root 配置对象。
2. 引入 Page 注册表和 `active_page_id`，让现有九个工具默认作用于活动页。
3. 为现有 `navigate`、`click`、`fill`、`press`、`eval_js`、`get_content`、`screenshot`、Cookie 工具补充 `page_id` 可选参数。
4. 统一工具返回中的 `page_id`、`url`、`artifact_id` 字段。
5. 确保旧调用没有传入 `page_id` 时，输出行为与当前版本兼容。

### Phase 1：P0 能力

1. 实现 `wait_for`。
2. 实现网络、控制台、页面错误的事件监听及有界查询接口。
3. 实现 `upload_file` 和 `download`，加入允许路径校验与元数据。
4. 调整截图工具：默认写入 artifact root，并返回 artifact ID，而非仅容器绝对路径。
5. 将 Browser artifact 摘要写入项目工具日志。

### Phase 2：P1 能力

1. 实现表单补充工具。
2. 实现 Tab 生命周期和 Page 切换。
3. 实现 Frame 枚举、读取和有限 JS 求值。
4. 实现受控 Storage 读取/写入。
5. 在 Member 提示词/工具描述中加入适用场景与优先顺序，建议优先使用结构化工具而不是 `eval_js`。

### Phase 3：P2 复盘与导出

1. 实现 Playwright Trace。
2. 将截图、下载、Trace 纳入归档与 Derive。
3. 评估并实现独立 Context 的 HAR 录制。
4. 实现 Context 级模拟；明确会话迁移和敏感状态确认规则。

### Phase 4：可选的高级网络能力

1. 先提供只读 `route_observe`，验证其日志和授权边界。
2. 经单独安全评审后，再考虑受严格范围限制的 `route_intercept`。

## 7. 测试与验收

### 7.1 单元测试

- 所有新增工具都应有 Fake Page/Fake Context 契约测试。
- 验证工具返回 `available: false` 时包含稳定的工具名和错误，不泄漏调用栈或秘密。
- 验证事件环形缓冲区的上限、溢出计数和 `after_id` 增量读取。
- 验证默认脱敏规则覆盖 Cookie、Authorization、Token、密码和 Set-Cookie。
- 验证 `upload_file` 拒绝工作区以外路径、符号链接越界与不存在文件。
- 验证截图、下载、Trace 的文件名不能路径穿越，且 metadata 含 SHA-256。
- 验证 tab 和 frame 操作不会使旧工具失去活动页。

### 7.2 集成测试

使用本地 HTTP 测试应用覆盖：

1. JavaScript 渲染后出现元素，`wait_for` 成功。
2. 表单提交发出 XHR，网络日志可得到脱敏后的请求/响应摘要。
3. 页面 `console.error` 与异常可被查询。
4. 文件上传和下载均生成 project/member 隔离的 artifact。
5. 点击新窗口链接后可用 `tabs_list` 和 `tab_switch` 操作。
6. iframe 内容可枚举并读取。
7. 两个 Member 的 Cookie、Storage、下载目录和网络日志互不混合。
8. Member MCP 会话结束后 Chromium 进程退出，资源不泄漏。

### 7.3 回归与验收标准

- 现有 9 个 Browser MCP 工具的测试保持通过。
- `pytest -q` 全量通过。
- `docker compose build ipc-task-image` 能成功构建，并能执行 Playwright Chromium。
- 新增 Browser 功能不改变默认 ZAP 关闭、Docker 沙箱与项目生命周期行为。
- 大型页面、下载和 Trace 不会直接进入模型上下文；模型只接收有界摘要与 artifact ID。
- 任意网络、Storage 或错误日志默认不暴露凭据明文。

## 8. 关键风险与决策

| 风险 | 处理策略 |
| --- | --- |
| 网络日志包含密钥 | 默认脱敏、默认不存请求体和完整响应体、限制预览类型与长度。 |
| 大文件消耗任务磁盘 | 单文件/单会话配额，定期记录大小；超限返回可行动错误。 |
| Playwright 事件顺序与页面跳转竞争 | 先注册监听器，再执行导航/点击；所有状态更新由会话锁保护。 |
| `eval_js` 难以审计 | 保留兼容，但在 Member 提示中优先推荐结构化工具；记录脚本摘要与长度。 |
| 多 Tab 与 Popup 导致页面引用失效 | 为 Page 分配稳定 ID，监听 close 事件并自动切换活动页。 |
| HAR 需要 Context 创建时配置 | 采用独立观察 Context 方案，不承诺对既有 Context 补录历史。 |
| CTF 目标差异大 | 不预设网站规则；使用项目 origin 白名单作为可选收紧措施。 |

## 9. 建议的首个合并单元

首个变更应控制在 Phase 0 + Phase 1，并只包含：

1. Page 标识与兼容性重构；
2. `wait_for`；
3. `network_log_*`、`console_logs`、`page_errors`；
4. `upload_file`、`download`；
5. 截图 artifact 化与 metadata；
6. 完整单元/集成测试。

这组能力已经能让 Member 从“看见页面”升级为“能够稳定等待、解释前端行为、保留关键证据”，同时不会过早引入多 Context、HAR、路由改写等高复杂度功能。
