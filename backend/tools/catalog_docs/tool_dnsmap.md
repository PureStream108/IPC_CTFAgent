# dnsmap

基于字典的子域名爆破工具。

## 用途与适用场景

在授权域范围内补齐子域清单，发现只在内网解析或指向测试环境的入口。

## 版本检查

```bash
dnsmap 2>&1 | head -5
```

## 命令、导入与镜像路径

- 常用入口：`dnsmap`
- 镜像路径：`/usr/bin/dnsmap（Kali rolling 包）`

## 常用工作流

先用内置字典，再用 -d 指定项目字典；用 -i 忽略通配符解析的误报，用 -r 指定结果文件。

## 可执行示例

```bash
dnsmap example.com -d /tmp/subs.txt -i -r /tmp/dnsmap.txt
```

## 输出解释

命中行给出子域、IP 与耗时；末尾统计“不可解析”数量。

## 常见错误与限制

只按字典穷举，发现不了字典外的名字；通配符 DNS 会产生海量假阳性，必须先加 -i。

## 关联条目

- [dig](/memory/catalog/tool_dig/document)
- [nmap](/memory/catalog/tool_nmap/document)

## 官方参考

- [https://github.com/laramies/dnsmap](https://github.com/laramies/dnsmap)
