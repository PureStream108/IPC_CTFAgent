# sslscan

检查 TLS 协议版本、密码套件与证书信息。

## 用途与适用场景

确认目标是否仍允许过时协议或弱套件；证书里的 SAN/CN 也可以作为 vhost 与子域线索。

## 版本检查

```bash
sslscan --version
```

## 命令、导入与镜像路径

- 常用入口：`sslscan`
- 镜像路径：`/usr/bin/sslscan（Kali rolling 包）`

## 常用工作流

先跑默认全量检测，再用 --no-ciphersuites 只看证书与协议；配合 --sni-name 指定虚拟主机。

## 可执行示例

```bash
sslscan --no-colour --sni-name TARGET TARGET:443 2>/dev/null | head -40
```

## 输出解释

按 TLS 版本分组列出 Accepted/Preferred 套件；证书段给出 Subject、Alternate names、有效期与公钥信息。

## 常见错误与限制

只做被动握手，不验证证书链是否被客户端信任；对 SNI 依赖的站点必须显式指定 --sni-name。

## 关联条目

- [nmap](/memory/catalog/tool_nmap/document)
- [openssl](/memory/catalog/tool_openssl/document)

## 官方参考

- [https://github.com/rbsec/sslscan](https://github.com/rbsec/sslscan)
