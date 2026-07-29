# Memory MCP

检索当前运行期经验和只读的预装工具知识目录。

## 用途与适用场景

先 memory_search，再用 memory_get 读取经验或 memory_doc 打开工具文档。

## 版本检查

```bash
ipc-mcp-server --help
```

## 命令、导入与镜像路径

- 常用入口：`ipc-mcp-server memory`
- 镜像路径：`backend/memory/memory_mcp.py`

## 常用工作流

1. 在 Member 工作目录中确认附件、目标地址和授权范围。
2. 按题目类型使用 `ipc-mcp-server memory` 进行最小化探测。
3. 保存原始输出，再根据结果逐步增加参数，避免一开始执行破坏性操作。

## 可执行示例

```bash
ipc-mcp-server memory --transport stdio
```

## 输出解释

重点检查退出码、错误输出、命中项、地址/偏移和生成文件；将可复现结论记录到项目事实与 WP。

## 常见错误与限制

版本和参数可能随镜像升级变化，执行前先查看 `--help`。

## 关联条目

- 可通过 Memory 工具目录返回同级目录查看相关能力。

## 官方参考

- 使用镜像内 `--help`、语言内置帮助或工具上游仓库作为版本对应参考。
