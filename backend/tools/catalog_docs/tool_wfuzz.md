# wfuzz

通用 HTTP 模糊测试器，支持多迭代器与复杂过滤。

## 用途与适用场景

当 ffuf 的过滤表达不够用时，用 wfuzz 的 --hs/--hc/--sc 组合精确分离响应；也可以对参数值与请求头做多位置模糊测试。

## 版本检查

```bash
wfuzz --help | head -20
```

## 命令、导入与镜像路径

- 常用入口：`wfuzz`
- 镜像路径：`/usr/local/bin/wfuzz（pip 固定版本 3.1.0）`

## 常用工作流

先用小字典确认过滤条件有效，再扩大范围；用 -t 限制并发、-s 增加请求间隔，避免触发目标限流或 WAF。

## 可执行示例

```bash
wfuzz -c -z file,/workspace/wordlists/SecLists/Discovery/Web-Content/common.txt --hc 404 -t 20 http://TARGET/FUZZ
```

## 输出解释

表格形式输出编号、响应码、行/词/字符数与 payload；用 -f 可导出 JSON/CSV 供后续分析。

## 常见错误与限制

参数组合较多，容易写出静默失败的命令；Python 3.10 下依赖固定版本，升级镜像时需重新验证。

## 关联条目

- [ffuf](/memory/catalog/tool_ffuf/document)
- [gobuster](/memory/catalog/tool_gobuster/document)
- [feroxbuster](/memory/catalog/tool_feroxbuster/document)

## 官方参考

- [https://github.com/xmendez/wfuzz](https://github.com/xmendez/wfuzz)
