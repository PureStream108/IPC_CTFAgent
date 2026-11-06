# GCC / G++ / Make / Musl / CMake

编译题目源码、利用程序和本地复现所需的原生辅助代码，并用 musl-gcc 生成静态二进制。

## 用途与适用场景

需要编译 C/C++ 题目、编写 pwntools 辅助程序、构建带调试符号的样本，或生成可移植静态 ELF 时使用。

## 版本检查

```bash
gcc --version | head -1 && g++ --version | head -1 && musl-gcc --version | head -1 && make --version | head -1
```

## 命令、导入与镜像路径

- 常用入口：`gcc / g++ / make / musl-gcc / cmake`
- 镜像路径：`/usr/bin/gcc、/usr/bin/g++、/usr/bin/make、/usr/bin/musl-gcc、/usr/bin/cmake`

## 常用工作流

先看题目是否自带 Makefile/CMakeLists；调试样本用 `-O0 -g -fno-pie -no-pie`，需要跨容器搬运时用 `musl-gcc -static`。编译产物放 /tmp 或 Member 专属目录，避免覆盖附件。

## 可执行示例

```bash
gcc -O0 -g -fno-pie -no-pie -o /tmp/sample sample.c && musl-gcc -static -O2 -o /tmp/sample-static sample.c && file /tmp/sample-static
```

## 输出解释

检查编译退出码、架构、是否 PIE、动态依赖和符号；`file`、`readelf -h`、`ldd` 可确认产物是否符合预期。

## 常见错误与限制

musl 与 glibc 行为存在差异，涉及线程、DNS 或 dlopen 的题目应优先用 glibc 编译。静态链接不等于免沙箱。

## 关联条目

- [objdump](/memory/catalog/tool_objdump/document)
- [gdb](/memory/catalog/tool_gdb/document)

## 官方参考

- [https://gcc.gnu.org/onlinedocs/](https://gcc.gnu.org/onlinedocs/)
