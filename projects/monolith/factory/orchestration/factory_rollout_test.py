"""Publication and live revision evidence, with no network or cluster access."""

import asyncio
import base64
from copy import deepcopy
from types import SimpleNamespace

import httpx
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
        "application": None,
        "resource": None,
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


def test_short_cache_expires_and_shares_failures_without_turning_them_into_success(
    monkeypatch,
):
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

    failures = []

    def failed():
        failures.append(True)
        raise RuntimeError("unavailable")

    for _ in range(20):
        with pytest.raises(RuntimeError, match="unavailable"):
            rollout._cached(("snapshot",), failed)
    assert len(failures) == 1
    now[0] += 30
    assert rollout._cached(("snapshot",), lambda: {"recovered": True}) == {
        "recovered": True
    }


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


def encoded(text):
    return {
        "type": "file",
        "encoding": "base64",
        "content": base64.b64encode(text.encode()).decode(),
    }


def fleet():
    """Two live chart Applications with separate workload and pod namespaces."""
    state = {"applications": [], "workloads": {}, "pods": {}, "unreadable": {}}
    for name in ("monolith", "embervm"):
        item = snapshot()
        app = item["applications"][0]
        app["metadata"]["name"] = name
        for source in (
            app["spec"]["sources"][0],
            app["status"]["sync"]["comparedTo"]["sources"][0],
        ):
            source["chart"] = name
            source["helm"] = {
                "valueFiles": [f"$values/projects/{name}/deploy/values.yaml"]
            }
        resource = app["status"]["resources"][0]
        resource["namespace"] = name
        workload = next(iter(item["workloads"].values()))
        workload["metadata"]["namespace"] = name
        state["applications"].append(app)
        state["workloads"][("Deployment", name, "backend")] = workload
        state["pods"][name] = item["pods"]["demo"]
    return state


def fleet_commit():
    value = commit()
    value["commit"]["message"] = value["commit"]["message"].replace(
        "Chart-Published: projects/demo/chart 1.2.3\n",
        "Chart-Published: projects/monolith/chart 1.2.3\n"
        "Chart-Published: projects/embervm/chart 1.2.3\n",
    )
    return value


def fleet_github(moved=(), documents=None, directories=()):
    documents = documents or {}

    def get(repo, path):
        if path == f"compare/{'f' * 40}...{SOURCE}?per_page=1":
            return {"status": "behind"}
        if not path.startswith("contents/"):
            return github(repo, path)
        location, ref = path.removeprefix("contents/").split("?ref=")
        if location.endswith("/Chart.yaml"):
            name = location.split("/")[1]
            version = "1.2.2" if ref == MERGE and name in moved else "1.2.3"
            return encoded(f"name: {name}\nversion: {version}\n")
        if location in documents:
            return encoded(documents[location])
        if location in directories:
            return []
        raise httpx.HTTPStatusError(
            "not found",
            request=httpx.Request("GET", "https://example.com"),
            response=httpx.Response(404),
        )

    return get


def scoped_verify(
    files, state=None, *, get=None, listing=None, commits=None, pr_number=6660
):
    def list_page(_repo, path):
        if path.startswith("commits?"):
            return [fleet_commit()] if commits is None else commits
        return [{"filename": f} for f in files]

    return rollout.verify(
        "owner/repo",
        MERGE,
        pr_number,
        get=get or fleet_github(),
        listing=listing or list_page,
        snapshot=fleet() if state is None else state,
    )


def test_unrelated_unhealthy_application_does_not_block():
    state = fleet()
    embervm = state["applications"][1]
    embervm["status"]["health"]["status"] = "Progressing"
    state["workloads"][("Deployment", "embervm", "backend")]["status"][
        "readyReplicas"
    ] = 0
    state["pods"]["embervm"][0]["status"]["containerStatuses"][0]["state"] = {
        "waiting": {"reason": "CrashLoopBackOff"}
    }
    state["unreadable"]["embervm"] = "ApiException"
    result = scoped_verify(
        ["projects/monolith/app/main.py"], state, get=fleet_github(moved=["monolith"])
    )
    assert result["verified"] is True
    assert result["scoped_applications"] == ["monolith"]
    assert result["changed_files_count"] == 1


