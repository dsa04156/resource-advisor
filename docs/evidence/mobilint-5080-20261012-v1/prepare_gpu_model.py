"""Prepare the pinned public Candy graph. This does not identify the MXQ source."""
import hashlib
from pathlib import Path
import sys
import urllib.request

import onnx
import onnxruntime as ort

URL = ("https://media.githubusercontent.com/media/onnx/models/"
       "c5cae0f1942c8d7727372c7b1f6b485aa39e4421/validated/vision/"
       "style_transfer/fast_neural_style/model/candy-9.onnx")
SOURCE_SHA256 = "9d11a3529d1e547da6ae07201d93484dbab2ec0a3614535752c8f40f0fe2968a"
PREPARED_SHA256 = "ab607dd30842a4b96b7fbbf7ddf01a18509a308e0584bcfcfc6c6198f3c2ef40"


def main():
    if ort.__version__ != "1.23.2" or onnx.__version__ != "1.19.1":
        raise ValueError("ONNX1.19.1 and ORT1.23.2 required for reproducible serialization")
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=True)
    source = urllib.request.urlopen(URL, timeout=60).read()
    assert hashlib.sha256(source).hexdigest() == SOURCE_SHA256
    (output / "candy-9.onnx").write_bytes(source)
    model = onnx.load_from_string(source)
    original = {x.name: onnx.numpy_helper.to_array(x).tobytes() for x in model.graph.initializer}
    inputs = [x for x in model.graph.input if x.name not in original]
    assert len(inputs) == 1 and len(model.graph.output) == 1
    for value in inputs + list(model.graph.output):
        for dimension, size in zip(value.type.tensor_type.shape.dim, [1, 3, 224, 344]):
            dimension.dim_value = size
    del model.graph.input[:]
    model.graph.input.extend(inputs)
    model.ir_version = 4
    onnx.checker.check_model(model)
    frozen = output / "candy-9-224x344-frozen.onnx"
    onnx.save(model, frozen)
    options = ort.SessionOptions()
    options.log_severity_level = 3
    options.intra_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
    options.optimized_model_filepath = str(output / "gpu-candy.onnx")
    # CPU constant-folding prepares the graph; no CPU latency is reported.
    ort.InferenceSession(str(frozen), options, providers=["CPUExecutionProvider"])
    prepared = onnx.load(output / "gpu-candy.onnx")
    actual = {x.name: onnx.numpy_helper.to_array(x).tobytes() for x in prepared.graph.initializer}
    assert all(actual.get(name) == value for name, value in original.items())
    digest = hashlib.sha256((output / "gpu-candy.onnx").read_bytes()).hexdigest()
    assert digest == PREPARED_SHA256, digest
    print("prepared SHA256 " + digest)


if __name__ == "__main__":
    main()
