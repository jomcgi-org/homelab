"""Publication and live revision evidence, with no network or cluster access."""

from copy import deepcopy
import base64

import pytest

from factory.orchestration import factory_rollout as rollout

MERGE, SOURCE, RECEIPT = "a" * 40, "b" * 40, "c" * 40
IMAGE = "ghcr.io/jomcgi/homelab/projects/demo/backend@sha256:" + "d" * 64


def commit(message=None):
    return {
        "sha": RECEIPT,
        "commit": {
            "author": {"email": "chart-version-bot@users.noreply.github.com"},
            "message": message
            or (
                "chore(charts): publish 0 chart version(s)\n\n"
                f"Chart-Source-Commit: {SOURCE}\n"
                "Chart-Publication-Complete: true\n"
                "Chart-Published: projects/demo/chart 1.2.3\n"
            ),
        },
    }


def github(_repo, path):
    if path.startswith("compare/"):
        return {"status": "ahead"}
    if path.endswith("/status"):
        return {"statuses": [{"context": "pr-checks", "state": "success"}]}
    if path.startswith("contents/"):
        return {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(b"name: demo\nversion: 1.2.2\n").decode(),
        }
    raise AssertionError(path)


def snapshot():
    app_sources = [
        {"repoURL": rollout.REGISTRY, "chart": "demo", "targetRevision": "1.2.3"},
        {
            "repoURL": "https://github.com/owner/repo.git",
            "targetRevision": "HEAD",
            "ref": "values",
        },
    ]
    app = {
        "metadata": {"name": "demo", "uid": "app-uid"},
        "spec": {"sources": deepcopy(app_sources)},
        "status": {
            "health": {"status": "Healthy"},
            "sync": {
                "status": "Synced",
                "revisions": ["1.2.3", SOURCE],
                "comparedTo": {"sources": app_sources},
            },
            "resources": [
                {"kind": "Deployment", "namespace": "demo", "name": "backend"}
            ],
        },
    }
    workload = {
        "kind": "Deployment",
        "metadata": {"name": "backend", "namespace": "demo", "generation": 2},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "demo"}},
            "template": {"spec": {"containers": [{"name": "backend", "image": IMAGE}]}},
        },
        "status": {
            "observedGeneration": 2,
            "replicas": 1,
            "updatedReplicas": 1,
            "readyReplicas": 1,
        },
    }
    pod = {
        "metadata": {"name": "backend-new", "labels": {"app": "demo"}},
        "spec": {"containers": [{"name": "backend", "image": IMAGE}]},
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [
                {
                    "name": "backend",
                    "ready": True,
                    "imageID": IMAGE,
                    "state": {"running": {"startedAt": "2026-09-22T10:00:00Z"}},
                }
            ],
        },
    }
    return {
        "applications": [app],
        "workloads": {("Deployment", "demo", "backend"): workload},
        "pods": {"demo": [pod]},
    }


def verify(state=None, get=github, commits=None):
    return rollout.verify(
        "owner/repo",
        MERGE,
        get=get,
        listing=lambda *_: [commit()] if commits is None else commits,
        snapshot=snapshot() if state is None else state,
    )


def test_noop_publication_proves_reused_chart_and_running_digest():
    result = verify()
    assert result["verified"] is True
    assert result["merge_commit_sha"] == MERGE
    assert result["source_commit_sha"] == SOURCE
    assert result["publication_commit_sha"] == RECEIPT
    assert result["applications"][0]["workloads"][0]["image_ids"] == [IMAGE]


@pytest.mark.parametrize(
    "line",
    [
        "Chart-Published: projects/../demo 1.2.3",
        "Chart-Published: projects/demo/chart 1.2.3 extra",
        "Chart-Published: projects/demo/chart 1.2.3",
        "Chart-Source-Commit: " + MERGE,
    ],
)
def test_malformed_or_duplicate_receipt_never_verifies(line):
    value = commit()
    value["commit"]["message"] += line + "\n"
    assert verify(commits=[value])["verified"] is False


def test_untrusted_author_and_missing_receipt_never_become_noop_success():
    value = commit()
    value["commit"]["author"]["email"] = "worker@example.com"
    assert verify(commits=[value])["reason"] == "publication_receipt_missing"
    assert verify(commits=[])["reason"] == "publication_receipt_missing"