def test_own_application_unhealthy_names_application_and_resource():
    state = fleet()
    embervm = state["applications"][1]
    embervm["status"]["health"]["status"] = "Progressing"
    embervm["status"]["resources"][0]["health"] = {"status": "Progressing"}
    result = scoped_verify(
        ["projects/embervm/bricks.go"], state, get=fleet_github(moved=["embervm"])
    )
    assert result == {
        "verified": False,
        "reason": "application_not_healthy_and_synced",
        "application": "embervm",
        "resource": "Deployment/embervm/backend",
    }


def test_unregistered_manifests_verify_on_merge_with_render_check():
    state = fleet()
    for app in state["applications"]:
        app["status"]["health"]["status"] = "Degraded"
    files = [
        "projects/loom/deploy/values.yaml",
        "projects/gke-apps/loom/application.yaml",
    ]
    state["applications"].append(hub())
    get = hub_github()
    result = scoped_verify(files, state, get=get)
    assert result["verified"] is True
    assert result["applications"] == []
    assert result["scoped_applications"] == []
    assert result["changed_files_count"] == 2
    assert result["scope"] == "no_live_application"
    assert result["render_check"] == {
        "context": "pr-checks",
        "state": "success",
        "commit_sha": "b" * 40,
    }
    assert (
        scoped_verify(files, state, get=get, commits=[])["reason"]
        == "publication_receipt_missing"
    )


def test_listing_failure_is_observation_unavailable():
    def listing(_repo, path):
        if path.startswith("commits?"):
            return [fleet_commit()]
        raise RuntimeError("sensitive upstream response")

    assert scoped_verify([], listing=listing) == {
        "verified": False,
        "reason": "observation_unavailable",
        "error_type": "RuntimeError",
        "application": None,
        "resource": None,
    }


def test_thirty_full_pages_fall_back_to_all_managed_applications():
    calls = []

    def listing(_repo, path):
        if path.startswith("commits?"):
            return [fleet_commit()]
        calls.append(path)
        return [{"filename": f"docs/file-{len(calls)}-{i}.md"} for i in range(100)]

    state = fleet()
    state["applications"][1]["status"]["health"]["status"] = "Progressing"
    result = scoped_verify([], state, listing=listing)
    assert result["reason"] == "application_not_healthy_and_synced"
    assert result["application"] == "embervm"
    assert len(calls) == 30
    assert calls[-1] == "pulls/6660/files?per_page=100&page=30"
    state["applications"][1]["status"]["health"]["status"] = "Healthy"
    result = scoped_verify([], state, listing=listing)
    assert result["scoped_applications"] == ["embervm", "monolith"]
    assert result["changed_files_count"] is None


def test_missing_pr_number_falls_back_to_all_managed_applications():
    state = fleet()
    state["applications"][1]["status"]["health"]["status"] = "Progressing"
    assert scoped_verify([], state, pr_number=None)["application"] == "embervm"
    result = scoped_verify([], pr_number=None)
    assert result["scoped_applications"] == ["embervm", "monolith"]
    assert result["changed_files_count"] is None


def test_changed_files_pages_and_previous_filename_are_in_scope():
    calls = []

    def listing(_repo, path):
        if path.startswith("commits?"):
            return [fleet_commit()]
        calls.append(path)
        if len(calls) == 1:
            return [{"filename": f"docs/{i}.md"} for i in range(100)]
        return [
            {
                "filename": "docs/moved.yaml",
                "previous_filename": "projects/monolith/chart/old.yaml",
            }
        ]

    result = scoped_verify([], listing=listing)
    assert result["scoped_applications"] == ["monolith"]
    assert result["changed_files_count"] == 102
    assert calls == [
        "pulls/6660/files?per_page=100&page=1",
        "pulls/6660/files?per_page=100&page=2",
    ]


