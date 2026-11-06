# elfutils

eu-readelf、eu-objdump、eu-nm、eu-stack 等 ELF/DWARF 工具。

## 用途与适用场景

binutils 对畸形 ELF、压缩节或 DWARF 解析失败时，用 elfutils 交叉验证节表、符号、注释和调用栈。

## 版本检查

```bash
eu-readelf --version | head -1
```

## 命令、导入与镜像路径

- 常用入口：`eu-readelf / eu-objdump / eu-nm / eu-stack`
- 镜像路径：`/usr/bin/eu-readelf、/usr/bin/eu-objdump、/usr/bin/eu-nm、/usr/bin/eu-stack`

## 常用工作流

先 `eu-readelf -h -S -s file`；需要 DWARF 时加 `--debug-dump=info`；运行中进程用 `eu-stack -p PID` 取样。把结果与 readelf 对照。

## 可执行示例

```bash
eu-readelf -h -S -s /tmp/challenge | head -120 && eu-nm -a /tmp/challenge | head -40
```

## 输出解释

输出节索引、类型、偏移、大小、符号表和 DWARF 信息；关注异常节名、重叠地址、缺失节头和压缩标志。

## 常见错误与限制

elfutils 与 binutils 的选项/输出格式不同；损坏严重的文件可能两者都拒绝。不要对运行中的生产进程随意 attach。

## 关联条目

- [objdump](/memory/catalog/tool_objdump/document)

## 官方参考

- [https://sourceware.org/elfutils/](https://sourceware.org/elfutils/)
