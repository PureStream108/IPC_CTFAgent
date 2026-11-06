# ncat

Nmap 项目维护的 netcat 实现，支持 SSL 与连接控制。

## 用途与适用场景

手工与未知服务对话、抓 banner、或者在本机起一个临时监听端口接收回调；比传统 nc 多了 --ssl 与更明确的超时控制。

## 版本检查

```bash
ncat --version
```

## 命令、导入与镜像路径

- 常用入口：`ncat`
- 镜像路径：`/usr/bin/ncat（Kali rolling 独立包）`

## 常用工作流

先把目标响应保存到文件，再用 --ssl/--sni 变体逐个排除；监听场景只在容器内网使用，避免暴露到宿主机。

## 可执行示例

```bash
printf 'HEAD / HTTP/1.0\r\n\r\n' | ncat --wait 3 TARGET 80
```

## 输出解释

原样打印对端字节流；没有输出通常意味着端口关闭、协议不对或被 --wait 提前截断。

## 常见错误与限制

纯 TCP/UDP，不解析应用层协议；--ssl 只做传输层封装，不验证证书链。

## 关联条目

- [netcat](/memory/catalog/core_netcat/document)
- [nmap](/memory/catalog/tool_nmap/document)
- [socat](/memory/catalog/tool_socat/document)

## 官方参考

- [https://nmap.org/ncat/](https://nmap.org/ncat/)
