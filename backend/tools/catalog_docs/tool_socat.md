# socat

通用 TCP/UDP/UNIX socket 与进程端点转发工具。

## 用途与适用场景

需要把容器内服务暴露到本地端口、把本地端口转发到远程目标、或在 pwn 题中构造双向管道时使用。

## 版本检查

```bash
socat -V | head -3
```

## 命令、导入与镜像路径

- 常用入口：`socat`
- 镜像路径：`/usr/bin/socat`

## 常用工作流

监听用 `TCP-LISTEN:PORT,reuseaddr,fork`，转发用 `TCP:host:port`，进程端点用 `EXEC:./target,pty,stderr`。测试结束 kill 监听进程。

## 可执行示例

```bash
socat TCP-LISTEN:9000,reuseaddr,fork TCP:127.0.0.1:8000 & sleep 1; curl -s http://127.0.0.1:9000/ | head
```

## 输出解释

socat 默认把连接日志写到 stderr；确认监听端口、转发目标和进程退出码。连接拒绝通常是目标端口未启动或网络命名空间不同。

## 常见错误与限制

容器 localhost 与宿主机不是同一命名空间；需要跨容器时使用共享网络或显式端口映射。不要暴露未授权服务到公网。

## 关联条目

- [netcat](/memory/catalog/core_netcat/document)
- [gdbserver](/memory/catalog/tool_gdbserver/document)

## 官方参考

- [http://www.dest-unreach.org/socat/doc/socat.html](http://www.dest-unreach.org/socat/doc/socat.html)
