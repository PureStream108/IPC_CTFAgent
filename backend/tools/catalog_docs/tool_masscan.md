# masscan

异步 TCP 端口扫描器，适合大范围的浅层探测。

## 用途与适用场景

在授权的网段上快速找出开放端口，再交给 nmap -sV 做服务识别；比 nmap 全端口扫描快一个数量级。

## 版本检查

```bash
masscan --version 2>&1 | head -2
```

> `masscan --version` 打印版本后会以退出码 1 结束（上游行为），因此验收按“存在 + 可启动”判定，不看退出码。

## 命令、导入与镜像路径

- 常用入口：`masscan`
- 镜像路径：`/usr/bin/masscan（Kali rolling 包）`

## 常用工作流

先用 --rate 1000 与明确的 -p 列表扫目标，确认可用后逐步提速；结果以 -oJ 保存，再交给 nmap 做服务探测。

## 可执行示例

```bash
masscan -p80,443,8080 --rate 1000 --wait 3 TARGET -oJ /tmp/masscan.json
```

## 输出解释

标准输出逐行打印 Discovered open port；-oJ 生成 JSON 数组，包含 ip、port 与时间戳。

## 常见错误与限制

必须显式指定 -p 与 --rate，否则行为不符合预期；高速扫描会触发 IDS/防火墙并可能压垮上游链路，只用于授权范围。

## 关联条目

- [nmap](/memory/catalog/tool_nmap/document)
- [ncat](/memory/catalog/tool_ncat/document)

## 官方参考

- [https://github.com/robertdavidgraham/masscan](https://github.com/robertdavidgraham/masscan)
