# nuclei

基于 YAML 模板的已知 CVE 与配置问题扫描器，模板已固化在镜像内。

## 用途与适用场景

在确认真实技术栈与版本后，用针对性模板快速验证已知漏洞，而不是手工重写每个 PoC。

## 版本检查

```bash
nuclei -version && nuclei -duc -tl | head -5
```

## 命令、导入与镜像路径

- 常用入口：`nuclei`
- 镜像路径：`/usr/local/bin/nuclei（固定 release 版本）与 /root/nuclei-templates`
- 模板目录必须是真实目录：nuclei v3.4.10 不会跟随指向 `$HOME/nuclei-templates` 的符号链接，会报 `no templates found`。

## 常用工作流

先按标签选模板（-tags cve），再用 -t 指向具体目录收窄；加 -rl 限制请求速率、-timeout 限制单请求时长。模板目录只读，不要在容器里更新。

## 可执行示例

```bash
nuclei -duc -u http://TARGET/ -tags cve -rl 20 -timeout 10 -silent
```

## 输出解释

每条命中以 [template-id] [protocol] [severity] host 开头，后面是匹配到的证据；-jsonl 可输出机器可读结果。

## 常见错误与限制

模板只覆盖已被收录的漏洞；自定义代码缺陷不会被发现。首次运行必须使用镜像内模板，不要依赖联网下载。

## 关联条目

- [whatweb](/memory/catalog/tool_whatweb/document)
- [ffuf](/memory/catalog/tool_ffuf/document)
- [nmap](/memory/catalog/tool_nmap/document)

## 官方参考

- [https://docs.projectdiscovery.io/tools/nuclei/overview](https://docs.projectdiscovery.io/tools/nuclei/overview)
