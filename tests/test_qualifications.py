import copy
import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from resource_advisor.api import Principal, create_app
from resource_advisor.contracts import now, signature
from resource_advisor.qualifications import QualificationImport, Qualifications
from resource_advisor.store import entities, jobs, outbox, usage


def payload():
    # Synthetic fixture whose gate disagreement mirrors the real boundary case.
    at = now() - timedelta(minutes=1)
    return {
        "ref": "qual-1",
        "project_ref": "team-a",
        "evidence_kind": "synthetic",
        "model_name": "classification-test",
        "accelerator_model": "fixture-npu",
        "device_class": "npu",
        **{
            key: signature(key)
            for key in [
                "model_digest",
                "compiled_artifact_digest",
                "image_digest",
                "runner_digest",
                "input_manifest_digest",
                "source_report_digest",
            ]
        },
        "plan_ref": "fixed-test-plan",
        "runtime_versions": {"runtime": "test-v1"},
        "backend": "kubernetes",
        "external_job_ref": "external-test-job",
        "external_job_uid": "external-test-uid",
        "started_at": at.isoformat(),
        "finished_at": (at + timedelta(seconds=4)).isoformat(),
        "container_exit_code": 2,
        "measurement_boundary": "test-only",
        "host_process_peak_rss_mib": 96,
        "expected_images": 100,
        "gates": {
            "minimum_accuracy": 0.75,
            "minimum_reference_agreement": 0.90,
            "maximum_accuracy_loss": 0.05,
        },
        "samples": [
            {
                "input_digest": signature(["input", i]),
                "output_digest": signature(["output", i]),
                "label": 0,
                "reference_top1": 0 if i < 81 else 1,
                "prediction": 0 if i < 77 else (2 if i < 88 else 1),
                "elapsed_seconds": 0.02,
            }
            for i in range(100)
        ],
    }


def client(service):
    credentials = {
        hashlib.sha256(key.encode()).hexdigest(): Principal(project, operator)
        for key, project, operator in [
            ("operator", "team-a", True),
            ("reader", "team-a", False),
            ("other", "team-b", True),
        ]
    }
    return TestClient(create_app(service, credentials))


def headers(role="operator"):
    return {"Authorization": "Bearer " + role}


def counts(service):
    with service.store.transaction() as conn:
        return [
            conn.execute(select(func.count()).select_from(t)).scalar_one()
            for t in (entities, jobs, usage, outbox)
        ]


def test_import_recomputes_failed_gate_and_does_not_create_execution_or_usage(service):
    before = counts(service)
    report = payload()
    with client(service) as c:
        response = c.post("/api/v1/compute/qualifications", json=report, headers=headers())
        assert response.status_code == 200
        value = response.json()
        a = value["assessment"]
        assert a["accuracy"] == 0.77 and a["reference_agreement"] == 0.89
        assert a["reference_accuracy"] == 0.81
        assert a["failed_checks"] == ["minimum_reference_agreement"]
        assert a["inference_completed"] is True and a["quality_passed"] is False
        assert a["recommendation_authorized"] is False
        assert value["container_exit_code"] == 2 and "samples" not in value
        replay = c.post("/api/v1/compute/qualifications", json=report, headers=headers())
        assert replay.json() == value
        read = c.get("/api/v1/compute/qualifications/qual-1", headers=headers("reader"))
        assert len(read.json()["samples"]) == 100
        assert read.headers["cache-control"] == "no-store"
    after = counts(service)
    assert after == [before[0] + 1, *before[1:]]


def test_authorization_scope_and_immutable_conflict(service):
    with client(service) as c:
        url = "/api/v1/compute/qualifications"
        report = payload()
        assert c.post(url, json=report).status_code == 401
        assert c.post(url, json=report, headers=headers("reader")).status_code == 403
        assert c.post(url, json=report, headers=headers("other")).status_code == 422
        assert c.post(url, json=report, headers=headers()).status_code == 200
        assert c.get(url + "/qual-1", headers=headers("other")).status_code == 404
        assert c.get(url, headers=headers("other")).json()["total"] == 0
        report["samples"][0]["prediction"] = 999
        assert c.post(url, json=report, headers=headers()).status_code == 409
        assert c.get(url + "/qual-1", headers=headers()).json()["samples"][0]["prediction"] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(assessment={"quality_passed": True}),
        lambda p: p.update(expected_images=99),
        lambda p: p["samples"][0].update(input_digest=p["samples"][1]["input_digest"]),
        lambda p: p["samples"][0].update(prediction=1000),
        lambda p: p["samples"][0].update(prediction=True),
        lambda p: p["samples"][0].update(elapsed_seconds=0),
        lambda p: p["samples"][0].update(elapsed_seconds="NaN"),
        lambda p: p.update(finished_at=(now() + timedelta(hours=1)).isoformat()),
        lambda p: p.update(finished_at=(now() - timedelta(days=1)).isoformat()),
        lambda p: p.update(runtime_versions={"version": "x" * 129}),
    ],
)
def test_incomplete_forged_or_invalid_evidence_is_rejected_without_mutation(service, mutation):
    before = counts(service)
    report = payload()
    mutation(report)
    with client(service) as c:
        response = c.post("/api/v1/compute/qualifications", json=report, headers=headers())
        assert response.status_code == 422
    assert counts(service) == before


def test_passing_import_still_does_not_promote_a_variant(service):
    report = payload()
    report["container_exit_code"] = 0
    for sample in report["samples"]:
        sample.update(prediction=0, reference_top1=0)
    q = Qualifications(service.store)
    before = counts(service)
    value = q.create("team-a", QualificationImport.model_validate(report))
    assert value["assessment"]["quality_passed"] is True
    assert value["assessment"]["recommendation_authorized"] is False
    assert counts(service) == [before[0] + 1, *before[1:]]


def test_paged_overview_omits_samples_and_is_project_scoped(service):
    q = Qualifications(service.store)
    base = payload()
    for index in range(26):
        report = copy.deepcopy(base)
        report["ref"] = f"qual-{index}"
        q.create("team-a", QualificationImport.model_validate(report))
    with client(service) as c:
        first = c.get("/api/v1/compute/overview", headers=headers("reader")).json()
        page = first["qualifications"]
        assert page["total"] == 26 and len(page["items"]) == 25 and page["has_next"]
        assert all("samples" not in row for row in page["items"])
        second = c.get("/api/v1/compute/overview?qualifications_page=1", headers=headers()).json()
        assert len(second["qualifications"]["items"]) == 1
        other = c.get("/api/v1/compute/overview", headers=headers("other")).json()
        assert other["qualifications"]["total"] == 0
        assert c.get("/api/v1/compute/qualifications?page=-1", headers=headers()).status_code == 422
