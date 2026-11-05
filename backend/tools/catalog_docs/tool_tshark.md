# tshark

用 tshark 解析 pcap，用 tcpdump 做轻量抓包和流量过滤。

## 用途与适用场景

题目给出 pcap、需要分析协议会话，或在容器内复现网络交互并抓包时使用。

## 版本检查

```bash
tshark --version | head -1 && tcpdump --version | head -1
```

## 命令、导入与镜像路径

- 常用入口：`tshark / tcpdump`
- 镜像路径：`/usr/bin/tshark、/usr/bin/tcpdump`

## 常用工作流

有 pcap 时先 `tshark -r file -q -z io,phs` 看协议层级，再用 `-Y` 过滤 HTTP/DNS/TLS 字段；需要现场抓包时用 `tcpdump -i lo -w /tmp/x.pcap`，复现后 Ctrl-C 并用 tshark 读取。

## 可执行示例

```bash
tshark -r /tmp/capture.pcap -Y 'http.request' -T fields -e frame.number -e http.host -e http.request.uri | head -30
```

## 输出解释

关注帧号、源/目的地址、协议、HTTP Host/URI、DNS 查询和 TLS SNI。必要时用 `-x` 查看 hex，或导出对象。

## 常见错误与限制

容器内抓包需要 root/CAP_NET_RAW；`-i any` 在部分环境不可用，优先 `lo` 或 `eth0`。pcapng 与 pcap 均支持。

## 关联条目

- [Scapy / dpkt](/memory/catalog/lib_scapy/document)
- [binwalk](/memory/catalog/tool_binwalk/document)

## 官方参考

- [https://www.wireshark.org/docs/man-pages/tshark.html](https://www.wireshark.org/docs/man-pages/tshark.html)
