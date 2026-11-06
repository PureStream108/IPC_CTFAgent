# gobuster

Go 编写的目录、DNS 与 vhost 枚举器。

## 用途与适用场景

需要一个依赖少、启动快的枚举器时使用；dns 模式可用于子域名爆破，vhost 模式可用于共享 IP 的虚拟主机发现。

## 版本检查

```bash
gobuster --version
```

## 命令、导入与镜像路径

- 常用入口：`gobuster`
- 镜像路径：`/usr/local/bin/gobuster（固定 release v3.8.2）`
- apt 的 jammy 版本只有 v2.0.1：没有 `version` 子命令，`-q`/`--timeout` 的行为也不同，因此本镜像从 release 安装 v3，文档与验收都按 v3 写。

## 常用工作流

先按模式选子命令（dir/dns/vhost），再用 -w 指定字典、-t 限制并发、--timeout 限制单请求时长；DNS 模式只对授权域使用。

## 可执行示例

```bash
gobuster dir -u http://TARGET/ -w /workspace/wordlists/SecLists/Discovery/Web-Content/common.txt -t 20 --timeout 10s -q
```

## 输出解释

命中行给出路径与状态码（dir），或子域名与 IP（dns）。没有命中时会打印 “Finished”，不等于目标没有隐藏路径。

## 常见错误与限制

v3.8.2 没有 `version` 子命令（会报 `No help topic for 'version'`），版本用 `gobuster --version`。v2 与 v3 的参数也不兼容，照抄旧教程的 `gobuster -u ... -w ...` 会直接报参数错误；脚本应以 `gobuster dir --help` 的实际输出为准。默认跟随重定向会让状态码失真。

## 关联条目

- [ffuf](/memory/catalog/tool_ffuf/document)
- [dirb](/memory/catalog/tool_dirb/document)
- [feroxbuster](/memory/catalog/tool_feroxbuster/document)

## 官方参考

- [https://github.com/OJ/gobuster](https://github.com/OJ/gobuster)
