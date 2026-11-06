# nikto

Web 服务器与 CGI 配置类问题的签名扫描器。

## 用途与适用场景

指纹识别之后做一次服务器级别的已知问题排查：默认文件、危险方法、版本过期、备份文件等。

## 版本检查

```bash
nikto -Version
```

## 命令、导入与镜像路径

- 常用入口：`nikto`
- 镜像路径：`/usr/bin/nikto（Kali rolling 包）`

## 常用工作流

先用 -maxtime 限制总时长，再用 -Tuning 收窄测试类型；结果逐条手工复核，不要直接把扫描输出当成漏洞结论。

## 可执行示例

```bash
nikto -host http://TARGET/ -maxtime 5m -nointeractive -output /tmp/nikto.txt -Format txt
```

## 输出解释

每条问题以 + 开头，带 OSVDB/CVE 编号与请求路径；结尾的 “items” 统计用于判断是否被 maxtime 截断。

## 常见错误与限制

噪声大、误报多，且会向目标发送大量请求。共享或生产目标必须先拿到授权并限速；它不做认证后的深度测试。

## 关联条目

- [whatweb](/memory/catalog/tool_whatweb/document)
- [nmap](/memory/catalog/tool_nmap/document)
- [nuclei](/memory/catalog/tool_nuclei/document)

## 官方参考

- [https://github.com/sullo/nikto](https://github.com/sullo/nikto)
