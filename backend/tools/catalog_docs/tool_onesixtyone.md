# onesixtyone

批量尝试 SNMP community string 的快速扫描器。

## 用途与适用场景

SNMP 常用的 public/private 之外，很多设备使用自定义社区名；先找到社区名，才能用 snmpwalk 取数据。

## 版本检查

```bash
onesixtyone -h 2>&1 | head -20
```

## 命令、导入与镜像路径

- 常用入口：`onesixtyone`
- 镜像路径：`/usr/bin/onesixtyone（Kali rolling 包）`

## 常用工作流

用小字典（不要用百万级字典）扫单个目标，确认后再扩大到授权网段；命中后立即改用 snmpwalk 取证，不要继续猜。

## 可执行示例

```bash
onesixtyone -c /workspace/wordlists/SecLists/Usernames/top-usernames-shortlist.txt -i /tmp/targets.txt
```

## 输出解释

每行给出 IP、hostname 与命中的 community string；没有输出表示所有尝试都被丢弃（UDP 无响应）。

## 常见错误与限制

UDP 无响应无法区分“端口关闭”“被过滤”“社区名错误”；高并发会被网络监控发现。

## 关联条目

- [snmpwalk](/memory/catalog/tool_snmpwalk/document)
- [nmap](/memory/catalog/tool_nmap/document)

## 官方参考

- [https://github.com/trailofbits/onesixtyone](https://github.com/trailofbits/onesixtyone)
