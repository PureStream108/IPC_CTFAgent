# PyGhidra

从 Python 启动 Ghidra、分析程序并调用反编译 API。

## 用途与适用场景

项目通过 reverse MCP 的隔离 worker 调用 PyGhidra，避免直接启动无法回收的 JVM。

## 版本检查

```bash
python3 -c "import importlib.metadata,pyghidra; print(importlib.metadata.version('pyghidra'))"
```

## 命令、导入与镜像路径

- 常用入口：`python3 -c "import importlib.metadata,pyghidra; print(importlib.metadata.version('pyghidra'))"`
- 镜像路径：`/opt/ghidra/Ghidra/Features/PyGhidra`

## 常用工作流

1. 在 Member 工作目录中确认附件、目标地址和授权范围。
2. 按题目类型使用 `python3 -c "import importlib.metadata,pyghidra; print(importlib.metadata.version('pyghidra'))"` 进行最小化探测。
3. 保存原始输出，再根据结果逐步增加参数，避免一开始执行破坏性操作。

## 可执行示例

```bash
python3 -c "import pyghidra; print(pyghidra)"
```

## 输出解释

重点检查退出码、错误输出、命中项、地址/偏移和生成文件；将可复现结论记录到项目事实与 WP。

## 常见错误与限制

版本和参数可能随镜像升级变化，执行前先查看 `--help`。

## 关联条目

- 可通过 Memory 工具目录返回同级目录查看相关能力。

## 官方参考

- [https://github.com/NationalSecurityAgency/ghidra/tree/master/Ghidra/Features/PyGhidra](https://github.com/NationalSecurityAgency/ghidra/tree/master/Ghidra/Features/PyGhidra)
