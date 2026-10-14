"""Online routing over explicitly qualified endpoints; single API-process reservations."""

import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Annotated, Literal

import httpx
from pydantic import Field, model_validator

from .contracts import Contract, Digest, Ref, signature
from .model_serving import ServingUnavailable
from .service import NotFound, Rejected

Sha = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Pixel = Annotated[float, Field(ge=0, le=255, strict=True, allow_inf_nan=False)]
Image = Annotated[tuple[Pixel, ...], Field(min_length=150528, max_length=150528)]


class InferenceInput(Contract):
    instances: Annotated[tuple[Image, ...], Field(min_length=1, max_length=4)]
    deadline_ms: int = Field(default=5000, ge=50, le=10000)


class EndpointBinding(Contract):
    project_ref: Ref
    url: str
    model_ref: Ref
    source_model_sha256: Sha
    runtime: Literal["cuda", "hailo"]
    artifact_sha256: Sha
    runtime_image_digest: Digest
    qualification_digest: Digest
    input_contract: Literal["rgb-f32-nhwc-224x224x3-v1"] = "rgb-f32-nhwc-224x224x3-v1"
    output_contract: Literal["imagenet-scores-f32-1000-v1"] = "imagenet-scores-f32-1000-v1"

    @model_validator(mode="after")
    def internal_endpoint(self):
        if not re.fullmatch(
            r"https?://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\."
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.svc"
            r"(?:\.cluster\.local)?(?::[0-9]{1,5})?",
            self.url,
        ):
            raise ValueError("inference endpoints must be configured cluster-local services")
        port = httpx.URL(self.url).port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("invalid inference service port")
        return self

    def identity(self):
        return self.model_dump(exclude={"project_ref", "url"})


