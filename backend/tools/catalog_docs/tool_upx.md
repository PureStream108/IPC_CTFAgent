# upx

UPX 4.2.4 检测、压缩和解压可执行文件。

## 用途与适用场景

二进制熵高、区段名异常或 strings 很少时，先判断是否 UPX 加壳；分析前对副本解压。

## 版本检查

```bash
upx --version | head -1
```

## 命令、导入与镜像路径

- 常用入口：`upx`
- 镜像路径：`/usr/local/bin/upx（4.2.4）`

## 常用工作流

先 `upx -t` 测试，再 `cp` 副本并 `upx -d`；需要压缩时保留原始文件。不要对题目原件执行 `-d` 覆盖。

## 可执行示例

```bash
upx -t /tmp/challenge && cp /tmp/challenge /tmp/challenge.upx && upx -d /tmp/challenge.upx -o /tmp/challenge.unpacked && file /tmp/challenge.unpacked
```

## 输出解释

成功时显示 packed/unpacked 大小和比例；`-t` 返回 OK 表示可识别。解压后用 checksec/readelf/r2 重新分析。

## 常见错误与限制

UPX 只处理其支持的格式和变体；修改过的 UPX 头、其他壳或加密压缩无法直接解。旧版 upx-ucl 3.96 已移除。

## 关联条目

- [radare2](/memory/catalog/tool_radare2/document)
- [objdump](/memory/catalog/tool_objdump/document)

## 官方参考

- [https://upx.github.io/](https://upx.github.io/)
