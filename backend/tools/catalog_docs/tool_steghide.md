# steghide

在 JPEG/BMP/WAV 中检测、提取和嵌入隐藏数据。

## 用途与适用场景

怀疑图片或音频使用 steghide 隐写，且可能需要空口令或已知口令时使用。

## 版本检查

```bash
steghide --version
```

## 命令、导入与镜像路径

- 常用入口：`steghide`
- 镜像路径：`/usr/bin/steghide`

## 常用工作流

先 `steghide info file` 判断是否含嵌入数据；提取时先试空口令（直接回车），再用题目线索中的口令。嵌入只对副本操作。

## 可执行示例

```bash
steghide info /tmp/cover.jpg && steghide extract -sf /tmp/cover.jpg -p '' -xf /tmp/steghide.out
```

## 输出解释

成功时输出写入的文件名和大小；失败会提示口令错误或文件格式不支持。提取物用 `file` 和 `xxd` 继续检查。

## 常见错误与限制

steghide 只支持特定 JPEG/BMP/WAV 编码；PNG 用 zsteg，音频也可试 sox/spectrogram。口令错误不会破坏原文件。

## 关联条目

- [zsteg](/memory/catalog/lib_zsteg/document)
- [exiftool](/memory/catalog/tool_exiftool/document)

## 官方参考

- [https://steghide.sourceforge.net/](https://steghide.sourceforge.net/)
