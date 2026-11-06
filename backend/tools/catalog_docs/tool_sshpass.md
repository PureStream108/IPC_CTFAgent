# sshpass

为非交互 SSH 登录提供口令。

## 用途与适用场景

用已知口令脚本化一次授权登录，或用 -f 从文件读取口令做单次验证。

## 版本检查

```bash
sshpass -V
```

## 命令、导入与镜像路径

- 常用入口：`sshpass`
- 镜像路径：`/usr/bin/sshpass（Kali rolling 包）`

## 常用工作流

只做单次已验证口令的登录；批量猜测交给 hydra/medusa，那里才有并发与限速控制。

## 可执行示例

```bash
sshpass -p 'PASSWORD' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 user@TARGET 'id'
```

## 输出解释

透传远端命令输出；认证失败时打印 Permission denied (publickey,password)。

## 常见错误与限制

口令会出现在进程参数里，共享主机上会被 ps 看到；不要把它塞进循环做爆破。

## 关联条目

- [hydra](/memory/catalog/tool_hydra/document)
- [medusa](/memory/catalog/tool_medusa/document)

## 官方参考

- [https://sourceforge.net/projects/sshpass/](https://sourceforge.net/projects/sshpass/)
