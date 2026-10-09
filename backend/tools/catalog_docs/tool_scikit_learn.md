# scikit-learn

使用 scikit-learn 与 joblib 分析经典机器学习模型、预处理管线和序列化估计器。

## 用途与适用场景

适用于分类、回归、聚类、特征预处理、模型参数检查和受控 CPU 推理。

## 版本检查

```bash
python3 -c "import sklearn,joblib; print(sklearn.__version__)"
```

## 命令、导入与镜像路径

- Python 导入：`sklearn`、`joblib`、`numpy`
- 镜像路径：Python site-packages

## 常用工作流

1. 在任务沙箱中识别模型类型和输入形状。
2. 检查预处理器、类别标签、学习参数和依赖版本。
3. 使用最小受控输入执行推理并保存结果。

## 可执行示例

```bash
python3 -c "import joblib; m=joblib.load('model.joblib'); print(type(m)); print(m.get_params())"
```

## 输出解释

核对估计器类型、特征顺序、类别映射、预测概率和数据类型，避免把形状错误误判为模型行为。

## 常见错误与限制

joblib/pickle 文件可执行反序列化代码，只能在题目沙箱中加载不可信模型。版本不匹配时先记录原始依赖信息。

## 关联条目

- ONNX Runtime 可用于分析导出的跨框架推理模型。
- Keras / h5py 用于神经网络和 HDF5 模型。

## 官方参考

- [https://scikit-learn.org/stable/](https://scikit-learn.org/stable/)
