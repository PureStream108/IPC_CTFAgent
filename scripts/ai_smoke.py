"""CPU inference checks run inside the task image, without downloading models."""
import tempfile
from pathlib import Path

import joblib
import numpy as np
import onnx
import onnxruntime as ort
import sklearn
import torch
from onnx import TensorProto, helper
from sklearn.linear_model import LinearRegression


def main():
    model = LinearRegression().fit([[0.0], [1.0]], [1.0, 3.0])
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "model.joblib"
        joblib.dump(model, path)
        assert np.allclose(joblib.load(path).predict([[2.0]]), [5.0])
    graph = helper.make_graph(
        [helper.make_node("Identity", ["input"], ["output"])], "identity",
        [helper.make_tensor_value_info("input", TensorProto.FLOAT, [1])],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, [1])],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 9
    onnx.checker.check_model(model)
    session = ort.InferenceSession(model.SerializeToString(), providers=["CPUExecutionProvider"])
    assert np.allclose(session.run(None, {"input": np.array([3], dtype=np.float32)})[0], [3])
    assert torch.tensor([2.0]).numpy()[0] == 2.0
    print("AI CPU smoke checks passed:", sklearn.__version__, ort.__version__)


if __name__ == "__main__":
    main()