def test_chart_missing_from_receipt_is_in_scope_and_not_deployed():
    value = fleet_commit()
    value["commit"]["message"] = value["commit"]["message"].replace(
        "Chart-Published: projects/embervm/chart 1.2.3\n", ""
    )
    result = scoped_verify(["docs/readme.md"], commits=[value])
    assert result["reason"] == "published_chart_not_deployed"
    assert result["application"] == "embervm"


def test_unreadable_chart_at_merge_is_in_scope():
    fallback = fleet_github()

    def get(repo, path):
        if path == f"contents/projects/embervm/chart/Chart.yaml?ref={MERGE}":
            raise RuntimeError("unreadable")
        return fallback(repo, path)

    assert scoped_verify(["docs/readme.md"], get=get)["scoped_applications"] == [
        "embervm"
    ]


def test_owned_ref_values_file_scopes_only_its_application():
    result = scoped_verify(["projects/embervm/deploy/values.yaml"])
    assert result["scoped_applications"] == ["embervm"]
    assert (
        scoped_verify(["projects/embervm/deploy/values.yaml.bak"])[
            "scoped_applications"
        ]
        == []
    )


def hub(path="projects/gke-cluster"):
    app = snapshot()["applications"][0]
    source = {
        "repoURL": "https://github.com/owner/repo.git",
        "path": path,
        "targetRevision": "HEAD",
    }
    app["metadata"]["name"] = "hub"
    app["spec"] = {"source": source}
    app["status"]["sync"] = {
        "status": "Synced",
        "revision": SOURCE,
        "comparedTo": {"source": deepcopy(source)},
    }
    app["status"]["resources"] = []
    return app


def hub_github():
    return fleet_github(
        documents={
            "projects/gke-cluster/kustomization.yaml": "resources:\n- ../../projects/platform-gke\n- ../../projects/gke-apps\n",
            "projects/platform-gke/kustomization.yaml": "resources: []\n",
            "projects/gke-apps/kustomization.yaml": "resources:\n- ./monolith\n- ./embervm\n",
            "projects/gke-apps/monolith/kustomization.yaml": "resources: [application.yaml]\n",
            "projects/gke-apps/monolith/application.yaml": "kind: Application\n",
            "projects/gke-apps/embervm/kustomization.yaml": "resources: [application.yaml]\n",
            "projects/gke-apps/embervm/application.yaml": "kind: Application\n",
        },
        directories=[
            "projects/platform-gke",
            "projects/gke-apps",
            "projects/gke-apps/monolith",
            "projects/gke-apps/embervm",
        ],
    )


def test_kustomize_recursion_maps_registered_application_to_hub():
    state = fleet()
    state["applications"].append(hub())
    result = scoped_verify(
        ["projects/gke-apps/monolith/application.yaml"], state, get=hub_github()
    )
    assert result["scoped_applications"] == ["hub"]


@pytest.mark.parametrize(
    "filename", ["kustomization.yaml", "kustomization.yml", "Kustomization"]
)
def test_kustomize_file_resource_and_config_itself_are_exact_surfaces(filename):
    state = fleet()
    state["applications"].append(hub("projects/config"))
    get = fleet_github(
        documents={
            f"projects/config/{filename}": "resources: [./sub/../manifest.yaml, https://example.com/remote.yaml]\n",
            "projects/config/manifest.yaml": "kind: ConfigMap\n",
        }
    )
    for path in (f"projects/config/{filename}", "projects/config/manifest.yaml"):
        assert scoped_verify([path], state, get=get)["scoped_applications"] == ["hub"]
    assert (
        scoped_verify(["projects/config/manifest.yaml.bak"], state, get=get)[
            "scoped_applications"
        ]
        == []
    )


