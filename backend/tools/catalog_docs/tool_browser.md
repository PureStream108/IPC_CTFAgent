# browser

Browser 是运行在任务容器内的有状态 Playwright Chromium MCP。它提供结构化页面交互、有界且默认脱敏的网络/JavaScript 诊断，以及项目隔离的截图和下载产物。

## 用途与适用场景

- JavaScript 页面、SPA、异步表单和需要保持 Cookie 的登录流程。
- 需要解释 Fetch/XHR、控制台错误或未捕获页面异常时。
- 需要向页面上传工作区文件，或保存下载与截图证据时。
- Member 启动本地 Web UI 后，使用观察结果中的共享 URL，而不是容器内的 `127.0.0.1`。

优先使用结构化工具，缺少相应能力时再调用 `eval_js`。

## 版本检查

```bash
ipc-mcp-server --help
```

## 命令、导入与镜像路径

- 容器入口：`ipc-mcp-server browser`
- MCP 实现：`backend/mcp/shared.py`
- 镜像位置：`task-container`

## 常用工作流

1. `network_log_start(capture_response_preview=false)` 开始有界网络记录。
2. `navigate(url=...)` 打开目标，记录返回的 `page_id`。
3. 用 `wait_for` 等待 DOM、URL 或加载状态，再调用 `click`、`fill`、`press` 或 `upload_file`。
4. 用 `network_log_list`、`console_logs`、`page_errors` 解释页面行为；使用 `after_id` 增量读取。
5. 用 `screenshot` 或 `download` 保存证据，并在事实/WP 中引用 artifact ID、相对路径和 SHA-256。
6. `network_log_stop()` 停止网络记录。

## 可执行示例

```json
{
  "kind": "tool",
  "args": {
    "server": "browser",
    "tool": "wait_for",
    "args": {"selector": "#result", "timeout_ms": 10000}
  }
}
```

## 当前工具

- 页面：`navigate`、`click`、`fill`、`press`、`wait_for`、`get_content`、`eval_js`
- 诊断：`network_log_start`、`network_log_list`、`network_log_stop`、`console_logs`、`page_errors`
- 状态：`cookies`、`set_cookie`
- 文件与证据：`upload_file`、`download`、`screenshot`

所有旧页面工具都支持可选 `page_id`。未传时操作当前活动页面；默认页面为 `main`，新页面使用稳定的 `page_XX` ID。

## 输出解释

- 成功返回 `available: true`；失败返回 `available: false`、`tool` 和脱敏后的 `error`。
- 仅允许 HTTP(S) 导航；配置了 `browser_allowed_origins` 时会在请求层阻断白名单外的导航、重定向和子资源。
- Cookie 默认不返回明文；网络日志不记录请求体、认证头、Cookie 或完整响应体。
- 响应预览必须显式开启，只支持文本类型并受字节上限控制。
- 上传路径只能位于当前 Member 工作区或 `/workspace/shared`，符号链接越界会被拒绝。
- 截图和下载保存到 `/workspace/<member>/browser-artifacts/`，返回 artifact ID、相对路径、大小和 SHA-256；大型内容不会直接进入模型上下文。

## 常见错误与限制

当前尚未实现 Tab 管理、Frame/Storage 工具、Trace、HAR、设备模拟和路由改写。Browser MCP 也不复用宿主机 Chrome Profile，不替代 ZAP 的爬虫或主动扫描。

## 调试入口与实现位置

```bash
ipc-mcp-server browser --transport stdio
```

- 容器入口：`ipc-mcp-server browser`
- MCP 实现：`backend/mcp/shared.py`
- 镜像位置：`task-container`

完整参数、产物元数据和诊断字段说明参见同一工具目录中的 `browser MCP` 条目。

## 关联条目

- 可通过 Memory 工具目录查看 `browser MCP`、ZAP 和其他 Web 工具条目。

## 官方参考

- 以当前 MCP `tools/list` schema、镜像内 `--help` 和仓库实现为版本对应依据。
- Playwright Python API：<https://playwright.dev/python/docs/api/class-page>
