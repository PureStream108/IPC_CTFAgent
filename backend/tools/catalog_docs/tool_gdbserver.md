# gdbserver

在本地或容器内启动远程调试 stub，并用 gdb 连接。

## 用途与适用场景

目标程序需要独立进程运行、gdb 直接启动不便，或需要从另一个终端/脚本附加调试时使用。

## 版本检查

```bash
gdbserver --version | head -1
```

## 命令、导入与镜像路径

- 常用入口：`gdbserver`
- 镜像路径：`/usr/bin/gdbserver`

## 常用工作流

用 `gdbserver 127.0.0.1:1234 ./target` 启动，再在另一进程执行 `gdb -q ./target -ex 'target remote 127.0.0.1:1234'`；调试结束关闭监听，避免端口残留。

## 可执行示例

```bash
gdbserver 127.0.0.1:1234 /tmp/challenge & sleep 1; gdb -q --batch -ex 'file /tmp/challenge' -ex 'target remote 127.0.0.1:1234' -ex 'info registers' -ex detach
```

## 输出解释

gdbserver 输出监听地址和 pid；gdb 连接后显示停止位置、寄存器和内存。远程连接失败先检查端口、进程和防火墙。

## 常见错误与限制

gdbserver 需要目标可执行且架构匹配；容器内 localhost 端口只在容器命名空间有效。不要暴露到公网。

## 关联条目

- [gdb](/memory/catalog/tool_gdb/document)
- [socat](/memory/catalog/tool_socat/document)

## 官方参考

- [https://sourceware.org/gdb/current/onlinedocs/gdb.html/Server.html](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Server.html)
