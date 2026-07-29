# angr

对二进制执行符号执行、CFG 恢复和路径约束求解。

## 用途与适用场景

先确认输入与目标，再使用 angr 完成针对性分析。

## 版本检查

```bash
python3 -c "import angr; print(angr.__version__)"
```

## 命令、导入与镜像路径

- 常用入口：`python3 -c "import angr; print(angr.__version__)"`
- 镜像路径：`Python site-packages/angr`

## 常用工作流

1. 在 Member 工作目录中确认附件、目标地址和授权范围。
2. 按题目类型使用 `python3 -c "import angr; print(angr.__version__)"` 进行最小化探测。
3. 保存原始输出，再根据结果逐步增加参数，避免一开始执行破坏性操作。

## 可执行示例

```bash
python3 -c "import angr; p=angr.Project('./challenge',auto_load_libs=False); print(p.entry)"
```

## 输出解释

重点检查退出码、错误输出、命中项、地址/偏移和生成文件；将可复现结论记录到项目事实与 WP。

## 常见错误与限制

版本和参数可能随镜像升级变化，执行前先查看 `--help`。

## 关联条目

- 可通过 Memory 工具目录返回同级目录查看相关能力。

## 官方参考

- [https://docs.angr.io/](https://docs.angr.io/)
