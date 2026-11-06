# wafw00f

识别目标前置的 Web 应用防火墙类型。

## 用途与适用场景

当同一个 payload 在本地可用、到目标却被拦时，先确认是否存在 WAF，避免把拦截误判成“漏洞不存在”。

## 版本检查

```bash
wafw00f --help | head -20
```

## 命令、导入与镜像路径

- 常用入口：`wafw00f`
- 镜像路径：`/usr/local/bin/wafw00f（pip 固定版本 2.2.0）`

## 常用工作流

先跑默认探测，再对可疑结果加 -a 做全部指纹匹配；确认 WAF 后调整 payload 编码与请求节奏，而不是继续加大并发。

## 可执行示例

```bash
wafw00f -a http://TARGET/
```

## 输出解释

打印 “is behind X” 的判定与参与的检测插件；No WAF detected 只表示没有匹配到已知指纹，不等于没有防护。

## 常见错误与限制

只识别已知 WAF 指纹；自研网关、云厂商默认策略或纯 IP 白名单都会返回“未检测到”。它本身也会触发防护日志。

## 关联条目

- [whatweb](/memory/catalog/tool_whatweb/document)
- [ffuf](/memory/catalog/tool_ffuf/document)
- [nikto](/memory/catalog/tool_nikto/document)

## 官方参考

- [https://github.com/EnableSecurity/wafw00f](https://github.com/EnableSecurity/wafw00f)
