"""Fixed JSON-only MLflow loader for the qualified two-feature linear model."""

import json
import math
from pathlib import Path


class LinearModel:
    def __init__(self, checkpoint):
        weights, bias = checkpoint["weights"], checkpoint["bias"]
        if (
            len(weights) != 2
            or any(len(row) != 1 for row in weights)
            or len(bias) != 1
            or checkpoint["step"] != 10
            or any(
                type(n) not in (int, float) or not math.isfinite(n)
                for n in [*sum(weights, []), *bias]
            )
        ):
            raise ValueError("qualified two-feature, ten-step checkpoint required")
        self.weights = [weights[0][0], weights[1][0]]
        self.bias = bias[0]

    def predict(self, model_input, params=None):
        rows = model_input.tolist() if hasattr(model_input, "tolist") else model_input
        if hasattr(rows, "values"):
            rows = rows.values.tolist()
        predictions = []
        for row in rows:
            if len(row) != 2 or any(
                type(n) not in (int, float) or not math.isfinite(n) for n in row
            ):
                raise ValueError("two finite numeric features required")
            prediction = row[0] * self.weights[0] + row[1] * self.weights[1] + self.bias
            if not math.isfinite(prediction):
                raise ValueError("non-finite prediction")
            predictions.append([prediction])
        return predictions


def _load_pyfunc(data_path):
    return LinearModel(json.loads(Path(data_path).read_text()))
