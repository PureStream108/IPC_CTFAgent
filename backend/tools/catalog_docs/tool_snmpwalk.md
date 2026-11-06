# snmpwalk

遍历设备的 SNMP OID 树。

## 用途与适用场景

拿到 community string 后读取接口、进程、系统描述与已安装软件，是网络设备与嵌入式目标的常见信息源。

## 版本检查

```bash
snmpwalk -V
```

## 命令、导入与镜像路径

- 常用入口：`snmpwalk`
- 镜像路径：`/usr/bin/snmpwalk（snmp 包）`

## 常用工作流

先 snmpwalk -v2c -c 查 system 子树，再按需扩展到 interfaces 与 hrSWRun；设置 -t 与 -r 避免长时间等待。

## 可执行示例

```bash
snmpwalk -v2c -c public -t 2 -r 1 TARGET 1.3.6.1.2.1.1
```

## 输出解释

每个 OID 一行，形如 OID::MIB = TYPE: value；Timeout 表示 community 错误或被过滤。

## 常见错误与限制

只读；写操作（snmpget/snmpset）不在本工具范围。UDP 丢包会让输出不完整，重要结论需要重跑确认。

## 关联条目

- [onesixtyone](/memory/catalog/tool_onesixtyone/document)
- [nmap](/memory/catalog/tool_nmap/document)

## 官方参考

- [https://www.net-snmp.org/docs/man/snmpwalk.html](https://www.net-snmp.org/docs/man/snmpwalk.html)
