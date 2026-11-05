# checksec

检查 ELF 的 NX、PIE、RELRO、Stack Canary 和 Fortify 等缓解措施。

## 用途与适用场景

拿到 pwn 二进制后先判断利用面：是否 PIE、能否写 GOT、是否有 canary、栈是否可执行。

## 版本检查

```bash
checksec --file=/bin/true
```

## 命令、导入与镜像路径

- 常用入口：`checksec`
- 镜像路径：`/usr/bin/checksec`

## 常用工作流

先 `checksec --file=target`，再用 `readelf -lW` 与 `readelf -dW` 交叉确认 GNU_STACK、GNU_RELRO 和 BIND_NOW。pwntools 可用 `ELF.checksec()` 作为回退。

## 可执行示例

```bash
checksec --file=/tmp/challenge && readelf -lW /tmp/challenge | grep -E 'GNU_STACK|GNU_RELRO'
```

## 输出解释

关注 Arch、RELRO、Canary、NX、PIE、RPATH/RUNPATH；结合 libc 版本判断 ret2libc/ROP 可行性。

## 常见错误与限制

checksec 只反映静态标记，不能证明运行时一定启用；容器内核、ASLR 和 seccomp 仍需动态验证。

## 关联条目

- [objdump](/memory/catalog/tool_objdump/document)
- [gdb](/memory/catalog/tool_gdb/document)

## 官方参考

- [https://github.com/slimm609/checksec.sh](https://github.com/slimm609/checksec.sh)
