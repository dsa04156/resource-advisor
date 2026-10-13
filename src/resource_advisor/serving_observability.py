"""Project-scoped serving metrics; bounded input bins, never raw request rows."""

import bisect
import math
import threading
import time
from collections import Counter as Counts
from collections import deque
from dataclasses import dataclass, field

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

WINDOW = 1024
MIN_SAMPLES = 128


@dataclass
class Observation:
    deployment_ref: str
    revision: int
    model_uri: str
    edges: tuple
    reference: tuple
    rows: deque = field(default_factory=lambda: deque(maxlen=WINDOW))
    requests: Counts = field(default_factory=Counts)
    latency_seconds: float | None = None
    probe: dict | None = None
    started_at_seconds: float = field(default_factory=time.time)
    user_latency_seconds: float | None = None
    user_duration_seconds: float = 0


class ProjectMetrics:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "resource_advisor_model_requests_total",
            "Verified serving attempts",
            ["service_ref", "source", "outcome"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "resource_advisor_model_request_duration_seconds",
            "Serving attempt latency",
            ["service_ref", "source"],
            registry=self.registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
        )
        self.model = Gauge(
            "resource_advisor_model_info",
            "Current desired model",
            ["service_ref", "deployment_ref", "model_uri"],
            registry=self.registry,
        )
        self.probe_ok = Gauge(
            "resource_advisor_model_probe_success",
            "Last live probe succeeded (0 or 1)",
            ["service_ref"],
            registry=self.registry,
        )
        self.probe_time = Gauge(
            "resource_advisor_model_probe_timestamp_seconds",
            "Last completed live probe",
            ["service_ref"],
            registry=self.registry,
        )
        self.samples = Gauge(
            "resource_advisor_model_input_samples",
            "Rows in current deployment drift window",
            ["service_ref"],
            registry=self.registry,
        )
        self.psi = Gauge(
            "resource_advisor_model_input_psi",
            "Input population stability index",
            ["service_ref", "feature"],
            registry=self.registry,
        )
        self.services = {}


def baseline(rows):
    edges, reference = [], []
    for feature in range(2):
        values = sorted(row[feature] for row in rows)
        cuts = sorted({values[min(len(values) - 1, len(values) * n // 10)] for n in range(1, 10)})
        if values[0] == values[-1]:
            epsilon = max(abs(values[0]) * 0.01, 0.01)
            cuts = [values[0] - epsilon, values[0] + epsilon]
        counts = Counts(bisect.bisect_right(cuts, value) for value in values)
        edges.append(tuple(cuts))
        reference.append(tuple(counts[n] for n in range(len(cuts) + 1)))
    return tuple(edges), tuple(reference)


def psi(reference, observed):
    # Additive smoothing keeps empty bins finite, including out-of-range inputs.
    def proportions(counts):
        total = sum(counts) + 0.001 * len(counts)
        return [(count + 0.001) / total for count in counts]

    expected, actual = proportions(reference), proportions(observed)
    return sum((a - e) * math.log(a / e) for e, a in zip(expected, actual, strict=True))


class ServingTelemetry:
    def __init__(self, projects):
        self.projects = {project: ProjectMetrics() for project in projects}
        self.lock = threading.RLock()

    def configure(self, project, service, desired, training_rows):
        with self.lock:
            metrics = self.projects[project]
            previous = metrics.services.get(service)
            if previous and previous.revision >= desired["revision"]:
                return
            if previous:
                metrics.model.remove(service, previous.deployment_ref, previous.model_uri)
            edges, reference = baseline(training_rows)
            model_uri = desired["registration"]["model_uri"]
            metrics.services[service] = Observation(
                desired["ref"],
                desired["revision"],
                model_uri,
                edges,
                reference,
            )
            metrics.model.labels(service, desired["ref"], model_uri).set(1)
            metrics.probe_ok.labels(service).set(0)
            metrics.probe_time.labels(service).set(0)
            metrics.samples.labels(service).set(0)
            for source in ("request", "probe"):
                metrics.latency.labels(service, source)
                for outcome in ("success", "error", "conflict"):
                    metrics.requests.labels(service, source, outcome).inc(0)
            for feature in range(2):
                metrics.psi.labels(service, str(feature)).set(0)

    def record(self, project, service, deployment, source, outcome, seconds, rows):
        with self.lock:
            metrics = self.projects[project]
            metrics.requests.labels(service, source, outcome).inc()
            metrics.latency.labels(service, source).observe(seconds)
            observation = metrics.services[service]
            if observation.deployment_ref != deployment:
                return
            observation.requests[(source, outcome)] += 1
            observation.latency_seconds = seconds
            if source != "request":
                return
            observation.user_latency_seconds = seconds
            observation.user_duration_seconds += seconds
            observation.rows.extend(
                tuple(bisect.bisect_right(observation.edges[i], row[i]) for i in range(2))
                for row in rows
            )
            metrics.samples.labels(service).set(len(observation.rows))
            values = self._drift(observation)["psi"]
            for feature, value in enumerate(values or (0, 0)):
                metrics.psi.labels(service, str(feature)).set(value)

    def record_probe(self, project, service, deployment, success, seconds, reason):
        with self.lock:
            metrics = self.projects[project]
            observation = metrics.services[service]
            if observation.deployment_ref != deployment:
                return
            timestamp = time.time()
            observation.probe = {
                "success": success,
                "timestamp_seconds": timestamp,
                "duration_seconds": seconds,
                "reason": reason,
                "deployment_ref": deployment,
            }
            metrics.probe_ok.labels(service).set(int(success))
            metrics.probe_time.labels(service).set(timestamp)

    @staticmethod
    def _drift(observation):
        samples = len(observation.rows)
        values = None
        if samples >= MIN_SAMPLES:
            values = []
            for feature, reference in enumerate(observation.reference):
                counts = Counts(row[feature] for row in observation.rows)
                values.append(psi(reference, [counts[i] for i in range(len(reference))]))
        return {
            "state": "AVAILABLE" if values is not None else "INSUFFICIENT_DATA",
            "samples": samples,
            "minimum_samples": MIN_SAMPLES,
            "window_rows": WINDOW,
            "psi": values,
        }

    def snapshot(self, project, service):
        with self.lock:
            observation = self.projects[project].services[service]
            user_counts = {
                outcome: observation.requests[("request", outcome)]
                for outcome in ("success", "error", "conflict")
            }
            attempts = sum(user_counts.values())
            return {
                "deployment_ref": observation.deployment_ref,
                "model_uri": observation.model_uri,
                "requests": [
                    {"source": source, "outcome": outcome, "count": count}
                    for (source, outcome), count in sorted(observation.requests.items())
                ],
                "last_attempt_duration_seconds": observation.latency_seconds,
                "probe": observation.probe,
                "observed_since_seconds": observation.started_at_seconds,
                "user_requests": {
                    "attempts": attempts,
                    **user_counts,
                    "mean_duration_seconds": (
                        observation.user_duration_seconds / attempts if attempts else None
                    ),
                    "last_duration_seconds": observation.user_latency_seconds,
                },
                "input_drift": self._drift(observation),
                "storage": "API_PROCESS_MEMORY",
            }

    def exposition(self, project):
        with self.lock:
            return generate_latest(self.projects[project].registry)
