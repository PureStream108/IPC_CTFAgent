# enum4linux

从 Windows/Samba 主机枚举 SMB 与 NetBIOS 信息。

## 用途与适用场景

SMB 场景的第一轮信息收集：用户列表、共享列表、密码策略、操作系统版本、工作组。

## 版本检查

```bash
enum4linux -h 2>&1 | head -20
```

## 命令、导入与镜像路径

- 常用入口：`enum4linux`
- 镜像路径：`/usr/local/bin/enum4linux`，实际执行 `/opt/ipc-tools/tools/enum4linux/enum4linux.pl`
- 上游是单个 Perl 脚本，jammy 没有对应的 deb 包，因此从固定分支源码安装，并以包装脚本暴露 CLI。

## 常用工作流

先跑 -a 全量，再针对有价值的段单独执行（-U 用户、-S 共享、-P 密码策略）；拿到账号后用 smbmap 验证共享权限。

## 可执行示例

```bash
enum4linux -a TARGET
```

## 输出解释

以分区标题输出（Domain Info、Share Enumeration、Users …）；大量 “unknown username or bad password” 说明匿名会话被拒。

## 常见错误与限制

依赖匿名或已知凭据的 SMB 会话；现代域环境常关闭匿名枚举，空结果不代表没有共享。脚本会调用 `rpcclient`/`smbclient`（镜像已装 perl 与 smbclient），缺少它们时只会退化成“没有共享”的假象；`-P` 还依赖 `polenum`，本镜像未安装，密码策略段会是空的。

## 关联条目

- [smbmap](/memory/catalog/tool_smbmap/document)
- [nmap](/memory/catalog/tool_nmap/document)
- [ldapsearch](/memory/catalog/tool_ldapsearch/document)

## 官方参考

- [https://github.com/CiscoCXSecurity/enum4linux](https://github.com/CiscoCXSecurity/enum4linux)
