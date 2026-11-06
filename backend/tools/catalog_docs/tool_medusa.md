# medusa

模块化的并行登录审计工具。

## 用途与适用场景

当 hydra 没有覆盖所需协议，或用 -M 指定多目标批量审计时使用。

## 版本检查

```bash
medusa -h 2>&1 | head -20
```

## 命令、导入与镜像路径

- 常用入口：`medusa`
- 镜像路径：`/usr/bin/medusa（Kali rolling 包）`

## 常用工作流

用 -M 指定主机文件、-U/-P 指定账号与口令文件；-t 控制并发、-n 控制端口，先小规模验证再放大。

## 可执行示例

```bash
medusa -h TARGET -u admin -P /tmp/passwords.txt -M ssh -t 4 -f
```

## 输出解释

命中时打印 ACCOUNT FOUND 与 [协议] Host/User/Password；-f 表示命中即停。

## 常见错误与限制

与 hydra 相同：只对授权账号使用。模块间参数不完全一致，跨协议脚本要先读 -h 输出。

## 关联条目

- [hydra](/memory/catalog/tool_hydra/document)
- [ncrack](/memory/catalog/tool_ncrack/document)

## 官方参考

- [https://github.com/jmk-foofus/medusa](https://github.com/jmk-foofus/medusa)