def test_kustomization_with_patches_uses_whole_prefix():
    state = fleet()
    state["applications"].append(hub("projects/config"))
    get = fleet_github(
        documents={"projects/config/kustomization.yaml": "resources: []\npatches: []\n"}
    )
    assert scoped_verify(["projects/config/patch.yaml"], state, get=get)[
        "scoped_applications"
    ] == ["hub"]


def test_no_kustomization_uses_prefix_but_non_404_is_fail_closed():
    state = fleet()
    state["applications"].append(hub("projects/config"))
    assert scoped_verify(["projects/config/chart.yaml"], state)[
        "scoped_applications"
    ] == ["hub"]
    assert (
        scoped_verify(["projects/config-other/chart.yaml"], state)[
            "scoped_applications"
        ]
        == []
    )
    fallback = fleet_github()

    def get(repo, path):
        if "kustomization" in path:
            raise RuntimeError("not a 404")
        return fallback(repo, path)

    assert scoped_verify(["docs/readme.md"], state, get=get)["scoped_applications"] == [
        "hub"
    ]


@pytest.mark.parametrize("resource", ["../../../outside", "/outside"])
def test_resource_outside_repo_is_fail_closed(resource):
    state = fleet()
    state["applications"].append(hub("projects/config"))
    get = fleet_github(
        documents={"projects/config/kustomization.yaml": f"resources: [{resource}]\n"}
    )
    assert scoped_verify(["docs/readme.md"], state, get=get)["scoped_applications"] == [
        "hub"
    ]


def test_kustomization_recursion_bound_is_six():
    for depth, names in ((6, []), (7, ["hub"])):
        state = fleet()
        state["applications"].append(hub("projects/n0"))
        documents = {
            f"projects/n{i}/kustomization.yaml": f"resources: [../n{i + 1}]\n"
            for i in range(depth)
        }
        documents[f"projects/n{depth}/kustomization.yaml"] = "resources: []\n"
        get = fleet_github(
            documents=documents,
            directories=[f"projects/n{i}" for i in range(1, depth + 1)],
        )
        assert (
            scoped_verify(["docs/readme.md"], state, get=get)["scoped_applications"]
            == names
        )


def test_kustomization_read_bound_is_sixty_four_shared_per_verify():
    for count, names in ((64, []), (65, ["hub"])):
        state = fleet()
        state["applications"].append(hub("projects/root"))
        documents = {
            "projects/root/kustomization.yaml": "resources: ["
            + ",".join(f"../n{i}" for i in range(count - 1))
            + "]\n"
        }
        documents.update(
            {
                f"projects/n{i}/kustomization.yaml": "resources: []\n"
                for i in range(count - 1)
            }
        )
        base = fleet_github(
            documents=documents,
            directories=[f"projects/n{i}" for i in range(count - 1)],
        )
        reads = []

        def get(repo, path, reads=reads, base=base):
            if "/kustomization.yaml?" in path:
                reads.append(path)
            return base(repo, path)

        assert (
            scoped_verify(["docs/readme.md"], state, get=get)["scoped_applications"]
            == names
        )
        assert len(reads) == 64


def test_unknown_owned_source_or_value_ref_is_fail_closed():
    state = fleet()
    app = state["applications"][0]
    app["spec"]["sources"][0]["helm"]["valueFiles"] = [
        "$missing/projects/monolith/deploy/values.yaml"
    ]
    app["status"]["sync"]["comparedTo"]["sources"] = deepcopy(app["spec"]["sources"])
    assert scoped_verify(["docs/readme.md"], state)["scoped_applications"] == [
        "monolith"
    ]
    app = hub()
    del app["spec"]["source"]["path"]
    app["status"]["sync"]["comparedTo"]["source"] = deepcopy(app["spec"]["source"])
    state["applications"] = [app]
    assert scoped_verify(["docs/readme.md"], state)["scoped_applications"] == ["hub"]


