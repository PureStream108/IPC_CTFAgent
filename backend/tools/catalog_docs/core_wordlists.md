# 常用字典

预装 rockyou 与 SecLists 精选目录，供口令、目录和用户名爆破使用。

## 用途与适用场景

需要字典攻击 Web 目录、弱口令、Flask secret 或用户名枚举时使用；先小字典验证流程，再扩大。

## 版本检查

```bash
test -s /workspace/wordlists/rockyou.txt && wc -l /workspace/wordlists/SecLists/Discovery/Web-Content/common.txt
```

## 命令、导入与镜像路径

- 常用入口：`ls /workspace/wordlists`
- 镜像路径：`/opt/ipc-tools/wordlists、/workspace/wordlists`

## 常用工作流

常用路径：rockyou.txt、SecLists/Discovery/Web-Content/common.txt、raft-medium-directories.txt、Passwords/Common-Credentials/xato-net-10-million-passwords-100000.txt、Usernames/top-usernames-shortlist.txt。先 common.txt/小口令表，再按需换大表。

## 可执行示例

```bash
test -s /workspace/wordlists/rockyou.txt && wc -l /workspace/wordlists/SecLists/Discovery/Web-Content/common.txt /workspace/wordlists/rockyou.txt
```

## 输出解释

确认文件非空和行数；爆破工具通常输出命中项、状态码或有效凭据。记录使用的字典路径和过滤规则，便于复现。

## 常见错误与限制

完整 SecLists 未安装，只有精选目录；rockyou 很大，可能触发账号锁定或超时。仅对授权目标使用。

## 关联条目

- [hydra](/memory/catalog/tool_hydra/document)
- [john](/memory/catalog/tool_john/document)

## 官方参考

- [https://github.com/danielmiessler/SecLists](https://github.com/danielmiessler/SecLists)
