# feroxbuster

递归式内容发现扫描器，会自动解析页面链接继续探测。

## 用途与适用场景

目标站点自身链接结构复杂时，用递归模式把目录树完整铺开，比固定深度的手工扫描更省步骤。

## 版本检查

```bash
feroxbuster --version
```

## 命令、导入与镜像路径

- 常用入口：`feroxbuster`
- 镜像路径：`/usr/local/bin/feroxbuster（固定 release 版本）`

## 常用工作流

第一轮用 --no-recursion 与 -t 10 观察响应，稳定后再开递归；用 -d 限制最大深度、--rate-limit 限制每秒请求。

## 可执行示例

```bash
feroxbuster -q -u http://TARGET/ -w /workspace/wordlists/SecLists/Discovery/Web-Content/raft-medium-directories.txt -t 10 --depth 2
```

## 输出解释

命中行给出状态码、方法、耗时、行数与 URL；文件末尾打印统计与被过滤的数量。

## 常见错误与限制

递归会放大请求量，脆弱服务容易被打挂；默认加 -C 过滤 404，动态页面需要关闭过滤。

## 关联条目

- [ffuf](/memory/catalog/tool_ffuf/document)
- [gobuster](/memory/catalog/tool_gobuster/document)
- [dirb](/memory/catalog/tool_dirb/document)

## 官方参考

- [https://github.com/epi052/feroxbuster](https://github.com/epi052/feroxbuster)
