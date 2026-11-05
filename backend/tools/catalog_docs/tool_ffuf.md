# ffuf

高速 Web 内容与参数模糊测试器。

## 用途与适用场景

目录、文件、参数名、vhost 与请求头发现；比 dirb 快，也比 wfuzz 更易表达过滤条件。

## 版本检查

```bash
ffuf -V
```

## 命令、导入与镜像路径

- 常用入口：`ffuf`
- 镜像路径：`/usr/local/bin/ffuf（固定 release 版本）`

## 常用工作流

用镜像内字典做第一轮小字典探测，再根据命中扩展目录深度；每次显式给 -t 与 -rate，确认目标能承受再加速。

## 可执行示例

```bash
ffuf -s -w /workspace/wordlists/SecLists/Discovery/Web-Content/common.txt -u http://TARGET/FUZZ -t 20 -rate 50 -mc 200,204,301,302,401,403
```

## 输出解释

默认输出命中行（状态码、长度、词数、行数、URL）；-o 可写 JSON。全部 403 通常意味着前置网关在统一响应。

## 常见错误与限制

不带 -mc 会把 404 也算命中；自动校准（默认开启）在动态页面上可能滤掉真实结果，异常时应加 -ac 关闭。

## 关联条目

- [feroxbuster](/memory/catalog/tool_feroxbuster/document)
- [gobuster](/memory/catalog/tool_gobuster/document)
- [dirb](/memory/catalog/tool_dirb/document)

## 官方参考

- [https://github.com/ffuf/ffuf](https://github.com/ffuf/ffuf)
