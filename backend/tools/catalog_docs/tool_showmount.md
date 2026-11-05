# showmount

列出 NFS 服务器导出的目录。

## 用途与适用场景

确认 NFS 导出路径与允许的客户端范围；导出目录常常直接暴露备份、源码或 home 目录。

## 版本检查

```bash
showmount --help | head -20
```

## 命令、导入与镜像路径

- 常用入口：`showmount`
- 镜像路径：`/usr/sbin/showmount（nfs-common 包）`

## 常用工作流

先用 showmount -e 取导出列表，再在容器内以只读方式挂载：mount -t nfs -o ro,nolock TARGET:/export /mnt。

## 可执行示例

```bash
showmount -e TARGET
```

## 输出解释

Export list for TARGET 段下列出路径与允许的客户端网段；没有输出表示 rpc.mountd 不可达。

## 常见错误与限制

需要目标允许 mountd 查询；挂载需要容器具备 CAP_SYS_ADMIN，多数任务容器只有在特权模式下才能 mount。

## 关联条目

- [nmap](/memory/catalog/tool_nmap/document)

## 官方参考

- [https://linux.die.net/man/8/showmount](https://linux.die.net/man/8/showmount)
