# curl

使用 curl 或 wget 发起 HTTP 请求、下载附件并检查响应头、重定向和 API 数据。

## 用途与适用场景

curl 适合可重复的协议探测和 API 请求，wget 适合递归或断点下载；敏感认证头只放在当前命令中。

## 版本检查

```bash
curl --version
```

## 命令、导入与镜像路径

- 常用入口：`curl`
- 镜像路径：`/usr/bin/curl 与 /usr/bin/wget`

## 常用工作流

1. 在 Member 工作目录中确认附件、目标地址和授权范围。
2. 按题目类型使用 `curl` 进行最小化探测。
3. 保存原始输出，再根据结果逐步增加参数，避免一开始执行破坏性操作。

## 可执行示例

```bash
curl -iL https://example.com/ && wget -O attachment.bin https://example.com/attachment.bin
```

## 输出解释

重点检查退出码、错误输出、命中项、地址/偏移和生成文件；将可复现结论记录到项目事实与 WP。

## 常见错误与限制

版本和参数可能随镜像升级变化，执行前先查看 `--help`。

## 关联条目

- 可通过 Memory 工具目录返回同级目录查看相关能力。

## 官方参考

- [https://curl.se/docs/](https://curl.se/docs/)
