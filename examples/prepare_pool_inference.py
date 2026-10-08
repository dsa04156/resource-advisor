"""Train a real digit classifier on CPU and freeze portable FP32 inference inputs."""

import argparse
import base64
import hashlib
import json
from pathlib import Path

import numpy as np
import sklearn
from sklearn.datasets import load_digits
from sklearn.model_selection import train_test_split
from sklearn.neural_network import MLPClassifier
from threadpoolctl import threadpool_limits


def packed(value):
    raw = np.asarray(value, dtype="<f4", order="C").tobytes()
    return dict(data=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest())


def prepare(output):
    dataset = load_digits()
    inputs = np.asarray(dataset.data / 16, dtype=np.float32)
    indices = np.arange(len(inputs))
    train, test = train_test_split(
        indices, test_size=0.2, random_state=20261008, stratify=dataset.target
    )
    with threadpool_limits(limits=1):
        model = MLPClassifier(
            hidden_layer_sizes=(256, 256),
            random_state=20261008,
            max_iter=60,
            batch_size=128,
            learning_rate_init=0.001,
        ).fit(inputs[train], dataset.target[train])
        selected = inputs[test[:256]]
        value = selected
        layers = []
        for index, (weights, bias) in enumerate(zip(model.coefs_, model.intercepts_, strict=True)):
            weights, bias = weights.astype("<f4"), bias.astype("<f4")
            value = value @ weights + bias
            if index < len(model.coefs_) - 1:
                value = np.maximum(value, 0)
            layers.append(
                dict(
                    inputs=weights.shape[0],
                    outputs=weights.shape[1],
                    weights=packed(weights),
                    bias=packed(bias),
                )
            )
        accuracy = float(np.mean(value.argmax(1) == dataset.target[test[:256]]))
        assert accuracy >= 0.90, "Do not benchmark a classifier failing the frozen accuracy gate"
    fixture = dict(
        schema_version=1,
        seed=20261008,
        model="trained Digits MLP 64-256-256-10",
        dataset="scikit-learn load_digits; stratified 80/20 train/test split",
        sklearn_version=sklearn.__version__,
        numpy_version=np.__version__,
        precision="fp32",
        batch_size=256,
        layers=layers,
        input=packed(selected),
        reference=packed(value),
        labels=dataset.target[test[:256]].tolist(),
        train_indices=train.tolist(),
        test_indices=test.tolist(),
        accuracy=accuracy,
        quality=dict(top1_reference_agreement=1.0, atol=0.003, rtol=0.001),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(fixture, separators=(",", ":")) + "\n")
    print(
        json.dumps(
            dict(
                fixture=str(output),
                accuracy=accuracy,
                fixture_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
            )
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    prepare(parser.parse_args().output)
