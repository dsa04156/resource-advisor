import httpx
import pytest
from test_scheduling import profile

from resource_advisor.research import ResearchServices, UpstreamUnavailable
from resource_advisor.service import NotFound
from resource_advisor.store import Conflict


def pipeline_job(store, ref, key, project="team-a", device="gpu"):
    from sqlalchemy import insert

    from resource_advisor.store import jobs

    with store.transaction() as conn:
        conn.execute(
            insert(jobs).values(
                id=ref,
                project=project,
                idempotency_key=key,
                state="SUCCEEDED",
                epoch=1,
                version=1,
                body={
                    "created_at": "2026-10-06T17:05:00Z",
                    "attempt_id": "attempt-" + ref,
                    "candidate": {"backend": "kubernetes"},
                    "variant": {"device_class": device},
                    "spec": {"ref": "workload-" + device},
                },
            )
        )


def test_pipeline_links_each_declared_stage_with_project_scope(database_store):
    pipeline_job(database_store, "cpu-job", "cpu-key", device="cpu")
    pipeline_job(database_store, "gpu-job", "gpu-key")
    pipeline_job(database_store, "foreign-job", "cpu-key", project="team-b")
    api = ResearchServices(database_store, config())
    run = {
        "runtime_config": {"parameters": {"cpu_run_key": "cpu-key", "gpu_run_key": "gpu-key"}},
        "pipeline_spec": {
            "pipeline_spec": {
                "root": {
                    "dag": {
                        "tasks": {
                            "cpu": {
                                "taskInfo": {"name": "CPU result"},
                                "inputs": {
                                    "parameters": {
                                        "run_key": {"componentInputParameter": "cpu_run_key"}
                                    }
                                },
                            },
                            "gpu": {
                                "dependentTasks": ["cpu"],
                                "inputs": {
                                    "parameters": {
                                        "run_key": {"componentInputParameter": "gpu_run_key"}
                                    }
                                },
                            },
                        }
                    }
                }
            }
        },
    }
    view = api.pipeline_view("team-a", run)
    assert view["job_id"] is None  # Neither stage pretends to be the only Job.
    assert [(j["task_name"], j["job_id"], j["device_class"]) for j in view["linked_jobs"]] == [
        ("cpu", "cpu-job", "cpu"),
        ("gpu", "gpu-job", "gpu"),
    ]
    assert view["graph"][0]["display_name"] == "CPU result"
    assert view["graph"][1]["dependencies"] == ["cpu"]
    assert api.pipeline_view("team-b", run)["linked_jobs"][0]["job_id"] == "foreign-job"
    assert len(api.pipeline_view("team-b", run)["linked_jobs"]) == 1


def test_pipeline_does_not_guess_links_from_parameter_names(database_store):
    pipeline_job(database_store, "existing", "known")
    api = ResearchServices(database_store, config())
    run = {
        "runtime_config": {"parameters": {"cpu_run_key": "known"}},
        "pipeline_spec": {
            "root": {
                "dag": {
                    "tasks": {
                        "dynamic": {
                            "inputs": {"parameters": {"run_key": {"taskOutputParameter": {}}}}
                        }
                    }
                }
            }
        },
    }
    view = api.pipeline_view("team-a", run)
    assert view["linked_jobs"] == [] and view["job_id"] is None


def test_pipeline_constant_and_legacy_single_key_links(database_store):
    pipeline_job(database_store, "existing", "known")
    api = ResearchServices(database_store, config())
    legacy = api.pipeline_view("team-a", {"runtime_config": {"parameters": {"run_key": "known"}}})
    assert legacy["job_id"] == "existing"
    assert legacy["linked_jobs"] == []
    view = api.pipeline_view(
        "team-a",
        {
            "pipeline_spec": {
                "root": {
                    "dag": {
                        "tasks": {
                            "constant": {
                                "inputs": {
                                    "parameters": {
                                        "run_key": {"runtimeValue": {"constant": "known"}}
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
    )
    assert view["job_id"] == "existing"
    assert view["linked_jobs"][0]["task_name"] == "constant"


def test_pipeline_retry_cannot_backfill_prior_failed_workflow(database_store):
    pipeline_job(database_store, "later-job", "reused-key")
    api = ResearchServices(database_store, config())
    run = {
        "state": "FAILED",
        "finished_at": "2026-10-06T17:03:25Z",
        "runtime_config": {"parameters": {"run_key": "reused-key"}},
        "pipeline_spec": {
            "root": {
                "dag": {
                    "tasks": {
                        "launch": {
                            "inputs": {
                                "parameters": {"run_key": {"componentInputParameter": "run_key"}}
                            }
                        }
                    }
                }
            }
        },
    }
    assert api.pipeline_view("team-a", run)["linked_jobs"] == []
    assert api.pipeline_view("team-a", run)["job_id"] is None
    # An actual later replay may legitimately refer to an earlier compute Job.
    run["state"] = "SUCCEEDED"
    run["finished_at"] = "2026-10-06T17:06:00Z"
    assert api.pipeline_view("team-a", run)["linked_jobs"][0]["job_id"] == "later-job"
    run["run_details"] = {"task_details": [{"display_name": "launch", "state": "SKIPPED"}]}
    assert api.pipeline_view("team-a", run)["linked_jobs"] == []
    assert api.pipeline_view("team-a", run)["job_id"] is None
    run["run_details"] = {
        "task_details": [
            {"display_name": "launch", "state": "FAILED", "end_time": "2026-10-06T17:04:00Z"}
        ]
    }
    assert api.pipeline_view("team-a", run)["linked_jobs"] == []
    assert api.pipeline_view("team-a", run)["job_id"] is None


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
