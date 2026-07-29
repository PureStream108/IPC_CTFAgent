# ghidra

PyGhidra decompiler in the task container (reverse MCP decompile/decompile_all).

## 用途与适用场景

Decompile binaries, analyze functions, rename/annotate.

## 版本检查

```bash
ipc-mcp-server --help
```

## 命令、导入与镜像路径

- 常用入口：`ipc-mcp-server reverse`
- 镜像路径：`task-container`

## 常用工作流

1. 在 Member 工作目录中确认附件、目标地址和授权范围。
2. 按题目类型使用 `ipc-mcp-server reverse` 进行最小化探测。
3. 保存原始输出，再根据结果逐步增加参数，避免一开始执行破坏性操作。

## 可执行示例

```bash
ipc-mcp-server reverse --help
```

## 输出解释

重点检查退出码、错误输出、命中项、地址/偏移和生成文件；将可复现结论记录到项目事实与 WP。

## 常见错误与限制

仅在合法授权的 CTF、靶场和研究环境中使用。

## 关联条目

- 可通过 Memory 工具目录返回同级目录查看相关能力。

## 官方参考

- 使用镜像内 `--help`、语言内置帮助或工具上游仓库作为版本对应参考。
