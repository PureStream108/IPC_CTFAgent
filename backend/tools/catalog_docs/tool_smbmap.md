# smbmap

列出 SMB 共享及当前账号的读写权限。

## 用途与适用场景

确认某个共享是否可读、是否可写，以及能否用已有凭据横向访问；比手工 smbclient 更快给出权限矩阵。

## 版本检查

```bash
smbmap -h 2>&1 | head -20
```

## 命令、导入与镜像路径

- 常用入口：`smbmap`
- 镜像路径：`/usr/bin/smbmap（Kali rolling 包）`

## 常用工作流

先用 -u 与 -p 指定已获得的凭据（或 -u '' -p '' 试匿名），再对目标目录加 -r 递归列目录。

## 可执行示例

```bash
smbmap -H TARGET -u guest -p '' -R --depth 2
```

## 输出解释

以表格给出 Disk、Permissions、Comment 与递归目录树；READ ONLY 表示只读，WRITE 表示可写。

## 常见错误与限制

写权限是可被审计的破坏性动作，测试时不要真的上传文件；SMB1 与现代签名策略会让结果与 enum4linux 不一致。

## 关联条目

- [enum4linux](/memory/catalog/tool_enum4linux/document)
- [nmap](/memory/catalog/tool_nmap/document)

## 官方参考

- [https://github.com/ShawnDEvans/smbmap](https://github.com/ShawnDEvans/smbmap)
