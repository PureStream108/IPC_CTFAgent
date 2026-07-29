# Bash / Shell

用于组合命令、处理文件、编写自动化探测与复现脚本。

## 用途与适用场景

使用 set -euo pipefail、明确引用变量，并把复杂流程保存为可复现脚本。

## 版本检查

```bash
bash --version
```

## 命令、导入与镜像路径

- 常用入口：`bash`
- 镜像路径：`/bin/bash`

## 常用工作流

1. 在 Member 工作目录中确认附件、目标地址和授权范围。
2. 按题目类型使用 `bash` 进行最小化探测。
3. 保存原始输出，再根据结果逐步增加参数，避免一开始执行破坏性操作。

## 可执行示例

```bash
bash -lc 'file ./challenge && strings ./challenge | head'
```

## 输出解释

重点检查退出码、错误输出、命中项、地址/偏移和生成文件；将可复现结论记录到项目事实与 WP。

## 常见错误与限制

版本和参数可能随镜像升级变化，执行前先查看 `--help`。

## 关联条目

- 可通过 Memory 工具目录返回同级目录查看相关能力。

## 官方参考

- [https://www.gnu.org/software/bash/manual/](https://www.gnu.org/software/bash/manual/)