@pytest.mark.parametrize(
    "change", ["stale", "missing_workload", "unreadable", "OutOfSync"]
)
def test_all_checks_skip_out_of_scope_applications(change):
    state = fleet()
    if change == "stale":
        state["applications"][1]["spec"]["sources"][0]["targetRevision"] = "9.9.9"
    elif change == "missing_workload":
        state["workloads"].pop(("Deployment", "embervm", "backend"))
    elif change == "unreadable":
        state["unreadable"]["embervm"] = "ApiException"
    else:
        state["applications"][1]["status"]["sync"]["status"] = change
    assert (
        scoped_verify(["projects/monolith/chart/templates/deploy.yaml"], state)[
            "verified"
        ]
        is True
    )


def test_in_scope_unreadable_workload_is_pending_with_application():
    state = fleet()
    state["unreadable"]["embervm"] = "ApiException"
    assert scoped_verify(["projects/embervm/deploy/values.yaml"], state) == {
        "verified": False,
        "reason": "workload_unreadable",
        "application": "embervm",
        "resource": None,
    }


def test_workload_and_pod_blockers_name_workload():
    state = fleet()
    state["pods"]["monolith"][0]["status"]["phase"] = "Pending"
    result = scoped_verify(["projects/monolith/deploy/values.yaml"], state)
    assert result["reason"] == "pod_not_running"
    assert result["application"] == "monolith"
    assert result["resource"] == "Deployment/monolith/backend"


def test_later_publication_does_not_pull_an_unrelated_chart_into_scope():
    later = fleet_commit()
    later["sha"] = "e" * 40
    later["commit"]["message"] = (
        later["commit"]["message"]
        .replace(SOURCE, "f" * 40)
        .replace("projects/embervm/chart 1.2.3", "projects/embervm/chart 1.2.4")
    )
    state = fleet()
    state["applications"][1]["status"]["health"]["status"] = "Progressing"
    result = scoped_verify(
        ["projects/monolith/app/main.py"],
        state,
        get=fleet_github(moved=("monolith",)),
        commits=[later, fleet_commit()],
    )
    assert result["verified"] is True
    assert result["scoped_applications"] == ["monolith"]
    assert result["publication_commit_sha"] == "e" * 40
    assert result["scope_publication_commit_sha"] == "c" * 40


def test_healthy_newer_publication_can_verify_the_original_merge():
    later = fleet_commit()
    later["sha"] = "e" * 40
    later["commit"]["message"] = (
        later["commit"]["message"]
        .replace(SOURCE, "f" * 40)
        .replace("projects/monolith/chart 1.2.3", "projects/monolith/chart 1.2.4")
    )
    state = fleet()
    app = state["applications"][0]
    app["spec"]["sources"][0]["targetRevision"] = "1.2.4"
    app["status"]["sync"]["comparedTo"]["sources"][0]["targetRevision"] = "1.2.4"
    app["status"]["sync"]["revisions"] = ["1.2.4", "f" * 40]
    result = scoped_verify(
        ["projects/monolith/app/main.py"],
        state,
        get=fleet_github(moved=("monolith",)),
        commits=[later, fleet_commit()],
    )
    assert result["verified"] is True
    assert result["scoped_applications"] == ["monolith"]
    assert result["publication_commit_sha"] == "e" * 40


def test_delayed_pre_merge_source_receipt_does_not_end_scope_search():
    later = fleet_commit()
    later["sha"] = "e" * 40
    later["commit"]["message"] = (
        later["commit"]["message"]
        .replace(SOURCE, "f" * 40)
        .replace("projects/embervm/chart 1.2.3", "projects/embervm/chart 1.2.4")
    )
    delayed = fleet_commit()
    delayed["sha"] = "1" * 40
    delayed["commit"]["message"] = delayed["commit"]["message"].replace(
        SOURCE, "0" * 40
    )
    fallback = fleet_github(moved=("monolith",))

    def get(repo, path):
        if path == f"compare/{MERGE}...{'0' * 40}?per_page=1":
            return {"status": "behind"}
        return fallback(repo, path)

    state = fleet()
    state["applications"][1]["status"]["health"]["status"] = "Progressing"
    result = scoped_verify(
        ["projects/monolith/app/main.py"],
        state,
        get=get,
        commits=[later, delayed, fleet_commit()],
    )
    assert result["verified"] is True
    assert result["scoped_applications"] == ["monolith"]


