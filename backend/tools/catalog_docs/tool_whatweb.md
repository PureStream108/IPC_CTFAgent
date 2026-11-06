# whatweb

Web 技术指纹识别，输出服务器、框架、CMS 与版本。

## 用途与适用场景

在拿到一个 Web 目标后先确认技术栈，再决定走 SQL 注入、模板注入、反序列化还是文件类漏洞；指纹也会暴露版本号，可据此选 CVE。

## 版本检查

```bash
whatweb --version
```

## 命令、导入与镜像路径

- 常用入口：`whatweb`
- 镜像路径：`/usr/bin/whatweb（Kali rolling 包）`

## 常用工作流

先用默认聚合级别扫一次首页，再用 --log-brief 保存摘要；确认框架后再加 -a 3 提高入侵性，并且只对授权主机使用 --max-threads 5 以下的并发。

## 可执行示例

```bash
whatweb --color=never --log-brief=/tmp/fingerprint.txt http://TARGET/
```

## 输出解释

每行一个 URL，后接 Technology[Detail] 列表；版本号出现在方括号内。没有版本号说明指纹只到框架层，不要据此直接套用 CVE。

## 常见错误与限制

指纹是启发式的，会漏报也会误报；自定义首页、CDN 与网关会掩盖真实后端。它对每个目标都发多个请求，共享目标请限制并发。

## 关联条目

- [nmap](/memory/catalog/tool_nmap/document)
- [nikto](/memory/catalog/tool_nikto/document)
- [nuclei](/memory/catalog/tool_nuclei/document)

## 官方参考

- [https://github.com/urbanadventurer/WhatWeb](https://github.com/urbanadventurer/WhatWeb)