def test_receipt_search_exhaustion_does_not_assume_publication():
    calls = []

    def listing(_repo, path):
        calls.append(path)
        return [{"sha": SOURCE, "commit": {"message": "ordinary commit"}}] * 100

    result = rollout.verify(
        "owner/repo", MERGE, get=github, listing=listing, snapshot=snapshot()
    )
    assert result["reason"] == "publication_receipt_missing"
    assert len(calls) == 2


@pytest.mark.parametrize("state", ["pending", "failure", "error"])
def test_publication_requires_successful_source_ci(state):
    def get(repo, path):
        if path.endswith("/status"):
            return {"statuses": [{"context": "pr-checks", "state": state}]}
        return github(repo, path)

    assert verify(get=get)["reason"] == "publication_ci_not_successful"


def test_publication_source_must_include_merge_and_be_on_main():
    def unrelated(repo, path):
        return (
            {"status": "diverged"}
            if path.startswith("compare/")
            else github(repo, path)
        )

    assert verify(get=unrelated)["reason"] == "publication_receipt_missing"

    def off_main(repo, path):
        if path.startswith(f"compare/{SOURCE}...{RECEIPT}"):
            return {"status": "diverged"}
        return github(repo, path)

    assert verify(get=off_main)["reason"] == "publication_source_not_on_main"


@pytest.mark.parametrize(
    "change,reason",
    [
        (lambda a: a["status"]["sync"].pop("revisions"), "deployed_revision_missing"),
        (
            lambda a: a["status"]["sync"]["revisions"].__setitem__(0, "1.2.2"),
            "published_chart_not_deployed",
        ),
        (
            lambda a: a["status"]["sync"]["revisions"].__setitem__(0, "1.2.4"),
            "published_chart_not_deployed",
        ),
        (
            lambda a: a["status"]["sync"].__setitem__("status", "OutOfSync"),
            "application_not_healthy_and_synced",
        ),
        (
            lambda a: a["status"]["health"].__setitem__("status", "Degraded"),
            "application_not_healthy_and_synced",
        ),
        (
            lambda a: a["spec"]["sources"][0].__setitem__("targetRevision", "1.2.4"),
            "application_comparison_stale",
        ),
        (lambda a: a["status"].pop("resources"), "application_resources_missing"),
    ],
)
def test_healthy_old_or_partial_application_is_not_deployed(change, reason):
    state = snapshot()
    change(state["applications"][0])
    assert verify(state)["reason"] == reason


def test_default_false_helm_flag_does_not_make_current_comparison_stale():
    state = snapshot()
    state["applications"][0]["spec"]["sources"][0]["helm"] = {
        "ignoreMissingValueFiles": False
    }
    assert verify(state)["verified"] is True


def test_older_git_values_cannot_pass_even_when_chart_is_current():
    state = snapshot()
    state["applications"][0]["status"]["sync"]["revisions"][1] = "e" * 40

    def get(repo, path):
        if path.startswith(f"compare/{SOURCE}...{'e' * 40}"):
            return {"status": "behind"}
        return github(repo, path)

    assert verify(state, get=get)["reason"] == "git_revision_not_deployed"


@pytest.mark.parametrize(
    "change,reason",
    [
        (
            lambda w: w["status"].__setitem__("observedGeneration", 1),
            "workload_generation_unobserved",
        ),
        (lambda w: w["status"].__setitem__("updatedReplicas", 0), "workload_not_ready"),
        (lambda w: w["status"].__setitem__("replicas", 2), "workload_not_ready"),
        (lambda w: w["status"].__setitem__("readyReplicas", 0), "workload_not_ready"),
    ],
)
def test_workload_rollout_must_be_complete(change, reason):
    state = snapshot()
    change(next(iter(state["workloads"].values())))
    assert verify(state)["reason"] == reason


@pytest.mark.parametrize(
    "change,reason",
    [
        (
            lambda p: p["metadata"].__setitem__("deletionTimestamp", "now"),
            "pod_not_running",
        ),
        (
            lambda p: p["status"]["conditions"][0].__setitem__("status", "False"),
            "pod_not_ready",
        ),
        (
            lambda p: p["status"]["containerStatuses"][0].__setitem__("imageID", ""),
            "running_image_unconfirmed",
        ),
        (
            lambda p: p["status"]["containerStatuses"][0].__setitem__(
                "imageID", "sha256:" + "e" * 64
            ),
            "running_digest_mismatch",
        ),
        (
            lambda p: p["spec"]["containers"][0].__setitem__("image", "old:image"),
            "running_image_unconfirmed",
        ),
    ],
)
def test_ready_pod_must_actually_run_desired_digest(change, reason):
    state = snapshot()
    change(state["pods"]["demo"][0])
    assert verify(state)["reason"] == reason


