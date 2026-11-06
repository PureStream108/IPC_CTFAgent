# file

根据魔数和结构识别文件类型、架构与容器格式。

## 用途与适用场景

拿到未知附件的第一步：区分 ELF、脚本、压缩包、镜像、PCAP、Office 文档或损坏文件。

## 版本检查

```bash
file --version
```

## 命令、导入与镜像路径

- 常用入口：`file`
- 镜像路径：`/usr/bin/file`

## 常用工作流

先 `file -k` 保留可能的多重识别，再结合 `xxd -l 64` 看魔数、`binwalk` 看嵌套结构。扩展名不可信，始终以内容为准。

## 可执行示例

```bash
file -k /tmp/attachment.bin && xxd -l 64 /tmp/attachment.bin
```

## 输出解释

输出通常为 `类型, 架构, 链接方式`；关注 ELF 32/64-bit、statically/dynamically linked、archive、gzip、PNG、PDF 和 filesystem。

## 常见错误与限制

file 只做启发式识别，可能把加密/压缩数据标为 data；需要进一步用 binwalk、7z、foremost 或专用解析器。

## 关联条目

- [file / xxd / hexdump](/memory/catalog/core_file_triage/document)
- [xxd](/memory/catalog/tool_xxd/document)

## 官方参考

- [https://www.darwinsys.com/file/](https://www.darwinsys.com/file/)
