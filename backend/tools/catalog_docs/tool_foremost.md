# foremost

按文件头尾签名从磁盘镜像或二进制 blob 中 carving 文件。

## 用途与适用场景

需要从镜像、内存转储或嵌套 blob 中恢复图片、压缩包、PDF、Office 等文件，而 binwalk 未覆盖时使用。

## 版本检查

```bash
foremost -V
```

## 命令、导入与镜像路径

- 常用入口：`foremost`
- 镜像路径：`/usr/bin/foremost`

## 常用工作流

用 `-i` 指定输入、`-o` 指定空目录、`-T` 加时间戳；先看 `audit.txt`，再检查按类型分目录的结果。输出目录必须为空或新建。

## 可执行示例

```bash
mkdir -p /tmp/carve && foremost -i /tmp/disk.img -o /tmp/carve -T && find /tmp/carve -type f | head
```

## 输出解释

audit.txt 记录扫描的文件和签名；输出目录按扩展名分组。用 file/strings/exiftool 验证恢复物是否完整。

## 常见错误与限制

carving 依赖头尾签名，碎片化或加密文件可能恢复失败；输出可能包含大量误报。不要对正在写入的镜像操作。

## 关联条目

- [binwalk](/memory/catalog/tool_binwalk/document)
- [file](/memory/catalog/tool_file/document)

## 官方参考

- [https://foremost.sourceforge.net/](https://foremost.sourceforge.net/)
