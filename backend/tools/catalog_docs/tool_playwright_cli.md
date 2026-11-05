# playwright-cli

通过 @playwright/cli 按需启动无头 Chromium，执行页面打开、快照、截图和交互。

## 用途与适用场景

需要真实浏览器渲染 JavaScript、检查登录态、点击表单或保存页面证据时使用。镜像不预启动 Chrome，也没有 Browser MCP。

## 版本检查

```bash
playwright-cli --help | head -20
```

## 命令、导入与镜像路径

- 常用入口：`playwright-cli`
- 镜像路径：`/opt/ipc-tools/tools/playwright-cli/playwright-cli、/usr/local/bin/playwright-cli、/opt/playwright-browsers`

## 常用工作流

为每个 project/member 使用唯一 `-s` 会话名；先 `open URL`，再 `snapshot` 或 `screenshot --filename=...`，最后 `close`。若 CLI 自动退出，重新 open 即可；不要把会话名复用到其他成员。

## 可执行示例

```bash
playwright-cli -s=demo-amber open http://127.0.0.1:8000 && playwright-cli -s=demo-amber snapshot --filename=/tmp/snap.yml && playwright-cli -s=demo-amber screenshot --filename=/tmp/shot.png && playwright-cli -s=demo-amber close
```

## 输出解释

snapshot 返回可访问性树/YAML，screenshot 写入 PNG。检查页面标题、表单字段、隐藏链接和网络错误；截图后用 file 和图片查看器确认非空。

## 常见错误与限制

容器内 Chromium 以无头模式运行，可能被目标站点的 bot 检测拦截。CLI 会话是临时的，任务结束应 close；不要依赖宿主机浏览器或持久 Profile。

## 关联条目

- [curl](/memory/catalog/tool_curl/document)
- [JavaScript / Node.js](/memory/catalog/lang_nodejs/document)

## 官方参考

- [https://playwright.dev/](https://playwright.dev/)
