# browser MCP

在每个 Member 的任务容器中启动独立的 Playwright Chromium 会话，用于渲染页面、执行结构化交互、观察浏览器侧网络与 JavaScript 错误，以及保存截图和下载产物。

## 用途与适用场景

- 访问依赖 JavaScript 渲染的题目页面或 Member 启动的本地 Web UI。
- 稳定等待 SPA 更新、表单提交跳转和异步提示，而不是反复截图猜测状态。
- 查看经过脱敏和数量限制的 Fetch/XHR、控制台日志与未捕获页面异常。
- 上传 Member/共享工作区中的文件，或将页面下载和截图保留为可追踪产物。
- 在同一次 Member solve 中保留页面、Cookie 和登录状态。

优先使用 `wait_for`、`click`、`fill`、`upload_file` 等结构化工具；只有缺少对应结构化操作时才使用 `eval_js`。

## 版本检查

```bash
ipc-mcp-server --help
```

具体工具参数以 MCP `tools/list` 返回的 schema 和当前镜像内实现为准。

## 命令、导入与镜像路径

- 常用入口：`ipc-mcp-server browser`
- MCP 实现：`backend/mcp/shared.py`
- 运行时注入：`backend/core/orchestrator.py`
- 工具目录：`backend/tools/registry/web.yaml`
- 运行位置：任务容器的 Member 工作目录

Browser MCP 由编排器通过 stdio 按需启动，Member 通常通过 `tool` action 调用，不需要自行启动服务器：

## 可执行示例

```json
{
  "kind": "tool",
  "args": {
    "server": "browser",
    "tool": "navigate",
    "args": {"url": "http://challenge"}
  }
}
```

调试入口：

```bash
ipc-mcp-server browser --transport stdio
```

- 默认页面 ID 为 `main`；新打开的页面会获得稳定的 `page_01`、`page_02` 等 ID。
- 未传 `page_id` 时，工具操作当前活动页面；返回结果会带上实际 `page_id` 和 URL。
- Browser、Context、页面、Cookie 和事件缓冲区仅属于当前 Member 的本次 solve，不与其他 Member 共享。
- MCP 会话结束时会关闭 Context、Chromium 和 Playwright，不保留常驻浏览器进程。

## 常用工作流

1. 确认目标 URL、授权范围和需要上传的工作区文件。
2. 调用 `network_log_start`，再用 `navigate` 打开页面。
3. 用 `wait_for` 等待确定状态，再执行 `click`、`fill`、`press` 或 `upload_file`。
4. 按 `after_id` 增量读取网络、控制台和页面错误，避免重复传入历史事件。
5. 用 `screenshot` 或 `download` 保存关键证据，在事实和 WP 中记录 artifact ID 与摘要。
6. 调用 `network_log_stop` 结束网络记录。

## 导航、交互与页面读取

| 工具 | 主要参数 | 说明 |
| --- | --- | --- |
| `navigate` | `url`, `wait_until`, `timeout_ms`, `page_id?` | 导航到 HTTP(S) URL，返回状态、Content-Type、最终 URL、标题和有界可见文本。 |
| `click` | `selector`, `page_id?` | 点击元素，并返回点击后的简短页面快照。 |
| `fill` | `selector`, `value`, `page_id?` | 填写输入框；结果只返回值长度，不回显填写内容。 |
| `press` | `key`, `page_id?` | 在当前页面发送键盘按键。 |
| `wait_for` | `selector?`, `url?`, `state`, `load_state?`, `timeout_ms`, `page_id?` | 等待元素、URL 或加载状态；三种条件至少提供一种。 |
| `get_content` | `page_id?` | 返回渲染后的 HTML 和可见文本；大型页面应谨慎调用。 |
| `eval_js` | `script`, `page_id?` | 在页面上下文执行 JavaScript；仅用于结构化工具无法覆盖的读取或交互。 |

`wait_for.state` 支持 `attached`、`detached`、`visible`、`hidden`；`load_state` 支持 `domcontentloaded`、`load`、`networkidle`。

## 网络与 JavaScript 诊断

推荐流程：先启动网络记录，再执行导航或交互，最后增量读取事件并停止记录。

