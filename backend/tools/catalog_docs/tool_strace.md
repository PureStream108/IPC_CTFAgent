# strace

跟踪进程的系统调用、信号、文件与网络行为。

## 用途与适用场景

二进制没有源码、需要观察它打开哪些文件、连接哪些地址、执行哪些程序，或定位权限/沙箱失败时使用。

## 版本检查

```bash
strace -V
```

## 命令、导入与镜像路径

- 常用入口：`strace`
- 镜像路径：`/usr/bin/strace`

## 常用工作流

用 `-f` 跟随子进程，用 `-e trace=file,network,process` 缩小范围，用 `-o` 写文件。先跑正常输入建立基线，再改变输入观察差异。

## 可执行示例

```bash
strace -f -e trace=execve,openat,connect -o /tmp/trace.log /tmp/challenge; tail -80 /tmp/trace.log
```

## 输出解释

每行是 syscall(参数) = 返回值；重点看 ENOENT/EACCES、connect 地址、execve 参数和信号。返回值 -1 表示失败。

## 常见错误与限制

ptrace 可能被 seccomp 或容器安全策略阻止；静态链接/自修改程序的行为仍需 gdb。strace 会显著拖慢程序。

## 关联条目

- [gdb](/memory/catalog/tool_gdb/document)
- [gdbserver](/memory/catalog/tool_gdbserver/document)

## 官方参考

- [https://strace.io/](https://strace.io/)
