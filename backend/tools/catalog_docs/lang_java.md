# Java 21 / Maven

用于 Java 题目、反序列化分析、JAR 工程和 Maven 依赖构建。

## 用途与适用场景

需要运行或编译 Java 代码、检查 JAR/WAR 结构、分析 Java 反序列化 gadget，或构建依赖 Maven 的利用工程时使用。Ghidra/PyGhidra 已移除，Java 反编译请改用 r2、javap 和字节码工具。

## 版本检查

```bash
java --version && javac --version && mvn --version
```

## 命令、导入与镜像路径

- 常用入口：`java / javac / mvn / javap`
- 镜像路径：`/opt/java21、/opt/maven/bin/mvn`

## 常用工作流

先 `java -version` 与 `javap -version` 确认运行时；分析 JAR 时用 `unzip -l`、`javap -c -p` 查看类与方法，再按需用 Maven 编译 ysoserial 或自定义 gadget。运行不可信 JAR 前先检查入口类、依赖和网络行为。

## 可执行示例

```bash
javap -classpath app.jar -c -p com.example.Main | head -80
```

## 输出解释

确认 class 文件版本、方法字节码、常量池引用和异常表；反序列化题重点关注 readObject、ObjectInputStream 与链式 getter/setter 调用。

## 常见错误与限制

Maven 首次构建可能访问外网并耗时；离线环境先检查 ~/.m2 缓存。不要直接用 root 权限运行来源不明的 JAR。

## 关联条目

- [ysoserial](/memory/catalog/tool_ysoserial/document)
- [radare2](/memory/catalog/tool_radare2/document)

## 官方参考

- [https://docs.oracle.com/en/java/javase/21/](https://docs.oracle.com/en/java/javase/21/)