def test_receipts_are_ordered_by_source_ancestry_not_publisher_finish_time():
    later = fleet_commit()
    later["sha"] = "e" * 40
    later["commit"]["message"] = (
        later["commit"]["message"]
        .replace(SOURCE, "f" * 40)
        .replace("projects/monolith/chart 1.2.3", "projects/monolith/chart 1.2.4")
        .replace("projects/embervm/chart 1.2.3", "projects/embervm/chart 1.2.4")
    )
    delayed = fleet_commit()
    delayed["sha"] = "1" * 40
    fallback = fleet_github(moved=("monolith",))

    def get(repo, path):
        if path == f"compare/{'f' * 40}...{SOURCE}?per_page=1":
            return {"status": "behind"}
        return fallback(repo, path)

    state = fleet()
    app = state["applications"][0]
    app["spec"]["sources"][0]["targetRevision"] = "1.2.4"
    app["status"]["sync"]["comparedTo"]["sources"][0]["targetRevision"] = "1.2.4"
    app["status"]["sync"]["revisions"] = ["1.2.4", "f" * 40]
    state["applications"][1]["status"]["health"]["status"] = "Progressing"
    result = scoped_verify(
        ["projects/monolith/app/main.py"], state, get=get, commits=[delayed, later]
    )
    assert result["verified"] is True
    assert result["scoped_applications"] == ["monolith"]
    assert result["scope_publication_commit_sha"] == "1" * 40
    assert result["publication_commit_sha"] == "e" * 40


def test_unordered_publication_sources_fail_closed():
    later = fleet_commit()
    later["sha"] = "e" * 40
    later["commit"]["message"] = later["commit"]["message"].replace(SOURCE, "f" * 40)
    fallback = fleet_github()

    def get(repo, path):
        if path in (
            f"compare/{'f' * 40}...{SOURCE}?per_page=1",
            f"compare/{SOURCE}...{'f' * 40}?per_page=1",
        ):
            return {"status": "diverged"}
        return fallback(repo, path)

    result = scoped_verify(
        ["projects/monolith/deploy/values.yaml"],
        get=get,
        commits=[later, fleet_commit()],
    )
    assert result["verified"] is False
    assert result["reason"] == "publication_source_not_ordered"


def test_unknown_kustomize_dependency_key_cannot_exclude_affected_application():
    state = fleet()
    app = hub("projects/config")
    app["status"]["health"]["status"] = "Progressing"
    state["applications"].append(app)
    get = fleet_github(
        documents={
            "projects/config/kustomization.yaml": "resources: []\ncomponents: [../shared]\n",
        }
    )
    result = scoped_verify(["projects/shared/manifest.yaml"], state, get=get)
    assert result["verified"] is False
    assert result["application"] == "hub"
    assert result["reason"] == "application_not_healthy_and_synced"


def snapshot_client(monkeypatch, *, failure=None, incomplete_apps=False):
    from kubernetes_asyncio import client, config

    state = fleet()
    reads = []

    class Api:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def sanitize_for_serialization(self, value):
            return value

    class Custom:
        async def list_namespaced_custom_object(self, *_args, **kwargs):
            assert kwargs == {"limit": 200}
            return {
                "items": state["applications"],
                "metadata": {"continue": "next" if incomplete_apps else ""},
            }

    class Apps:
        async def read_namespaced_deployment(self, name, namespace):
            reads.append((name, namespace))
            if failure == "workload" and namespace == "embervm":
                raise client.ApiException(status=404, reason="sensitive response")
            if failure == "stall" and namespace == "embervm":
                await asyncio.Event().wait()
            return state["workloads"][("Deployment", namespace, name)]

        read_namespaced_stateful_set = read_namespaced_deployment
        read_namespaced_daemon_set = read_namespaced_deployment

    class Core:
        async def list_namespaced_pod(self, namespace, **kwargs):
            assert kwargs == {"limit": 500}
            if failure == "pod_error" and namespace == "embervm":
                raise RuntimeError("sensitive response")
            return SimpleNamespace(
                metadata=SimpleNamespace(
                    _continue="next"
                    if failure == "pods" and namespace == "embervm"
                    else ""
                ),
                items=state["pods"][namespace],
            )

    monkeypatch.setattr(config, "load_incluster_config", lambda: None)
    monkeypatch.setattr(client, "ApiClient", Api)
    monkeypatch.setattr(client, "CustomObjectsApi", lambda _api: Custom())
    monkeypatch.setattr(client, "AppsV1Api", lambda _api: Apps())
    monkeypatch.setattr(client, "CoreV1Api", lambda _api: Core())
    return state, reads


