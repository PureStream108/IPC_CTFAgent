# dirb

经典 Web 内容扫描器，自带内置字典。

## 用途与适用场景

手边还没有合适字典、只想快速看一眼常见路径时使用；它的内置字典很小，适合作为最低成本的第一轮。

## 版本检查

```bash
dirb 2>&1 | head -5
```

## 命令、导入与镜像路径

- 常用入口：`dirb`
- 镜像路径：`/usr/bin/dirb（Kali rolling 包）`

## 常用工作流

先用默认字典跑一次，再用 -w 指定项目字典；用 -z 增加延迟、-r 关闭递归，避免脆弱服务被打断。

## 可执行示例

```bash
dirb http://TARGET/ /workspace/wordlists/SecLists/Discovery/Web-Content/common.txt -S -r -z 200
```

## 输出解释

以 + 开头的是命中，带状态码与响应长度；==> DIRECTORY 表示命中目录。结尾给出扫描量与耗时。

## 常见错误与限制

内置字典覆盖有限，空结果不能证明没有隐藏路径；递归行为容易造成请求风暴，默认应加 -r。

## 关联条目

- [ffuf](/memory/catalog/tool_ffuf/document)
- [gobuster](/memory/catalog/tool_gobuster/document)
- [字典与 rockyou](/memory/catalog/core_wordlists/document)

## 官方参考

- [https://sourceforge.net/projects/dirb/](https://sourceforge.net/projects/dirb/)
