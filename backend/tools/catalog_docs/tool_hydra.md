# hydra

并行网络登录口令审计工具。

## 用途与适用场景

在明确的授权范围内验证弱口令假设，覆盖 ssh、ftp、http-get/post、smb、rdp 等协议。

## 版本检查

```bash
hydra -h 2>&1 | head -20
```

## 命令、导入与镜像路径

- 常用入口：`hydra`
- 镜像路径：`/usr/bin/hydra（Kali rolling 包）`

## 常用工作流

先用单个用户名加小字典验证目标是否限速，再决定是否扩大；始终显式设置 -t 与 -W。

## 可执行示例

```bash
hydra -l admin -P /workspace/wordlists/SecLists/Passwords/Common-Credentials/xato-net-10-million-passwords-100000.txt -t 4 -W 3 -f ssh://TARGET
```

## 输出解释

命中时打印 [port][ssh] host: TARGET login: admin password: xxx；其余尝试只计数。

## 常见错误与限制

只用于任务明确授权的账号；并发过高会锁定账号或触发封禁，一次锁死等于审计失败。若目标是评测平台的一部分，禁止使用。

## 关联条目

- [medusa](/memory/catalog/tool_medusa/document)
- [ncrack](/memory/catalog/tool_ncrack/document)
- [sshpass](/memory/catalog/tool_sshpass/document)

## 官方参考

- [https://github.com/vanhauser-thc/thc-hydra](https://github.com/vanhauser-thc/thc-hydra)