def test_out_of_scope_stalled_workload_is_never_read(monkeypatch):
    _, reads = snapshot_client(monkeypatch, failure="stall")
    monkeypatch.setattr(rollout, "_recent", {})
    original_wait = asyncio.wait_for
    deadlines = []

    async def bounded(coro, *, timeout):
        deadlines.append(timeout)
        return await original_wait(coro, timeout=0.1)

    def listing(_repo, path):
        if path.startswith("commits?"):
            return [fleet_commit()]
        return [{"filename": "projects/monolith/deploy/values.yaml"}]

    monkeypatch.setattr(rollout.asyncio, "wait_for", bounded)
    result = rollout.verify(
        "owner/repo", MERGE, 6660, get=fleet_github(), listing=listing
    )
    assert result["verified"] is True
    assert result["scoped_applications"] == ["monolith"]
    assert reads == [("backend", "monolith")]
    assert deadlines and all(timeout == 30 for timeout in deadlines)


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("workload", "ApiException"),
        ("pods", "pod_inventory_incomplete"),
        ("pod_error", "RuntimeError"),
    ],
)
def test_snapshot_isolates_unreadable_application(monkeypatch, failure, reason):
    _, reads = snapshot_client(monkeypatch, failure=failure)
    observed = asyncio.run(rollout.cluster_snapshot("owner/repo"))
    assert reads == [("backend", "monolith"), ("backend", "embervm")]
    assert observed["unreadable"] == {"embervm": reason}
    assert (
        scoped_verify(["projects/monolith/deploy/values.yaml"], observed)["verified"]
        is True
    )
    assert (
        scoped_verify(["projects/embervm/deploy/values.yaml"], observed)["reason"]
        == "workload_unreadable"
    )


def test_snapshot_inventory_failure_is_not_isolated(monkeypatch):
    snapshot_client(monkeypatch, incomplete_apps=True)
    with pytest.raises(rollout.Pending, match="application_inventory_incomplete"):
        asyncio.run(rollout.cluster_snapshot("owner/repo"))


def test_incomplete_namespace_marks_every_using_application(monkeypatch):
    state, _ = snapshot_client(monkeypatch, failure="pods")
    other = deepcopy(state["applications"][1])
    other["metadata"]["name"] = "embervm-dev"
    state["applications"].append(other)
    observed = asyncio.run(rollout.cluster_snapshot("owner/repo"))
    assert observed["unreadable"] == {
        "embervm": "pod_inventory_incomplete",
        "embervm-dev": "pod_inventory_incomplete",
    }


def test_kustomization_read_budget_is_shared_between_applications():
    state = fleet()
    documents, directories = {}, []
    for name in ("first", "second"):
        app = hub(f"projects/{name}")
        app["metadata"]["name"] = name
        state["applications"].append(app)
        documents[f"projects/{name}/kustomization.yaml"] = (
            "resources: [" + ",".join(f"./n{i}" for i in range(32)) + "]\n"
        )
        for i in range(32):
            path = f"projects/{name}/n{i}"
            directories.append(path)
            documents[path + "/kustomization.yaml"] = "resources: []\n"
    get = fleet_github(documents=documents, directories=directories)
    # Each app needs 33 reads, but together they exceed the literal 64-read budget.
    assert scoped_verify(["docs/readme.md"], state, get=get)["scoped_applications"] == [
        "second"
    ]


