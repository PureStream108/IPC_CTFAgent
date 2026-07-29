# Java 21 / Maven

用于 Java 题目、反序列化工具、JAR 工程以及 Ghidra 运行时。

## 用途与适用场景

使用 javac 编译单文件，使用 Maven 构建依赖工程，并确认 JAVA_HOME=/opt/java21。

## 版本检查

```bash
java --version
```

## 命令、导入与镜像路径

- 常用入口：`java`
- 镜像路径：`/opt/java21`

## 常用工作流

1. 在 Member 工作目录中确认附件、目标地址和授权范围。
2. 按题目类型使用 `java` 进行最小化探测。
3. 保存原始输出，再根据结果逐步增加参数，避免一开始执行破坏性操作。

## 可执行示例

```bash
java -version && javac -version && mvn -version
```

## 输出解释

重点检查退出码、错误输出、命中项、地址/偏移和生成文件；将可复现结论记录到项目事实与 WP。

## 常见错误与限制

版本和参数可能随镜像升级变化，执行前先查看 `--help`。

## 关联条目

- 可通过 Memory 工具目录返回同级目录查看相关能力。

## 官方参考

- [https://docs.oracle.com/en/java/javase/21/](https://docs.oracle.com/en/java/javase/21/)
