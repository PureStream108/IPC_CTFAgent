# keras

Keras deep learning model loading/inference.

## 用途与适用场景

Load .h5/SavedModel, extract embedded data, inference attacks.

## 版本检查

```bash
KERAS_BACKEND=torch python3 -c "import keras; print(keras.__version__)"
```

## 命令、导入与镜像路径

- 常用入口：`KERAS_BACKEND=torch python3`
- 镜像路径：Python site-packages（Keras 3 + CPU PyTorch 后端）

## 常用工作流

1. 在 Member 工作目录中确认附件、目标地址和授权范围。
2. 设置 `KERAS_BACKEND=torch` 后导入 Keras，加载 .h5/.keras 模型并检查层与权重。
3. 保存原始输出，再根据结果逐步增加参数，避免一开始执行破坏性操作。

## 可执行示例

```bash
KERAS_BACKEND=torch python3 -c "import keras; print(keras.__version__)"
```

## 输出解释

重点检查退出码、错误输出、命中项、地址/偏移和生成文件；将可复现结论记录到项目事实与 WP。

## 常见错误与限制

仅在合法授权的 CTF、靶场和研究环境中使用。

## 关联条目

- 可通过 Memory 工具目录返回同级目录查看相关能力。

## 官方参考

- 使用镜像内 `--help`、语言内置帮助或工具上游仓库作为版本对应参考。
