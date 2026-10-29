"""Teacher logits HTTP contract with a stand-in backend (no NPU)."""

import hashlib
import http.client
import threading

import numpy as np
import pytest

from resource_advisor.mobilint_teacher import MAX_BATCH, make_server, teacher_logits


class Echo:
    """Deterministic stand-in for qbruntime: logit k = mean of channel (k % 3) + k."""

    def infer(self, image):
        means = image.reshape(-1, 3).mean(0)
        return np.array([means[k % 3] + k for k in range(1000)], dtype=np.float32)


@pytest.fixture
def url():
    server = make_server(Echo(), "127.0.0.1", 0, {"model_sha256": "fixture"})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_batch_logits_round_trip_with_identity_and_timings(url):
    batch = np.random.default_rng(0).standard_normal((4, 224, 224, 3)).astype(np.float32)
    logits, receipt = teacher_logits(url, batch)
    assert logits.shape == (4, 1000)
    assert logits[2, 4] == pytest.approx(batch[2].reshape(-1, 3).mean(0)[1] + 4, rel=1e-5)
    assert receipt["teacher"] == {"model_sha256": "fixture"}
    assert receipt["client"]["request_bytes"] == 4 * 224 * 224 * 3 * 4
    assert len(receipt["server"]["infer_seconds_per_sample"]) == 4
    assert receipt["input_sha256"] == hashlib.sha256(batch.tobytes()).hexdigest()


def post(url, body, shape, digest=None):
    port = int(url.rsplit(":", 1)[1])
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(
        "POST",
        "/v1/teacher/logits",
        body=body,
        headers={
            "X-Teacher-Shape": shape,
            "X-Input-Sha256": digest or hashlib.sha256(body).hexdigest(),
        },
    )
    r = c.getresponse()
    r.read()
    return r.status


def test_malformed_or_oversized_requests_are_rejected(url):
    good = np.zeros((1, 224, 224, 3), np.float32).tobytes()
    assert post(url, good, "1,224,224,3", digest="0" * 64) == 400
    assert post(url, good, "2,224,224,3") == 400
    assert post(url, good[:-4], "1,224,224,3") == 400
    assert post(url, good, f"{MAX_BATCH + 1},224,224,3") == 400
    nan = np.full((1, 224, 224, 3), np.nan, np.float32).tobytes()
    assert post(url, nan, "1,224,224,3") == 400


def test_client_pins_teacher_identity_and_batch_bound(url):
    batch = np.zeros((1, 224, 224, 3), np.float32)
    teacher_logits(url, batch, expect={"model_sha256": "fixture"})
    with pytest.raises(RuntimeError, match="pinned teacher"):
        teacher_logits(url, batch, expect={"model_sha256": "other"})
    with pytest.raises(ValueError):
        teacher_logits(url, np.zeros((MAX_BATCH + 1, 224, 224, 3), np.float32))
    assert MAX_BATCH == 55
