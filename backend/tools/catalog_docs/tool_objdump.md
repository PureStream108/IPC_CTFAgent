# objdump

使用 Binutils 的 objdump、readelf、nm 和 strip 检查 ELF 头、符号、段、重定位与反汇编。

## 用途与适用场景

需要在打开 r2/gdb 之前确认架构、PIE、节表、符号和重定位，或快速反汇编指定函数时使用。

## 版本检查

```bash
objdump --version | head -1 && readelf --version | head -1 && nm --version | head -1
```

## 命令、导入与镜像路径

- 常用入口：`objdump / readelf / nm / strip`
- 镜像路径：`/usr/bin/objdump、/usr/bin/readelf、/usr/bin/nm、/usr/bin/strip`

## 常用工作流

先 `readelf -hW`、`-lW`、`-SW` 看架构和段；`nm -an` 找符号；`objdump -d -M intel` 反汇编。strip 只对副本执行。

## 可执行示例

```bash
readelf -hW /tmp/challenge && nm -an /tmp/challenge | head -40 && objdump -d -M intel --disassemble=main /tmp/challenge | head -80
```

## 输出解释

确认 ELF 类型、入口、PIE、NX、动态依赖、符号地址和重定位；反汇编重点看比较、跳转和调用参数。

## 常见错误与限制

strip 后符号缺失，需要用入口/字符串/xrefs 定位；objdump 不恢复高级语义，复杂逻辑配合 r2 和 gdb。

## 关联条目

- [elfutils](/memory/catalog/tool_elfutils/document)
- [radare2](/memory/catalog/tool_radare2/document)
- [checksec](/memory/catalog/tool_checksec/document)

## 官方参考

- [https://sourceware.org/binutils/docs/binutils/](https://sourceware.org/binutils/docs/binutils/)
