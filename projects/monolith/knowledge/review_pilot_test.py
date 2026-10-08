"""Submission fences survive races, retries, GC and unknown Kubernetes outcomes."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlmodel import Session, create_engine, select

from knowledge import review_pilot as pilot
from knowledge.models import ReviewPilotRun

DRY = "knowledge-review-admission-dry-run"
APPLY = "knowledge-review-admission"


def crons():
    items = []
    for name in pilot.CONTROL_JOBS:
        args, deadline = pilot.JOBS.get(
            name, (["knowledge-review-backfill", "--apply", "--pending-only"], 300)
        )
        items.append(
            {
                "metadata": {"name": name, "namespace": pilot.NAMESPACE},
                "spec": {
                    "suspend": True,
                    "schedules": ["*/15 * * * *"],
                    "concurrencyPolicy": "Forbid",
                    "workflowSpec": {
                        "entrypoint": "run",
                        "activeDeadlineSeconds": deadline,
                        "synchronization": deepcopy(pilot.MUTEX),
                        "templates": [
                            {
                                "name": "run",
                                "container": {
                                    "args": args,
                                    "env": [
                                        {"name": "PRIVATE", "value": "must-not-leak"}
                                    ],
                                },
                                "outputs": {
                                    "parameters": [
                                        {
                                            "name": "review-report",
                                            "valueFrom": {
                                                "path": "/tmp/knowledge-review-report.json"
                                            },
                                        }
                                    ]
                                },
                            }
                        ],
                    },
                },
            }
        )
    return items


class Cluster:
    def __init__(self):
        self.crons = crons()
        self.workflows = {}
        self.creates = 0
        self.fail_after_create = False

    async def list_cronworkflows(self, namespace):
        assert namespace == pilot.NAMESPACE
        return self.crons

    async def list_workflows(self, namespace):
        assert namespace == pilot.NAMESPACE
        return list(self.workflows.values())

    async def get_workflow(self, namespace, name):
        assert namespace == pilot.NAMESPACE
        return self.workflows.get(name)

    async def create_workflow(self, namespace, manifest):
        assert namespace == pilot.NAMESPACE
        self.creates += 1
        name = manifest["metadata"]["name"]
        assert name not in self.workflows
        self.workflows[name] = deepcopy(manifest)
        if self.fail_after_create:
            raise TimeoutError()
        return name

    async def close(self):
        pass

    def complete(self, receipt, *, phase="Succeeded", report=None):
        workflow = self.workflows[receipt["workflow_name"]]
        workflow["status"] = {
            "phase": phase,
            "nodes": {
                "run": {
                    "templateName": "run",
                    "outputs": {
                        "parameters": [
                            {
                                "name": "review-report",
                                "value": json.dumps(
                                    report
                                    or {
                                        "dry_run": True,
                                        "candidates": 1,
                                        "note_ids": ["actual-note"],
                                    }
                                ),
                            }
                        ]
                    },
                }
            },
        }


@pytest.fixture
def lane(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'pilot.db'}").execution_options(
        schema_translate_map={"knowledge": None}
    )
    ReviewPilotRun.__table__.create(engine)
    cluster = Cluster()
    monkeypatch.setattr(pilot, "get_engine", lambda: engine)
    monkeypatch.setattr(pilot, "KubernetesClient", lambda: cluster)
    monkeypatch.setenv("SCHEDULER_WORKFLOW_NAMESPACE", pilot.NAMESPACE)
    yield engine, cluster
    engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_submit_and_gc_never_recreate(lane):
    _, cluster = lane
    key = str(uuid4())
    receipt = await pilot.submit(DRY, key, "operator")
    assert (await pilot.submit(DRY, key, "operator"))["workflow_name"] == receipt[
        "workflow_name"
    ]
    cluster.complete(receipt)
    inspected = await pilot.inspect(key)
    assert inspected["receipt"]["active"] is False
    assert "must-not-leak" not in json.dumps(inspected)
    cluster.workflows.clear()
    replay = await pilot.submit(DRY, key, "operator")
    assert replay["result"]["phase"] == "Succeeded"
    assert cluster.creates == 1


@pytest.mark.asyncio
async def test_unknown_create_remains_fenced_and_readback_recovers(lane):
    _, cluster = lane
    cluster.fail_after_create = True
    key = str(uuid4())
    receipt = await pilot.submit(DRY, key, "operator")
    assert receipt["result"]["phase"] == "Unknown"
    await pilot.submit(DRY, key, "operator")
    assert cluster.creates == 1
    saved = cluster.workflows.pop(receipt["workflow_name"])
    assert (await pilot.inspect(key))["receipt"]["active"]
    with pytest.raises(ValueError, match="active or uncertain"):
        await pilot.submit(DRY, str(uuid4()), "operator")
    cluster.workflows[receipt["workflow_name"]] = saved
    cluster.complete(receipt)
    assert not (await pilot.inspect(key))["receipt"]["active"]


@pytest.mark.asyncio
async def test_apply_requires_fresh_nonempty_matching_dry_run_and_consumes_once(lane):
    _, cluster = lane
    with pytest.raises(ValueError, match="matching completed"):
        await pilot.submit(APPLY, str(uuid4()), "operator")
    key = str(uuid4())
    receipt = await pilot.submit(DRY, key, "operator")
    cluster.complete(receipt)
    await pilot.inspect(key)
    applied = await pilot.submit(APPLY, str(uuid4()), "operator", key)
    assert cluster.creates == 2
    cluster.complete(applied, report={"dry_run": False, "renewed": 1, "requests": 1})
    await pilot.inspect(applied["request_id"])
    with pytest.raises(ValueError, match="duplicate"):
        await pilot.submit(APPLY, str(uuid4()), "operator", key)


@pytest.mark.parametrize(
    "report,age",
    [
        ({"dry_run": True, "candidates": 0}, 0),
        ({"dry_run": True, "candidates": 21}, 0),
        ({"dry_run": True, "candidates": 1}, 16),
        ({"dry_run": False, "candidates": 1}, 0),
    ],
)
def test_bad_dry_run_cannot_authorize_application(lane, report, age):
    engine, _ = lane
    key = str(uuid4())
    with Session(engine) as session:
        session.add(
            ReviewPilotRun(
                request_id=key,
                job=DRY,
                actor="operator",
                workflow_name="old",
                active_slot=None,
                created_at=datetime.now(timezone.utc) - timedelta(minutes=age),
                result={"phase": "Succeeded", "report": report},
            )
        )
        session.commit()
    with pytest.raises(ValueError, match="successful, nonempty"):
        pilot._reserve(APPLY, str(uuid4()), "operator", key)


def test_concurrent_reservations_have_one_durable_slot(lane):
    engine, _ = lane

    def reserve(_):
        try:
            return pilot._reserve(DRY, str(uuid4()), "operator", None)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(reserve, range(4)))
    assert sum(r is not None for r in results) == 1
    with Session(engine) as session:
        assert len(session.exec(select(ReviewPilotRun)).all()) == 1


@pytest.mark.parametrize(
    "change", ["args", "deadline", "suspend", "mutex", "retry", "namespace", "outputs"]
)
def test_template_drift_fails_closed(change):
    items = crons()
    item = next(i for i in items if i["metadata"]["name"] == APPLY)
    spec = item["spec"]["workflowSpec"]
    if change == "args":
        spec["templates"][0]["container"]["args"] = ["anything"]
    if change == "deadline":
        spec["activeDeadlineSeconds"] = 301
    if change == "suspend":
        item["spec"]["suspend"] = False
    if change == "mutex":
        spec["synchronization"] = {}
    if change == "retry":
        spec["retryStrategy"] = {"limit": 1}
    if change == "namespace":
        item["metadata"]["namespace"] = "other"
    if change == "outputs":
        spec["templates"][0]["outputs"] = {}
    with pytest.raises(ValueError):
        pilot.checked_spec(APPLY, items)


@pytest.mark.asyncio
async def test_rejects_unknown_job_noncanonical_key_and_wrong_environment(
    lane, monkeypatch
):
    _, cluster = lane
    for job, key in [("knowledge-review-backfill", str(uuid4())), (DRY, "not-a-uuid")]:
        with pytest.raises(ValueError):
            await pilot.submit(job, key, "operator")
    monkeypatch.setenv("SCHEDULER_WORKFLOW_NAMESPACE", "monolith-dev")
    with pytest.raises(ValueError):
        await pilot.submit(DRY, str(uuid4()), "operator")
    assert cluster.creates == 0


@pytest.mark.asyncio
async def test_out_of_band_review_workflow_blocks_submission(lane):
    _, cluster = lane
    cluster.workflows["manual"] = {
        "metadata": {"labels": {"workflows.argoproj.io/cron-workflow": APPLY}},
        "status": {"phase": "Running"},
    }
    with pytest.raises(ValueError, match="already pending"):
        await pilot.submit(DRY, str(uuid4()), "operator")
    assert cluster.creates == 0


@pytest.mark.asyncio
async def test_identity_mismatch_cannot_release_fence(lane):
    _, cluster = lane
    key = str(uuid4())
    receipt = await pilot.submit(DRY, key, "operator")
    cluster.complete(receipt)
    cluster.workflows[receipt["workflow_name"]]["metadata"]["labels"][
        "monolith.jomcgi.dev/review-job"
    ] = "other"
    with pytest.raises(ValueError, match="identity"):
        await pilot.inspect(key)
    assert pilot._read(key)["active"]
