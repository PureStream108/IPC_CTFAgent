# binwalk

识别并提取固件、镜像和 blob 中嵌入的文件与文件系统。

## 用途与适用场景

遇到未知固件、磁盘镜像、压缩包嵌套或文件末尾附加数据时，先用 binwalk 建立结构图。

## 版本检查

```bash
binwalk --help | head -5
```

## 命令、导入与镜像路径

- 常用入口：`binwalk`
- 镜像路径：`/usr/bin/binwalk`

## 常用工作流

先 `binwalk file` 看签名，再用 `binwalk -e` 提取到 `_file.extracted`；需要按类型恢复时配合 foremost。提取后立即 `file`/`ls -la` 检查产物。

## 可执行示例

```bash
binwalk /tmp/firmware.bin && binwalk -e --run-as=root /tmp/firmware.bin && find _firmware.bin.extracted -maxdepth 2 -type f | head
```

## 输出解释

每行给出偏移、签名和描述；提取目录按偏移命名。重点关注 squashfs、cpio、gzip、ELF 和证书。

## 常见错误与限制

binwalk 的签名匹配可能误报；加密/自定义容器无法直接提取。不要在题目原件目录递归提取，避免污染。

## 关联条目

- [foremost](/memory/catalog/tool_foremost/document)
- [file](/memory/catalog/tool_file/document)

## 官方参考

- [https://github.com/ReFirmLabs/binwalk](https://github.com/ReFirmLabs/binwalk)
