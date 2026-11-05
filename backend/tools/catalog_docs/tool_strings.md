# strings

使用 GNU strings 从二进制和 blob 中提取可打印字符串。

## 用途与适用场景

需要快速查找 flag、路径、URL、错误信息、格式字符串或硬编码密钥时使用。

## 版本检查

```bash
strings --version | head -1
```

## 命令、导入与镜像路径

- 常用入口：`strings`
- 镜像路径：`/usr/bin/strings`

## 常用工作流

先默认扫描，再用 `-n 6`、`-t x` 调整长度和偏移；对宽字符用 `-el`。把偏移带回 xxd/r2 定位引用。

## 可执行示例

```bash
strings -a -n 6 -t x /tmp/challenge | head -120
```

## 输出解释

每行包含文件偏移和字符串；关注 flag{、http、/bin/sh、password、secret 以及异常长的 base64/blob。

## 常见错误与限制

strings 只提取连续可打印字节，压缩/加密数据不会直接出现；UPX 样本先 `upx -d`。

## 关联条目

- [xxd](/memory/catalog/tool_xxd/document)
- [radare2](/memory/catalog/tool_radare2/document)

## 官方参考

- [https://sourceware.org/binutils/docs/binutils/strings.html](https://sourceware.org/binutils/docs/binutils/strings.html)
