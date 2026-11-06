# hashcat

支持大量哈希模式的口令破解工具。

## 用途与适用场景

与 john 相比模式更多、规则更强；在没有 GPU 的容器里用 CPU OpenCL 处理中小规模字典。

## 版本检查

```bash
hashcat --version
```

## 命令、导入与镜像路径

- 常用入口：`hashcat`
- 镜像路径：`/usr/bin/hashcat（Kali rolling 包）`

## 常用工作流

先用 -m 指定哈希模式、-a 指定攻击模式，再用 --show 复看结果；CPU 环境加 --force 与 -D 1，并降低 -w。

## 可执行示例

```bash
hashcat -m 0 -a 0 -w 1 --force -D 1 -o /tmp/cracked.txt /tmp/hashes.txt /workspace/wordlists/rockyou.txt
```

## 输出解释

状态行给出 Speed、Progress 与 Recovered；结果写入 -o 文件或 --show 输出。

## 常见错误与限制

容器没有 GPU，CPU OpenCL 速度有限，大字典会很慢；--force 会绕过自检，可能得到错误结果，只在 CPU 模式下使用。

## 关联条目

- [john](/memory/catalog/tool_john/document)
- [字典与 rockyou](/memory/catalog/core_wordlists/document)

## 官方参考

- [https://hashcat.net/wiki/](https://hashcat.net/wiki/)
