# xxd

十六进制查看、偏移定位和 hex 反转。

## 用途与适用场景

需要检查魔数、文件头、协议字段、补丁字节，或把编辑后的 hex 还原为二进制时使用。

## 版本检查

```bash
xxd -v
```

## 命令、导入与镜像路径

- 常用入口：`xxd`
- 镜像路径：`/usr/bin/xxd`

## 常用工作流

用 `-l` 限制长度、`-s` 跳转偏移、`-g 1` 按字节分组；修改时先 `xxd file > file.hex`，编辑后用 `xxd -r` 还原到副本并校验哈希。

## 可执行示例

```bash
xxd -l 128 -g 1 /tmp/challenge && xxd -s 0x40 -l 64 -g 1 /tmp/challenge
```

## 输出解释

左侧是偏移，中间是 hex，右侧是 ASCII。注意大小端、对齐和不可打印字节；修改后重新运行 file/checksec 验证结构。

## 常见错误与限制

`xxd -r` 不会自动修复校验和或长度字段；对题目原件直接修改可能破坏签名。优先复制后操作。

## 关联条目

- [file / xxd / hexdump](/memory/catalog/core_file_triage/document)
- [file](/memory/catalog/tool_file/document)

## 官方参考

- [https://linux.die.net/man/1/xxd](https://linux.die.net/man/1/xxd)
