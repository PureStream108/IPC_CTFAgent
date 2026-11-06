# dig

BIND 提供的 DNS 查询工具。

## 用途与适用场景

读取任意记录类型、指定解析器、尝试 AXFR，或用 +trace 观察委派路径；是 DNS 相关题目最快的取证入口。

## 版本检查

```bash
dig -v
```

## 命令、导入与镜像路径

- 常用入口：`dig`
- 镜像路径：`/usr/bin/dig（dnsutils 包）`

## 常用工作流

先查 A/AAAA 与 NS/MX，再针对可疑域尝试 TXT 与 AXFR；指定解析器用 @server，反向解析用 -x。

## 可执行示例

```bash
dig +short example.com A && dig @ns.example.com example.com AXFR +time=3 +tries=1
```

## 输出解释

标准格式分 HEADER/QUESTION/ANSWER 段；+short 只输出答案。status 字段（NOERROR/NXDOMAIN/REFUSED）比答案本身更有信息量。

## 常见错误与限制

AXFR 需要服务端允许区传送；递归解析器可能只返回缓存结果，需要显式指定权威服务器。

## 关联条目

- [dnsmap](/memory/catalog/tool_dnsmap/document)
- [nmap](/memory/catalog/tool_nmap/document)

## 官方参考

- [https://bind9.readthedocs.io/en/latest/manpages.html](https://bind9.readthedocs.io/en/latest/manpages.html)
