from datetime import timedelta

import pytest
from test_worker import SchedulerDouble

from resource_advisor.console import overview
from resource_advisor.contracts import JobRequest, now
from resource_advisor.service import Rejected, Service
from resource_advisor.worker import Worker


@pytest.mark.parametrize("ready", [True, False])
def test_operational_submission_keeps_evidence_and_worker_policy(bundle, database_store, ready):
    spec, _, variant, cap = bundle
    cap = cap.model_copy(update={"observed_at": now() - timedelta(days=7), "ready": ready})
    service = Service(database_store, operational_mode=True, console_workloads=(spec.ref,))
    for kind, model in [("workload", spec), ("variant", variant), ("capability", cap)]:
        service.register(kind, model, "team-a")
    request = JobRequest(workload_ref=spec.ref, candidate_ref=spec.baseline_candidate_ref)
    with pytest.raises(Rejected, match="CAPABILITY_STALE"):
        Service(database_store).submit("team-a", request, "strict")
    view = overview(service, "team-a")["submission_catalog"][0]["candidates"][0]
    assert not view["contract_compatible_now"]
    assert view["submittable_now"] == ready
    if not ready:
        with pytest.raises(Rejected, match="NODE_UNAVAILABLE"):
            service.submit("team-a", request, "manual")
        return
    job = service.submit("team-a", request, "manual")
    assert job["submission_warnings"] == ["CAPABILITY_STALE"]
    backend = SchedulerDouble()
    # Worker honors the recorded submission policy even with default service settings.
    worker = Worker(Service(database_store), {("team-a", "lab"): backend})
    assert worker.submit_one() and backend.submissions == 1
    assert service.get_job("team-a", job["job_id"])["state"] == "QUEUED"
    with database_store.transaction() as conn:
        assert database_store.get(conn, "capability", cap.ref)["body"][
            "observed_at"
        ] == cap.observed_at.isoformat().replace("+00:00", "Z")
