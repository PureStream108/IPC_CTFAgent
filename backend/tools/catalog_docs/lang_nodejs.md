# JavaScript / Node.js

用于运行 Node.js 题目、审计 npm 项目和编写 JavaScript 探针。

## 用途与适用场景

需要执行服务端 JavaScript、复现 npm 依赖漏洞、调试原型链污染，或用 playwright-cli 驱动页面时使用。

## 版本检查

```bash
node --version && npm --version
```

## 命令、导入与镜像路径

- 常用入口：`node / npm`
- 镜像路径：`/usr/local/bin/node（Node 22.23.4）、/usr/local/bin/npm`

## 常用工作流

先确认 node 与 npm 版本，再检查 package.json、锁文件和入口脚本。运行题目代码时限制环境变量与网络权限；需要浏览器时改用 playwright-cli。

## 可执行示例

```bash
node --version && node -e "console.log(process.version, process.platform)"
```

## 输出解释

关注 Node 版本、模块解析路径、未捕获异常栈和 npm 依赖树；原型链题目可打印 Object.prototype 与目标构造函数。

## 常见错误与限制

任务镜像不再安装 apt Node 12；npm 全局安装会改动镜像层，优先在 /tmp 或项目目录使用本地依赖。

## 关联条目

- [playwright-cli](/memory/catalog/tool_playwright_cli/document)

## 官方参考

- [https://nodejs.org/docs/latest/api/](https://nodejs.org/docs/latest/api/)
