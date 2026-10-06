import httpx
import pytest
from test_scheduling import profile

from resource_advisor.research import ResearchServices, UpstreamUnavailable
from resource_advisor.service import NotFound
from resource_advisor.store import Conflict


def config():
    return {
        "projects": {
            "team-a": {
                "mlflow": {"url": "https://tracking.invalid", "experiment_ids": ["owned"]},
                "kubeflow": {
                    "url": "https://pipelines.invalid",
                    "namespace": "research-a",
                    "experiment_ids": ["owned"],
                    "templates": {"auto": {"name": "Auto", "parameters": {}, "pipeline_spec": {}}},
                },
                "notebooks": {
                    "url": "https://cluster.invalid",
                    "namespace": "research-a",
                    "ui_url": "https://notebook.invalid",
                },
            }
        }
    }


def test_mlflow_scope_checked_before_writing_notes_or_listing_artifacts(database_store):
    calls = []

    def server(request):
        calls.append(request.method)
        return httpx.Response(
            200, json={"run": {"info": {"run_id": "foreign", "experiment_id": "other"}}}
        )

    api = ResearchServices(database_store, config(), httpx.MockTransport(server))
    with pytest.raises(NotFound):
        api.note("team-a", "foreign", "must not be written")
    with pytest.raises(NotFound):
        api.artifacts("team-a", "foreign")
    assert calls == ["GET", "GET"]


def test_kfp_scope_checked_before_termination(database_store):
    calls = []

    def server(request):
        calls.append(request.method)
        return httpx.Response(200, json={"experiment_id": "other", "state": "RUNNING"})

    api = ResearchServices(database_store, config(), httpx.MockTransport(server))
    with pytest.raises(NotFound):
        api.terminate("team-a", "foreign")
    assert calls == ["GET"]


def test_notebook_stop_uses_scoped_versioned_patch(database_store):
    import json

    def server(request):
        assert request.url.path == "/apis/kubeflow.org/v1/namespaces/research-a/notebooks/starter"
        if request.method == "GET":
            return httpx.Response(200, json={"metadata": {"resourceVersion": "17"}})
        body = json.loads(request.content)
        assert body["metadata"]["resourceVersion"] == "17"
        assert body["metadata"]["annotations"]["kubeflow-resource-stopped"].endswith("Z")
        assert request.headers["content-type"] == "application/merge-patch+json"
        return httpx.Response(200, json={})

    api = ResearchServices(database_store, config(), httpx.MockTransport(server))
    assert api.notebook_action("team-a", "starter", "stop")["requested"] == "stop"


def test_upstream_outage_is_not_reported_as_empty_healthy_service(database_store):
    def server(request):
        raise httpx.ConnectError("secret internal address", request=request)

    api = ResearchServices(database_store, config(), httpx.MockTransport(server))
    view = api.overview("team-a")
    assert all(view[k]["status"] == "unavailable" for k in ["mlflow", "kubeflow", "notebooks"])
    assert "secret" not in str(view)
    assert api.overview("team-b")["mlflow"]["status"] == "unconfigured"


@pytest.mark.parametrize("lost_response", [False, True])
def test_pipeline_create_idempotency_survives_uncertain_response(service, lost_response):
    service.register("scheduling_profile", profile(), "team-a")
    calls = []

    def server(request):
        calls.append(request)
        if lost_response:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json={"run_id": "run-1", "state": "PENDING"})

    api = ResearchServices(service.store, config(), httpx.MockTransport(server))
    args = (service, "team-a", "auto", "workload-1", "interactive-v1", "one")
    if lost_response:
        with pytest.raises(UpstreamUnavailable):
            api.launch(*args)
        with pytest.raises(Conflict, match="unresolved"):
            api.launch(*args)
    else:
        assert api.launch(*args)["run_id"] == "run-1"
        assert api.launch(*args)["run_id"] == "run-1"
    assert len(calls) == 1
