# gdb

GNU debugger，用于断点、寄存器、内存、调用栈和 core 文件分析。

## 用途与适用场景

需要确认运行时控制流、检查参数/返回值、定位崩溃地址，或验证 pwntools 利用假设时使用。

## 版本检查

```bash
gdb --version | head -1
```

## 命令、导入与镜像路径

- 常用入口：`gdb`
- 镜像路径：`/usr/bin/gdb`

## 常用工作流

先用 `gdb --batch -ex 'file target' -ex 'info functions'` 做无交互检查；需要断点时用 `-ex 'b *0x...' -ex run -ex 'info registers'`。core 文件用 `gdb target core` 查看 bt、frame 和 x/i。

## 可执行示例

```bash
gdb -q --batch -ex 'file /tmp/challenge' -ex 'b main' -ex run -ex 'info registers' -ex 'x/8i $pc'
```

## 输出解释

关注停止位置、RIP/RSI/RDI、栈内容和 backtrace；崩溃题对照 faulting address 与输入偏移。

## 常见错误与限制

容器内 ptrace 可能受 seccomp 限制；必要时用 gdbserver 或 strace。调试不可信程序时限制网络和文件权限。

## 关联条目

- [gdbserver](/memory/catalog/tool_gdbserver/document)
- [checksec](/memory/catalog/tool_checksec/document)

## 官方参考

- [https://sourceware.org/gdb/documentation/](https://sourceware.org/gdb/documentation/)
