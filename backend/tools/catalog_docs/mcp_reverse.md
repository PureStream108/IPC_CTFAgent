# reverse MCP

通过隔离的 PyGhidra worker 和 radare2 完成反编译与二进制分析。

## 用途与适用场景

优先调用 file_info/list_functions，再按函数调用 decompile；超时会自动降级到 r2。

## 版本检查

```bash
ipc-mcp-server --help
```

## 命令、导入与镜像路径

- 常用入口：`ipc-mcp-server reverse`
- 镜像路径：`backend/mcp/reverse_mcp.py`

## 常用工作流

1. 在 Member 工作目录中确认附件、目标地址和授权范围。
2. 按题目类型使用 `ipc-mcp-server reverse` 进行最小化探测。
3. 保存原始输出，再根据结果逐步增加参数，避免一开始执行破坏性操作。

## 可执行示例

```bash
ipc-mcp-server reverse --transport stdio
```

## 输出解释

重点检查退出码、错误输出、命中项、地址/偏移和生成文件；将可复现结论记录到项目事实与 WP。

## 常见错误与限制

版本和参数可能随镜像升级变化，执行前先查看 `--help`。

## 关联条目

- [PyGhidra](/memory/catalog/lib_pyghidra/document)
- [radare2](/memory/catalog/tool_radare2/document)

## 官方参考

- 使用镜像内 `--help`、语言内置帮助或工具上游仓库作为版本对应参考。
