# onnxruntime

使用 ONNX 检查计算图，并通过 ONNX Runtime 的 CPU provider 执行可复现推理。

## 用途与适用场景

适用于 ONNX 图结构、输入输出、张量形状、初始化权重、算子集和模型推理分析。

## 版本检查

```bash
python3 -c "import onnx,onnxruntime; print(onnx.__version__, onnxruntime.__version__)"
```

## 命令、导入与镜像路径

- Python 导入：`onnx`、`onnxruntime`、`numpy`
- 默认 provider：`CPUExecutionProvider`

## 常用工作流

1. 使用 `onnx.checker.check_model` 验证模型结构。
2. 枚举 session 输入输出名称、形状与数据类型。
3. 构造最小 NumPy 输入并保存原始推理输出。

## 可执行示例

```bash
python3 -c "import onnxruntime as ort; s=ort.InferenceSession('model.onnx',providers=['CPUExecutionProvider']); print(s.get_inputs(),s.get_outputs())"
```

## 输出解释

重点核对动态维度、输入名称、dtype、opset 和 provider，区分图验证失败与推理数据不匹配。

## 常见错误与限制

自定义算子可能需要镜像中不存在的运行库。不要从不可信模型引用的路径加载外部动态库。

## 关联条目

- scikit-learn / joblib 用于经典模型与预处理管线。
- torch 和 Keras 可用于检查模型导出前的框架结构。

## 官方参考

- [https://onnxruntime.ai/docs/](https://onnxruntime.ai/docs/)
