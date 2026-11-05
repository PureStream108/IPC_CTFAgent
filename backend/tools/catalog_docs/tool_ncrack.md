# ncrack

Nmap 项目的网络认证破解器。

## 用途与适用场景

在已有主机与端口清单时做批量凭据审计，支持把多个服务写在同一条命令里。

## 版本检查

```bash
ncrack -h 2>&1 | head -20
```

## 命令、导入与镜像路径

- 常用入口：`ncrack`
- 镜像路径：`/usr/bin/ncrack（Kali rolling 包）`

## 常用工作流

用 -U/-P 提供字典，配合 -T 2 与 --connection-limit 1 降低对目标的压力；先用单个用户验证响应再扩展。

## 可执行示例

```bash
ncrack -U /tmp/users.txt -P /tmp/passwords.txt -T 2 --connection-limit 1 ssh://TARGET:22
```

## 输出解释

命中时打印 Discovered credentials 与协议、地址、用户名、口令；进度行给出当前尝试速率。

## 常见错误与限制

高 -T 级别会触发防护与账号锁定；只用于任务授权的服务，禁止指向评测平台或互联网未知主机。

## 关联条目

- [hydra](/memory/catalog/tool_hydra/document)
- [medusa](/memory/catalog/tool_medusa/document)

## 官方参考

- [https://nmap.org/ncrack/](https://nmap.org/ncrack/)
