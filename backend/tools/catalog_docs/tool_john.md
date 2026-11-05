# john

John the Ripper 离线口令破解工具。

## 用途与适用场景

对手上已经拿到的哈希（shadow、kerberoast、zip/rar、数据库导出）做离线爆破；不接触目标，因此没有目标侧风险。

## 版本检查

```bash
john < /dev/null | head -2
```

## 命令、导入与镜像路径

- 常用入口：`john`
- 镜像路径：`/usr/bin/john（Kali rolling 包，1.8.0 core 版）`
- Ubuntu 的包**不支持** `--list=build-info`（那是 jumbo 版的功能），版本检查要用无参数调用并把 stdin 关掉。

## 常用工作流

先 --format 识别哈希类型，再用字典加规则跑一轮，最后 --show 查看已破解结果；大字典任务应交给 job_start。

## 可执行示例

```bash
john --format=sha512crypt --wordlist=/workspace/wordlists/rockyou.txt --rules /tmp/shadow.txt && john --show --format=sha512crypt /tmp/shadow.txt
```

## 输出解释

破解中的进度行给出猜测速率与剩余时间；--show 输出 user:password 列表。

## 常见错误与限制

Ubuntu 包是 core 版，缺少部分格式与 GPU 支持；运行时会写 ~/.john/john.pot，跨任务复用前先清理。

## 关联条目

- [hashcat](/memory/catalog/tool_hashcat/document)
- [字典与 rockyou](/memory/catalog/core_wordlists/document)

## 官方参考

- [https://www.openwall.com/john/](https://www.openwall.com/john/)
