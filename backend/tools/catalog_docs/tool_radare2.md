# radare2

Radare2 CLI 用于反汇编、函数发现、字符串、交叉引用和二进制补丁。

## 用途与适用场景

需要快速静态分析未知 ELF/PE/Mach-O、定位 main/校验函数、查看字符串和交叉引用，或在 gdb 前建立假设时使用。

## 版本检查

```bash
r2 -v
```

## 命令、导入与镜像路径

- 常用入口：`r2`
- 镜像路径：`/usr/local/bin/r2`

## 常用工作流

先 `r2 -q -c 'aaa; afl' target` 获取函数，再用 `pdf @ main`、`izz`、`axt @ address` 深入；需要脚本化时用 r2pipe。修改二进制前复制到 /tmp。

## 可执行示例

```bash
r2 -q -c 'aaa; afl; pdf @ main; izz' /tmp/challenge
```

## 输出解释

afl 输出函数地址、大小和名称；pdf 显示反汇编与基本块；izz 显示字符串地址和内容。结合 xrefs 判断关键分支。

## 常见错误与限制

r2 的自动分析不是反编译器；复杂 C++ 或混淆样本需要结合 gdb、objdump 和动态跟踪。不要对原件使用 `-w`。

## 关联条目

- [objdump](/memory/catalog/tool_objdump/document)
- [gdb](/memory/catalog/tool_gdb/document)
- [r2pipe](/memory/catalog/lib_r2pipe/document)

## 官方参考

- [https://radare.org/n/radare2.html](https://radare.org/n/radare2.html)
