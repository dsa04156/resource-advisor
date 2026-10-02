"""Independent source/grid collection through a scheduler double, never GPU evidence."""

import pytest
from test_transfer import run_study
from test_transfer import transfer_fixture as grid_fixture  # noqa: F401

from resource_advisor.contracts import StudyRequest
from resource_advisor.study import Studies
from resource_advisor.transfer_gpu_benchmark import validate


def test_grid_balances_every_candidate_and_confirms_all_without_optimizer(request, monkeypatch):
    fixture = request.getfixturevalue("grid_fixture")
    service = fixture[0]

    def forbidden(*args, **kwargs):
        pytest.fail("grid characterization must not fit or ask an optimizer")

    monkeypatch.setattr("resource_advisor.study.ask", forbidden)
    result = run_study(
        fixture, StudyRequest(workload_ref="source", strategy="grid_characterization", seed=11)
    )
    assert result["state"] == "COMPLETED"
    assert len(result["observations"]) == 15  # Two probes and three fresh confirmations per cell.
    for block in (0, 1):
        assert {s["candidate_ref"] for s in result["grid_schedule"] if s["block"] == block} == {
            "base",
            "middle",
            "other",
        }
    probes = [o for o in result["observations"] if o["mode"] == "pilot"]
    confirmations = [o for o in result["observations"] if o["mode"] == "confirmation"]
    assert {o["attempt_id"] for o in probes}.isdisjoint(o["attempt_id"] for o in confirmations)
    with service.store.transaction() as conn:
        profiles = service.store.list(conn, "profile", "team-a")
    assert len(profiles) == 9
    assert {p["body"]["candidate_ref"] for p in profiles} == {"base", "middle", "other"}


def test_grid_cancellation_does_not_submit_more_jobs(request):
    fixture = request.getfixturevalue("grid_fixture")
    service, _, _, worker, backend = fixture
    study = Studies(service).create(
        "team-a",
        StudyRequest(workload_ref="source", strategy="grid_characterization"),
        "cancel-grid",
    )
    Studies(service).tick(study["ref"])
    Studies(service).cancel("team-a", study["ref"])
    Studies(service).tick(study["ref"])
    worker.submit_one()
    assert backend.submissions == 0
    assert Studies(service).get("team-a", study["ref"])["state"] == "CANCELED"


@pytest.mark.parametrize("case", ["shape", "blocks", "precision", "shared", "extra", "cpu"])
def test_fixed_gpu_benchmark_refuses_unsupported_work(bundle, case):
    _, candidate, _, _ = bundle
    context = candidate.context.model_dump(mode="json")
    shape, blocks, precision = [1, 3, 128, 128], 12, "fp32"
    if case == "shape":
        shape[-1] = 129
    elif case == "blocks":
        blocks = 100
    elif case == "precision":
        precision = "fp16"
    elif case == "shared":
        context["allocation_mode"] = "virtual_slot"
    elif case == "extra":
        context["parameters"]["unapproved"] = 1
    else:
        context["resources"]["host_cpu"] = 3
    with pytest.raises(ValueError):
        validate(context, shape, blocks, precision)
