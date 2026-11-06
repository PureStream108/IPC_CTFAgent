# Scapy / dpkt

构造、解析和重放网络数据包，读取 pcap 并做协议层分析。

## 用途与适用场景

需要按协议分层过滤 pcap、提取字段、构造畸形包或在容器 lo 上重放流量时使用。

## 版本检查

```bash
python3 -c "import scapy, dpkt; print(scapy.__version__)"
```

## 命令、导入与镜像路径

- 常用入口：`python3`
- 镜像路径：`Python site-packages（scapy、dpkt）`

## 常用工作流

用 `rdpcap` 读取后按 `IP/TCP/HTTP/DNS` 分层过滤；需要重放时构造包并通过 `send`/`sendp` 发到容器接口。先用 tshark 建立协议基线。

## 可执行示例

```bash
python3 -c "from scapy.all import rdpcap, IP, TCP; pkts=rdpcap('/tmp/capture.pcap'); print([(p[IP].src,p[IP].dst,p[TCP].dport) for p in pkts if IP in p and TCP in p][:5])"
```

## 输出解释

包对象可直接访问各层字段；dpkt 更适合流式解析。关注源/目的、端口、payload 和异常标志位。

## 常见错误与限制

发送原始包需要 root/CAP_NET_RAW；容器网络命名空间可能限制外发。重放前确认授权，避免扫描外部目标。

## 关联条目

- [tshark](/memory/catalog/tool_tshark/document)

## 官方参考

- [https://scapy.readthedocs.io/](https://scapy.readthedocs.io/)
