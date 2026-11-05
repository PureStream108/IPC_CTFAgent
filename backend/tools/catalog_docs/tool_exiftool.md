# exiftool

读取和写回图片、PDF、Office 与媒体文件的元数据。

## 用途与适用场景

需要检查 EXIF、XMP、GPS、作者、软件版本、注释或隐藏字段时使用；也可批量扫描目录。

## 版本检查

```bash
exiftool -ver
```

## 命令、导入与镜像路径

- 常用入口：`exiftool`
- 镜像路径：`/usr/bin/exiftool`

## 常用工作流

先用 `exiftool -a -G -s file` 显示所有组和原始标签；批量用 `exiftool -r -csv dir > meta.csv`。写回只对副本，并先备份。

## 可执行示例

```bash
exiftool -a -G -s /tmp/image.jpg | head -80 && exiftool -r -csv /tmp/evidence > /tmp/metadata.csv
```

## 输出解释

关注 GPS、Artist、Comment、UserComment、Software、Thumbnail 和异常长字段；缩略图可单独提取。

## 常见错误与限制

写回会修改文件且可能破坏签名；CTF 中默认只读。某些格式的元数据被压缩或加密，exiftool 可能只显示容器层。

## 关联条目

- [steghide](/memory/catalog/tool_steghide/document)
- [file](/memory/catalog/tool_file/document)

## 官方参考

- [https://exiftool.org/](https://exiftool.org/)
