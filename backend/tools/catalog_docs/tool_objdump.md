# objdump

使用 Binutils 的 objdump、readelf、nm 和 strip 检查 ELF 头、符号、段、重定位与反汇编。

## 用途与适用场景

先用 readelf/file 确认架构和段，再用 nm/objdump 定位符号与代码；除副本外不要对题目原件执行 strip。

## 版本检查

```bash
objdump --version
```

## 命令、导入与镜像路径

- 常用入口：`objdump`
- 镜像路径：`/usr/bin/objdump、/usr/bin/readelf、/usr/bin/nm、/usr/bin/strip`

## 常用工作流

1. 在 Member 工作目录中确认附件、目标地址和授权范围。
2. 按题目类型使用 `objdump` 进行最小化探测。
3. 保存原始输出，再根据结果逐步增加参数，避免一开始执行破坏性操作。

## 可执行示例

```bash
readelf -hW challenge && nm -an challenge | head && objdump -d -M intel challenge | head -80
```

## 输出解释

重点检查退出码、错误输出、命中项、地址/偏移和生成文件；将可复现结论记录到项目事实与 WP。

## 常见错误与限制

版本和参数可能随镜像升级变化，执行前先查看 `--help`。

## 关联条目

- 可通过 Memory 工具目录返回同级目录查看相关能力。

## 官方参考

- [https://sourceware.org/binutils/docs/](https://sourceware.org/binutils/docs/)
