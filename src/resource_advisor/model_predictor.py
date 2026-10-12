"""KServe v1 for the platform's fixed JSON linear model, without runtime credentials."""

import base64
import hashlib
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import Field

from .contracts import Contract, Digest, Ref, signature
from .linear_model import LinearModel
from .model_serving import PredictionInput

PACKAGE_FILES = frozenset(
    {
        "MLmodel",
        "code/ra_linear_model.py",
        "data/checkpoint.json",
        "lineage.json",
        "requirements.txt",
        "conda.yaml",
        "python_env.yaml",
    }
)
MAX_PACKAGE_BYTES = 256 * 1024


class ModelDescription(Contract):
    service_name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    deployment_ref: Ref
    model_uri: str = Field(pattern=r"^models:/[a-zA-Z0-9_.-]{1,96}/[1-9][0-9]*$")
    model_package_digest: Digest
    output_checkpoint_digest: Digest


def verified_package(package):
    if len(json.dumps(package).encode()) > MAX_PACKAGE_BYTES:
        raise ValueError("model package exceeds bounded contract")
    description = ModelDescription.model_validate(package["description"])
    if set(package["files"]) != PACKAGE_FILES:
        raise ValueError("model package file contract mismatch")
    files = {name: base64.b64decode(data, validate=True) for name, data in package["files"].items()}
    hashes = {name: "sha256:" + hashlib.sha256(data).hexdigest() for name, data in files.items()}
    if signature(hashes) != description.model_package_digest:
        raise ValueError("model package integrity mismatch")
    if files["code/ra_linear_model.py"] != Path(__file__).with_name("linear_model.py").read_bytes():
        raise ValueError("only the fixed platform linear loader is supported")
    checkpoint = json.loads(files["data/checkpoint.json"])
    lineage = json.loads(files["lineage.json"])
    if (
        signature(checkpoint) != description.output_checkpoint_digest
        or lineage["evaluation"]["output_checkpoint_digest"] != description.output_checkpoint_digest
        or not lineage["evaluation"]["passed"]
        or lineage["evaluation"]["mse"] > lineage["policy"]["maximum_mse"]
    ):
        raise ValueError("checkpoint/quality lineage integrity mismatch")
    return description, LinearModel(checkpoint)


def create_predictor(package):
    description, model = verified_package(package)
    identity = description.model_dump(mode="json")
    app = FastAPI(title="Qualified linear predictor")

    @app.get("/healthz")
    def health():
        return {"ready": True, "model": identity}

    @app.get("/v1/models/" + description.service_name)
    def metadata():
        return {"name": description.service_name, "ready": True, "model": identity}

    @app.post("/v1/models/" + description.service_name + ":predict")
    def predict(value: PredictionInput):
        try:
            return {"predictions": model.predict(value.instances), "model": identity}
        except (ValueError, OverflowError) as exc:
            raise HTTPException(422, "finite two-feature predictions required") from exc

    return app


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, default=Path("/model/package.json"))
    args = parser.parse_args()
    with args.package.open("rb") as stream:
        payload = stream.read(MAX_PACKAGE_BYTES + 1)
    if len(payload) > MAX_PACKAGE_BYTES:
        raise ValueError("model package exceeds bounded contract")
    uvicorn.run(create_predictor(json.loads(payload)), host="0.0.0.0", port=8080)