class InferencePool(Contract):
    ref: Ref
    model_ref: Ref
    source_model_sha256: Sha
    endpoint_refs: Annotated[tuple[Ref, ...], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def distinct_endpoints(self):
        if len(set(self.endpoint_refs)) != len(self.endpoint_refs):
            raise ValueError("endpoint references must be distinct")
        return self


@dataclass
class EndpointState:
    checked_at: float = -math.inf
    ready: bool = False
    reason: str = "UNOBSERVED"
    remote_inflight: int = 0
    local_inflight: int = 0
    max_inflight: int = 8
    service_ms: float = 1
    observed_ms: float | None = None
    blocked_until: float = 0
    successes: int = 0
    failures: int = 0


class InferenceRouter:
    def __init__(self, store, config=None, client=None):
        self.store = store
        self.bindings = {
            ref: EndpointBinding.model_validate(value)
            for ref, value in (config or {}).get("endpoints", {}).items()
        }
        self.states = {ref: EndpointState() for ref in self.bindings}
        self.lock = threading.RLock()
        self.client = client or httpx.Client(
            trust_env=False, follow_redirects=False, limits=httpx.Limits(max_connections=32)
        )
        self.owns_client = client is None

    def close(self):
        if self.owns_client:
            self.client.close()

    @staticmethod
    def pool_key(project, ref):
        return "ip-" + signature([project, ref])[7:47]

    def _binding(self, project, ref):
        binding = self.bindings.get(ref)
        if binding is None or binding.project_ref != project:
            raise Rejected("endpoint is not configured for this project")
        return binding

    @staticmethod
    def _identity_matches(binding, output):
        return isinstance(output, dict) and output.get("identity") == binding.identity()

    def _refresh(self, ref, *, deadline=None):
        state, binding = self.states[ref], self.bindings[ref]
        started = time.monotonic()
        with self.lock:
            if started - state.checked_at < 1:
                return
        if deadline is not None and time.monotonic() >= deadline:
            return
        timeout = min(0.5, deadline - time.monotonic()) if deadline else 0.5
        try:
            response = self.client.get(binding.url + "/health", timeout=max(0.001, timeout))
            response.raise_for_status()
            health = response.json()
            inflight, maximum, service_ms = (
                health["inflight"],
                health["max_inflight"],
                health["service_ms"],
            )
            if (
                type(inflight) is not int
                or type(maximum) is not int
                or not 0 <= inflight <= maximum <= 64
                or maximum < 1
                or not isinstance(service_ms, (int, float))
                or not math.isfinite(service_ms)
                or not 0 < service_ms <= 60000
            ):
                raise ValueError("invalid health counters")
            ready = health.get("ready") is True and self._identity_matches(binding, health)
            values = {
                "ready": ready,
                "reason": "READY" if ready else "NOT_READY_OR_IDENTITY_MISMATCH",
                "remote_inflight": inflight,
                "max_inflight": maximum,
                "service_ms": service_ms,
            }
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            values = {"ready": False, "reason": "HEALTH_UNAVAILABLE"}
        with self.lock:
            if state.checked_at > started:
                return
            for key, value in values.items():
                setattr(state, key, value)
            state.checked_at = time.monotonic()

    def register(self, project, value):
        bindings = {}
        for ref in value.endpoint_refs:
            binding = self._binding(project, ref)
            if (
                binding.model_ref != value.model_ref
                or binding.source_model_sha256 != value.source_model_sha256
            ):
                raise Rejected("all endpoints must serve the same source model")
            self._refresh(ref)
            with self.lock:
                if not self.states[ref].ready:
                    raise Rejected("endpoint has not passed readiness and identity checks")
            bindings[ref] = signature(binding)
        body = {
            "pool": value.model_dump(mode="json"),
            "bindings": bindings,
            "input_contract": "rgb-f32-nhwc-224x224x3-v1",
            "output_contract": "imagenet-scores-f32-1000-v1",
        }
        with self.store.transaction() as conn:
            self.store.put(conn, "inference_pool", self.pool_key(project, value.ref), project, body)
        return self.view(project, value.ref)

    def listing(self, project, page=0):
        with self.store.transaction() as conn:
            records = self.store.list(conn, "inference_pool", project)
        records = sorted(records, key=lambda row: row["body"]["pool"]["ref"])
        items = []
        for row in records[page * 12 : (page + 1) * 12]:
            body = row["body"]
            configured = all(
                ref in self.bindings
                and self.bindings[ref].project_ref == project
                and signature(self.bindings[ref]) == digest
                for ref, digest in body["bindings"].items()
            )
            items.append(
                {
                    **body["pool"],
                    "configured": configured,
                    "runtimes": sorted(
                        {
                            self.bindings[ref].runtime
                            for ref in body["bindings"]
                            if ref in self.bindings and self.bindings[ref].project_ref == project
                        }
                    ),
                }
            )
        return {
            "items": items,
            "page": page,
            "size": 12,
            "total": len(records),
            "has_next": (page + 1) * 12 < len(records),
        }

    def _pool(self, project, ref):
        with self.store.transaction() as conn:
            record = self.store.get(conn, "inference_pool", self.pool_key(project, ref))
        if record is None or record["project"] != project:
            raise NotFound("inference pool not found")
        body = record["body"]
        for endpoint, digest in body["bindings"].items():
            binding = self.bindings.get(endpoint)
            if not binding or binding.project_ref != project or signature(binding) != digest:
                raise ServingUnavailable(
                    "configured endpoint changed; register a new pool revision"
                )
        return body

    def view(self, project, ref):
        body = self._pool(project, ref)
        endpoints = []
        for endpoint in body["pool"]["endpoint_refs"]:
            self._refresh(endpoint)
            with self.lock:
                state = self.states[endpoint]
                endpoints.append(
                    {
                        "ref": endpoint,
                        "runtime": self.bindings[endpoint].runtime,
                        "ready": state.ready and state.blocked_until <= time.monotonic(),
                        "reason": state.reason,
                        "remote_inflight": state.remote_inflight,
                        "local_inflight": state.local_inflight,
                        "service_ms": state.service_ms,
                        "observed_ms": state.observed_ms,
                        "successes": state.successes,
                        "failures": state.failures,
                    }
                )
        return {
            **body["pool"],
            "input_contract": body["input_contract"],
            "output_contract": body["output_contract"],
            "endpoints": endpoints,
            "reservation_scope": "SINGLE_API_PROCESS",
            "health_cache_seconds": 1,
        }

    def _reserve(self, body, excluded, count, deadline):
        refs = [ref for ref in body["pool"]["endpoint_refs"] if ref not in excluded]
        for ref in refs:
            self._refresh(ref, deadline=deadline)
        with self.lock:
            options = []
            for ref in refs:
                state = self.states[ref]
                inflight = max(state.remote_inflight, state.local_inflight)
                if (
                    not state.ready
                    or state.blocked_until > time.monotonic()
                    or inflight >= state.max_inflight
                ):
                    continue
                service_ms = max(state.service_ms, state.observed_ms or 0)
                estimated_ms = (inflight + 1) * service_ms * count
                if estimated_ms > max(0, deadline - time.monotonic()) * 1000:
                    continue
                options.append((estimated_ms, inflight, ref))
            if not options or time.monotonic() >= deadline:
                raise ServingUnavailable("no ready qualified inference capacity within deadline")
            estimated_ms, inflight, ref = min(options)
            self.states[ref].local_inflight += 1
            return ref, estimated_ms, inflight

    def predict(self, project, ref, value):
        body = self._pool(project, ref)
        start = time.monotonic()
        deadline = start + value.deadline_ms / 1000
        payload = {"instances": value.model_dump(mode="json")["instances"]}
        excluded, attempts = set(), []
        for _ in range(min(2, len(body["pool"]["endpoint_refs"]))):
            endpoint, estimated_ms, inflight = self._reserve(
                body, excluded, len(value.instances), deadline
            )
            excluded.add(endpoint)
            binding = self.bindings[endpoint]
            attempt_start = time.monotonic()
            outcome = "UPSTREAM_FAILURE"
            answer = None
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("deadline expired")
                response = self.client.post(
                    binding.url + "/predict", json=payload, timeout=remaining
                )
                response.raise_for_status()
                output = response.json()
                predictions = output["predictions"]
                if (
                    not self._identity_matches(binding, output)
                    or len(predictions) != len(value.instances)
                    or any(
                        not isinstance(row, list)
                        or len(row) != 1000
                        or any(type(x) not in (int, float) or not math.isfinite(x) for x in row)
                        for row in predictions
                    )
                ):
                    outcome = "IDENTITY_OR_OUTPUT_MISMATCH"
                    raise ValueError("invalid prediction response")
                if time.monotonic() > deadline:
                    outcome = "DEADLINE_EXCEEDED"
                    raise ValueError("late inference result")
                outcome = "SUCCESS"
                duration_ms = (time.monotonic() - attempt_start) * 1000
                queue_seconds = output.get("queue_seconds", 0)
                if (
                    type(queue_seconds) not in (int, float)
                    or not math.isfinite(queue_seconds)
                    or queue_seconds < 0
                ):
                    outcome = "IDENTITY_OR_OUTPUT_MISMATCH"
                    raise ValueError("invalid queue timing")
                with self.lock:
                    state = self.states[endpoint]
                    state.successes += 1
                    queue_ms = queue_seconds * 1000
                    observed = max(0.001, duration_ms - queue_ms) / len(value.instances)
                    state.observed_ms = (
                        observed
                        if state.observed_ms is None
                        else 0.8 * state.observed_ms + 0.2 * observed
                    )
                attempts.append(
                    {"endpoint_ref": endpoint, "outcome": outcome, "duration_ms": duration_ms}
                )
                answer = {
                    "predictions": predictions,
                    "top1": [max(range(1000), key=row.__getitem__) for row in predictions],
                    "model": {
                        "model_ref": body["pool"]["model_ref"],
                        "source_model_sha256": body["pool"]["source_model_sha256"],
                    },
                    "routing": {
                        "endpoint_ref": endpoint,
                        "runtime": binding.runtime,
                        "estimated_ms": estimated_ms,
                        "inflight_at_selection": inflight,
                        "duration_ms": (time.monotonic() - start) * 1000,
                        "fallback": len(attempts) > 1,
                        "attempts": attempts,
                    },
                }
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                attempts.append(
                    {
                        "endpoint_ref": endpoint,
                        "outcome": outcome,
                        "duration_ms": (time.monotonic() - attempt_start) * 1000,
                    }
                )
                with self.lock:
                    state = self.states[endpoint]
                    state.failures += 1
                    state.blocked_until = time.monotonic() + 1
                    state.reason = outcome
            finally:
                with self.lock:
                    self.states[endpoint].local_inflight -= 1
            if answer is not None:
                if time.monotonic() > deadline:
                    with self.lock:
                        self.states[endpoint].successes -= 1
                        self.states[endpoint].failures += 1
                    raise ServingUnavailable("inference result exceeded deadline before return")
                return answer
        raise ServingUnavailable("qualified inference endpoints failed or deadline expired")
