# r2pipe

从 Python 驱动 radare2 CLI 并获取文本或 JSON 分析结果。

## 用途与适用场景

需要把 r2 的 aaa/afl/pdf/izz 等命令嵌入 pwntools 或批量分析脚本，而不想手工交互时使用。

## 版本检查

```bash
python3 -c "import r2pipe; print(r2pipe.__file__)"
```

## 命令、导入与镜像路径

- 常用入口：`python3 / r2pipe`
- 镜像路径：`Python site-packages/r2pipe、/usr/local/bin/r2`

## 常用工作流

用 `r2pipe.open(path, flags=['-2'])` 打开副本，执行 `aaa` 后按 JSON 解析 `aflj`、`pdfj`、`izzj`；脚本结束必须 `quit()`，避免遗留 r2 进程。

## 可执行示例

```bash
python3 -c "import r2pipe,json; r=r2pipe.open('/tmp/challenge'); r.cmd('aaa'); print(json.loads(r.cmd('aflj'))[:3]); r.quit()"
```

## 输出解释

函数列表包含地址、大小、名称；pdfj 给出基本块和指令。若输出为空，确认二进制架构、权限和 aaa 是否成功。

## 常见错误与限制

r2pipe 依赖可执行的 r2；不要对题目原件执行写操作。r2 分析大型二进制可能较慢，必要时先用 `aa` 或限定范围。

## 关联条目

- [radare2](/memory/catalog/tool_radare2/document)
- [objdump](/memory/catalog/tool_objdump/document)

## 官方参考

- [https://github.com/radareorg/radare2-r2pipe](https://github.com/radareorg/radare2-r2pipe)