def test_empty_inventory_and_missing_workload_are_unknown_not_healthy():
    state = snapshot()
    state["applications"] = []
    assert verify(state)["reason"] == "managed_applications_missing"
    state = snapshot()
    state["workloads"] = {}
    assert verify(state)["reason"] == "workload_missing"


def test_upstream_exception_does_not_copy_sensitive_message_into_audit():
    def get(*_):
        raise RuntimeError("credential-bearing upstream response")

    result = verify(get=get)
    assert result == {
        "verified": False,
        "reason": "observation_unavailable",
        "error_type": "RuntimeError",
    }


def test_git_managed_application_uses_observed_single_source_revision():
    state = snapshot()
    app = state["applications"][0]
    source = {
        "repoURL": "https://github.com/owner/repo.git",
        "path": "projects/config",
        "targetRevision": "HEAD",
    }
    app["spec"] = {"source": source}
    app["status"]["sync"] = {
        "status": "Synced",
        "revision": SOURCE,
        "comparedTo": {"source": deepcopy(source)},
    }
    assert verify(state)["verified"] is True
    del app["status"]["sync"]["revision"]
    assert verify(state)["reason"] == "deployed_revision_missing"


def test_short_cache_expires_and_does_not_cache_failed_observation(monkeypatch):
    monkeypatch.setattr(rollout, "_recent", {})
    now = [10.0]
    monkeypatch.setattr(rollout.time, "monotonic", lambda: now[0])
    reads = []

    def read():
        reads.append(now[0])
        return len(reads)

    assert rollout._cached(("ci",), read) == 1
    now[0] += 29
    assert rollout._cached(("ci",), read) == 1
    now[0] += 1
    assert rollout._cached(("ci",), read) == 2

    def failed():
        raise RuntimeError("unavailable")

    with pytest.raises(RuntimeError):
        rollout._cached(("snapshot",), failed)
    assert ("snapshot",) not in rollout._recent


def test_external_tagged_sidecar_does_not_require_our_publication_digest():
    state = snapshot()
    image = "docker.io/thirdparty/helper:1.0"
    workload = next(iter(state["workloads"].values()))
    workload["spec"]["template"]["spec"]["containers"][0]["image"] = image
    state["pods"]["demo"][0]["spec"]["containers"][0]["image"] = image
    assert verify(state)["verified"]
    image = "ghcr.io/jomcgi/homelab/projects/demo/backend:latest"
    workload["spec"]["template"]["spec"]["containers"][0]["image"] = image
    state["pods"]["demo"][0]["spec"]["containers"][0]["image"] = image
    assert verify(state)["reason"] == "published_image_not_pinned"


@pytest.mark.parametrize("kind", ["StatefulSet", "DaemonSet"])
def test_non_deployment_workloads_require_updated_ready_status(kind):
    state = snapshot()
    workload = next(iter(state["workloads"].values()))
    workload["kind"] = kind
    if kind == "StatefulSet":
        workload["status"].update(currentRevision="new", updateRevision="new")
    else:
        workload["status"].update(
            desiredNumberScheduled=1, updatedNumberScheduled=1, numberReady=1
        )
    assert (
        rollout.workload_evidence(workload, state["pods"]["demo"], pinned=True)[
            "replicas"
        ]
        == 1
    )
    if kind == "StatefulSet":
        workload["status"]["currentRevision"] = "old"
    else:
        workload["status"]["updatedNumberScheduled"] = 0
    with pytest.raises(rollout.Pending):
        rollout.workload_evidence(workload, state["pods"]["demo"], pinned=True)


def test_ready_flag_without_running_container_state_is_not_proof():
    state = snapshot()
    state["pods"]["demo"][0]["status"]["containerStatuses"][0]["state"] = {
        "waiting": {"reason": "CrashLoopBackOff"}
    }
    assert verify(state)["reason"] == "running_image_unconfirmed"