def test_cached_surface_cannot_bypass_depth_bound_for_another_application():
    state = fleet()
    app = hub("projects/n6")
    app["metadata"]["name"] = "shallow"
    state["applications"].extend([app, hub("projects/n0")])
    documents = {
        f"projects/n{i}/kustomization.yaml": f"resources: [../n{i + 1}]\n"
        for i in range(7)
    }
    documents["projects/n7/kustomization.yaml"] = "resources: []\n"
    get = fleet_github(
        documents=documents, directories=[f"projects/n{i}" for i in range(1, 8)]
    )
    assert scoped_verify(["docs/readme.md"], state, get=get)["scoped_applications"] == [
        "hub"
    ]


def test_resource_blocker_prioritizes_health_then_sync_and_cluster_scope():
    state = fleet()
    app = state["applications"][1]
    app["status"]["health"]["status"] = "Progressing"
    app["status"]["resources"] = [
        {
            "kind": "ConfigMap",
            "namespace": "embervm",
            "name": "config",
            "status": "OutOfSync",
        },
        {"kind": "Node", "name": "worker", "health": {"status": "Degraded"}},
    ]
    result = scoped_verify(["projects/embervm/deploy/values.yaml"], state)
    assert result["resource"] == "Node/worker"
    app["status"]["resources"][1]["health"]["status"] = "Healthy"
    assert (
        scoped_verify(["projects/embervm/deploy/values.yaml"], state)["resource"]
        == "ConfigMap/embervm/config"
    )


def test_immutable_mapper_reads_use_merge_ref(monkeypatch):
    calls = []
    fallback = hub_github()

    def get(repo, path):
        calls.append(path)
        return fallback(repo, path)

    monkeypatch.setattr(rollout, "_immutable_get", get)
    surface = rollout.DeploySurface("owner/repo", MERGE, rollout._get)
    assert (
        "projects/gke-apps/monolith/application.yaml"
        in surface.directory("projects/gke-cluster")[0]
    )
    assert calls
    assert all(path.endswith("?ref=" + "a" * 40) for path in calls)


def test_deliveries_share_inventory_and_only_same_scope_snapshots(monkeypatch):
    monkeypatch.setattr(rollout, "_recent", {})
    reads = []
    inventories = []

    async def inventory(repo):
        inventories.append(repo)
        return fleet()["applications"]

    async def observe(repo, applications, scoped):
        reads.append((repo, sorted(scoped)))
        assert len(applications) == 2
        return fleet()

    def listing(_repo, path):
        if path.startswith("commits?"):
            return [fleet_commit()]
        name = "monolith" if path.startswith("pulls/1/") else "embervm"
        return [{"filename": f"projects/{name}/deploy/values.yaml"}]

    monkeypatch.setattr(rollout, "cluster_snapshot", observe)
    monkeypatch.setattr(rollout, "application_inventory", inventory)
    for number, name in ((1, "monolith"), (2, "embervm"), (1, "monolith")):
        result = rollout.verify(
            "owner/repo", MERGE, number, get=fleet_github(), listing=listing
        )
        assert result["scoped_applications"] == [name]
    assert inventories == ["owner/repo"]
    assert reads == [("owner/repo", ["monolith"]), ("owner/repo", ["embervm"])]


def test_first_covering_publication_must_be_provable_within_two_pages():
    calls = []

    def listing(_repo, path):
        calls.append(path)
        return [fleet_commit()] * 100

    result = scoped_verify(["projects/monolith/deploy/values.yaml"], listing=listing)
    assert result["verified"] is False
    assert result["reason"] == "publication_scope_incomplete"
    assert calls == [
        "commits?sha=main&per_page=100&page=1",
        "commits?sha=main&per_page=100&page=2",
    ]
