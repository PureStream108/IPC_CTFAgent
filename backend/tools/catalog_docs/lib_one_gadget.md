# one_gadget

枚举 libc 中满足特定约束即可触发 execve 的 one-shot gadget。

## 用途与适用场景

先确认输入与目标，再使用 one_gadget 完成针对性分析。

## 版本检查

```bash
one_gadget --version
```

## 命令、导入与镜像路径

- 常用入口：`one_gadget`
- 镜像路径：`Ruby gems`

## 常用工作流

1. 在 Member 工作目录中确认附件、目标地址和授权范围。
2. 按题目类型使用 `one_gadget` 进行最小化探测。
3. 保存原始输出，再根据结果逐步增加参数，避免一开始执行破坏性操作。

## 可执行示例

```bash
one_gadget ./libc.so.6
```

## 输出解释

重点检查退出码、错误输出、命中项、地址/偏移和生成文件；将可复现结论记录到项目事实与 WP。

## 常见错误与限制

版本和参数可能随镜像升级变化，执行前先查看 `--help`。

## 关联条目

- 可通过 Memory 工具目录返回同级目录查看相关能力。

## 官方参考

- [https://github.com/david942j/one_gadget](https://github.com/david942j/one_gadget)