| 工具 | 说明 |
| --- | --- |
| `network_log_start` | 开始记录；默认忽略图片、字体和媒体等资源，聚焦 document、XHR、Fetch 和 script。可显式启用受限文本响应预览。 |
| `network_log_list` | 通过 `after_id` 增量读取；支持 `url_contains`、`methods`、`statuses` 过滤，返回 `next_after_id` 和 `dropped_count`。 |
| `network_log_stop` | 停止记录并返回保留事件数；不会清除已经保留的事件。 |
| `console_logs` | 读取控制台事件；支持 `after_id`、`limit` 和 `levels`。 |
| `page_errors` | 读取未捕获页面异常的名称、消息和有界堆栈。 |

网络事件包含方法、脱敏 URL、资源类型、状态、Content-Type、耗时、失败原因和重定向来源。默认不记录请求体、认证头、Cookie 或完整响应体；响应预览仅在显式启用时记录受支持的文本类型，并受运行时长度上限控制。

所有事件使用单调递增的 `event_id`，并保存在有界环形缓冲区中。缓冲区溢出时旧事件会被丢弃，`dropped_count` 会反映丢弃数量。

## Cookie 与敏感值

| 工具 | 说明 |
| --- | --- |
| `cookies` | 默认返回 Cookie 元数据、`[REDACTED]` 和值长度；只有显式设置 `include_values=true` 才返回明文值。 |
| `set_cookie` | 在当前 Context 中添加或替换 Cookie；结果不会回显 Cookie 明文。 |

网络 URL、响应预览、控制台日志、页面错误和产物元数据会对 Authorization、Cookie、密码以及名称包含 token、secret、key 的常见敏感值进行脱敏。`include_values=true` 和 `set_cookie` 会在 Member 工具日志中标记为敏感操作。

## 上传、下载与截图产物

| 工具 | 说明 |
| --- | --- |
| `upload_file` | 使用 `selector` 和 `paths` 设置文件输入；路径必须解析到当前 Member 工作区或 `/workspace/shared` 中的真实普通文件，符号链接越界会被拒绝。 |
| `download` | 必须且只能提供 `selector` 或 `url`；下载保存后返回建议文件名、artifact ID、大小、SHA-256 和相对路径。 |
| `screenshot` | 截取当前页面；默认保存为项目隔离的 screenshot artifact，并返回 artifact ID、大小、SHA-256 和相对路径。 |

产物目录：

```text
/workspace/<member>/browser-artifacts/
├── screenshots/
├── downloads/
└── metadata.jsonl
```

`metadata.jsonl` 每行记录时间、project ID、Member、工具、产物类型、artifact ID、相对路径、大小、SHA-256、脱敏 URL 和请求摘要。大型文件内容不会直接进入模型上下文；应在事实或 WP 中引用 artifact ID 与摘要。

## 输出解释

- 成功结果包含 `available: true`。
- 工具异常转换为 `available: false`、稳定的 `tool` 名称和经过脱敏的 `error`，不会把未处理异常抛到 MCP transport。
- 页面文本、响应预览、事件数量、错误堆栈和单个 artifact 大小均有上限。
- `network_log_list`、`console_logs`、`page_errors` 应使用返回的 `next_after_id` 继续读取，避免重复把历史事件送入上下文。

## 常见错误与限制

- 导航仅支持 HTTP(S)，拒绝 `file:`、`javascript:`、`data:` 等协议。
- 当 `browser_allowed_origins` 非空时，请求层只放行白名单中的 `scheme + host + port`，包括导航重定向和子资源请求。
- Browser MCP 不复用宿主机浏览器 Profile、账号、扩展或本地登录状态，也不会增加任务容器原本没有的网络权限。
- 当前版本尚未提供 `tabs_list/tab_*`、Frame 工具、Storage 工具、Trace、HAR、设备模拟或请求改写；不要假定这些 Phase 2/3 能力已经可用。
- Browser MCP 负责真实浏览器交互与浏览器侧观测，不替代 ZAP 的爬虫或主动扫描能力。

## 运行时配置

运行时可配置事件缓冲上限、控制台/错误上限、响应预览字节数、origin 白名单和单个产物大小上限。默认值由编排器注入到每个 Member 的 Browser MCP 进程，并在 `backend/core/config.py` 中进行上限校验。

## 关联条目

- 可通过 Memory 工具目录查看同级 MCP 与 Web 工具能力。

## 官方参考

- 以当前 MCP `tools/list` schema、镜像内 `--help` 和仓库实现为版本对应依据。
- Playwright Python API：<https://playwright.dev/python/docs/api/class-page>
