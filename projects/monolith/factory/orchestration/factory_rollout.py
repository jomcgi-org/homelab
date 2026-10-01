"""Read-only, fail-closed evidence for the repository's managed live fleet.

A healthy Application pinned to an old chart is not a deployed delivery. Bind
the merge to a main publication receipt, then compare actual Argo revisions and
running workloads. This is infrastructure evidence, not functional acceptance.
"""

from __future__ import annotations

import asyncio
import base64
import posixpath
import re
import time
from datetime import datetime, timezone
from functools import lru_cache

import yaml

REGISTRY = "ghcr.io/jomcgi/homelab/charts"
SHA = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
WORKLOADS = {"Deployment", "StatefulSet", "DaemonSet"}
_recent: dict[tuple, tuple[float, object]] = {}


def _cached(key: tuple, read):
    """Share bounded observations across pending deliveries, for at most 30s."""
    now = time.monotonic()
    prior = _recent.get(key)
    if prior is not None and now - prior[0] < 30:
        value = prior[1]
    else:
        try:
            value = read()
        except Exception as exc:  # noqa: BLE001 - cache failures, never an empty inventory
            value = exc.with_traceback(None)
        if len(_recent) >= 64:
            _recent.pop(next(iter(_recent)))
        _recent[key] = (time.monotonic(), value)
    if isinstance(value, Exception):
        # A timed-out cluster must not cost another 30s for every pending task.
        # Drop old traceback frames when sharing the same bounded failure.
        raise value.with_traceback(None)
    return value


@lru_cache(maxsize=256)
def _immutable_get(repo: str, path: str):
    from factory.orchestration.factory_landing import github_get

    return github_get(repo, path)


def _get(repo: str, path: str):
    from factory.orchestration.factory_landing import github_get

    if path.startswith(("compare/", "contents/")):
        return _immutable_get(repo, path)
    return _cached(("get", repo, path), lambda: github_get(repo, path))


def _listing(repo: str, path: str):
    from factory.orchestration.factory_landing import github_list

    return _cached(("list", repo, path), lambda: github_list(repo, path))


class Pending(ValueError):
    """An observation cannot yet prove rollout, never a success by omission."""

    def __init__(self, reason: str, *, application=None, resource=None):
        super().__init__(reason)
        self.application = application
        self.resource = resource


def publication(commit: dict) -> dict | None:
    body = commit.get("commit") or {}
    message = body.get("message", "")
    if "Chart-Publication-Complete: true" not in message.splitlines():
        return None
    author = body.get("author") or {}
    if author.get("email") != "chart-version-bot@users.noreply.github.com":
        return None
    if not message.startswith("chore(charts): publish "):
        return None
    sources, charts = [], {}
    for line in message.splitlines():
        if line.startswith("Chart-Source-Commit: "):
            sources.append(line.removeprefix("Chart-Source-Commit: "))
        if line.startswith("Chart-Published: "):
            parts = line.removeprefix("Chart-Published: ").split()
            if len(parts) != 2:
                raise Pending("invalid_publication_record")
            path, version = parts
            if (
                not re.fullmatch(r"projects/[A-Za-z0-9_./-]+", path)
                or any(part in ("", ".", "..") for part in path.split("/"))
                or not VERSION.fullmatch(version)
                or path in charts
            ):
                raise Pending("invalid_publication_record")
            charts[path] = version
    sha = commit.get("sha", "")
    if len(sources) != 1 or not SHA.fullmatch(sources[0]) or not SHA.fullmatch(sha):
        raise Pending("invalid_publication_identity")
    return {"commit_sha": sha, "source_sha": sources[0], "charts": charts}


def ancestor(repo: str, older: str, newer: str, get) -> bool:
    if not SHA.fullmatch(older) or not SHA.fullmatch(newer):
        raise Pending("missing_revision_identity")
    if older == newer:
        return True
    comparison = get(repo, f"compare/{older}...{newer}?per_page=1")
    return comparison.get("status") in ("ahead", "identical")


def receipt_for(repo: str, merge_sha: str, get, listing) -> dict:
    # A bounded search may defer, but may never turn an absent receipt into a
    # no-op publication. Lists come from main, not a PR or merge-queue ref.
    candidate = None
    latest = None
    complete = False
    for page in (1, 2):
        commits = listing(repo, f"commits?sha=main&per_page=100&page={page}")
        for commit in commits:
            receipt = publication(commit)
            if receipt is None:
                continue
            # Publisher source commits can finish out of order. Only the
            # actual write-back commit can prove we passed the merge in main.
            comparison = get(
                repo, f"compare/{merge_sha}...{receipt['commit_sha']}?per_page=1"
            )
            if comparison.get("status") == "behind":
                complete = True
                break
            if not ancestor(repo, merge_sha, receipt["source_sha"], get):
                continue
            if not ancestor(repo, receipt["source_sha"], receipt["commit_sha"], get):
                raise Pending("publication_source_not_on_main")
            # Publishers may finish out of source order. Source ancestry pins
            # the first covering scope and the newest proven deployment.
            if latest is None:
                latest = receipt
                candidate = receipt
            elif receipt["source_sha"] != latest["source_sha"]:
                if ancestor(repo, latest["source_sha"], receipt["source_sha"], get):
                    latest = receipt
                elif not ancestor(
                    repo, receipt["source_sha"], latest["source_sha"], get
                ):
                    raise Pending("publication_source_not_ordered")
            if receipt["source_sha"] != candidate["source_sha"] and ancestor(
                repo, receipt["source_sha"], candidate["source_sha"], get
            ):
                candidate = receipt
        if complete:
            break
        if len(commits) < 100:
            complete = True
            break
    if candidate is None:
        raise Pending("publication_receipt_missing")
    if not complete:
        raise Pending("publication_scope_incomplete")
    for source_sha in dict.fromkeys((candidate["source_sha"], latest["source_sha"])):
        ci = get(repo, f"commits/{source_sha}/status")
        required = [
            s for s in ci.get("statuses", []) if s.get("context") == "pr-checks"
        ]
        if len(required) != 1 or required[0].get("state") != "success":
            raise Pending("publication_ci_not_successful")
    return {**latest, "scope_receipt": candidate}


def sources(app: dict) -> list[dict]:
    spec = app.get("spec") or {}
    return spec.get("sources") or ([spec["source"]] if spec.get("source") else [])


def normalized(value):
    """Argo omits default-false Helm flags from comparedTo, unlike spec."""
    omitted_false = {
        "ignoreMissingValueFiles",
        "passCredentials",
        "skipCrds",
        "skipTests",
        "skipSchemaValidation",
        "recurse",
    }
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            item = normalized(item)
            if item not in (None, "", [], {}) and not (
                key in omitted_false and item is False
            ):
                result[key] = item
        return result
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


def owned_git(repo: str, source: dict) -> bool:
    return (
        source.get("repoURL", "").removesuffix(".git") == f"https://github.com/{repo}"
    )


def managed(repo: str, app: dict) -> bool:
    return any(owned_git(repo, s) or s.get("repoURL") == REGISTRY for s in sources(app))


def content_yaml(content):
    if content.get("encoding") != "base64" or content.get("type") != "file":
        raise Pending("chart_identity_unreadable")
    return yaml.safe_load(base64.b64decode(content["content"]))


def chart_catalog(repo: str, receipt: dict, get) -> dict[str, tuple[str, str]]:
    """Read each publication chart identity once, retaining its deploy directory."""
    result = {}
    for path, version in receipt["charts"].items():
        content = get(repo, f"contents/{path}/Chart.yaml?ref={receipt['source_sha']}")
        chart = content_yaml(content)
        name = chart.get("name") if isinstance(chart, dict) else None
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name)
            or name in result
        ):
            raise Pending("chart_identity_ambiguous")
        result[name] = (path, version)
    return result


def chart_versions(repo: str, receipt: dict, get) -> dict[str, str]:
    return {
        name: version
        for name, (_, version) in chart_catalog(repo, receipt, get).items()
    }


def changed_files(repo: str, pr_number: int | None, listing) -> set[str] | None:
    """None means unbounded, including GitHub's possibly truncated 3000 files."""
    if pr_number is None:
        return None
    paths = set()
    for page in range(1, 31):
        files = listing(repo, f"pulls/{pr_number}/files?per_page=100&page={page}")
        for file in files:
            paths.add(file["filename"])
            if file.get("previous_filename"):
                paths.add(file["previous_filename"])
        if len(files) < 100:
            return paths
    return None


def repo_path(path: str) -> str:
    """Normalize local paths without allowing resources outside the repository."""
    if not isinstance(path, str) or not path or path.startswith("/"):
        raise Pending("deploy_surface_unreadable")
    path = posixpath.normpath(path)
    if path == ".." or path.startswith("../"):
        raise Pending("deploy_surface_unreadable")
    return path


class DeploySurface:
    """Bounded, immutable kustomize resource traversal shared by one verification."""

    def __init__(self, repo, merge_sha, get):
        self.repo, self.merge_sha, self.get = repo, merge_sha, get
        self.reads = 0
        self.cache = {}

    def read(self, path):
        return self.get(self.repo, f"contents/{path}?ref={self.merge_sha}")

    def directory(self, path, depth=0):
        path = repo_path(path)
        if depth > 6:
            raise Pending("deploy_surface_unreadable")
        cache_key = (path, depth)
        if cache_key in self.cache:
            return self.cache[cache_key]
        prefix = "" if path == "." else path + "/"
        exact, prefixes = set(), set()
        for filename in ("kustomization.yaml", "kustomization.yml", "Kustomization"):
            self.reads += 1
            if self.reads > 64:
                raise Pending("deploy_surface_unreadable")
            location = prefix + filename
            try:
                content = self.read(location)
            except Exception as exc:
                # Only an actual HTTP 404 means this spelling does not exist.
                code = getattr(exc, "status", None) or getattr(
                    getattr(exc, "response", None), "status_code", None
                )
                if code == 404:
                    continue
                raise
            config = content_yaml(content)
            if not isinstance(config, dict):
                raise Pending("deploy_surface_unreadable")
            exact.add(location)
            known = {
                "apiVersion",
                "kind",
                "resources",
                "namespace",
                "commonLabels",
                "labels",
                "commonAnnotations",
                "namePrefix",
                "nameSuffix",
            }
            if config.keys() - known:
                # Unknown keys may reference dependencies outside this directory.
                # Include the Application conservatively via the caller's fence.
                raise Pending("deploy_surface_unreadable")
            resources = config.get("resources", [])
            if not isinstance(resources, list):
                raise Pending("deploy_surface_unreadable")
            for resource in resources:
                if not isinstance(resource, str):
                    raise Pending("deploy_surface_unreadable")
                if "://" in resource or resource.startswith("git::"):
                    continue
                if resource.startswith("/"):
                    raise Pending("deploy_surface_unreadable")
                target = repo_path(posixpath.join(path, resource))
                entry = self.read(target)
                if isinstance(entry, list) or entry.get("type") == "dir":
                    child_exact, child_prefixes = self.directory(target, depth + 1)
                    exact.update(child_exact)
                    prefixes.update(child_prefixes)
                elif entry.get("type") == "file":
                    exact.add(target)
                else:
                    raise Pending("deploy_surface_unreadable")
            break
        else:
            prefixes.add(prefix)
        self.cache[cache_key] = exact, prefixes
        return exact, prefixes


def application_in_scope(repo, app, files, catalog, surface) -> bool:
    if files is None:
        return True
    try:
        app_sources = sources(app)
        refs = {s["ref"] for s in app_sources if owned_git(repo, s) and s.get("ref")}
        for source in app_sources:
            registry = source.get("repoURL") == REGISTRY
            owned = owned_git(repo, source)
            if registry:
                chart = source.get("chart")
                if chart not in catalog:
                    return True
                path, version = catalog[chart]
                merged = content_yaml(surface.read(path + "/Chart.yaml"))
                if merged["version"] != version or any(
                    f.startswith(path + "/") for f in files
                ):
                    return True
            elif owned:
                if source.get("chart"):
                    return True
                if source.get("path"):
                    exact, prefixes = surface.directory(source["path"])
                    if files & exact or any(
                        f.startswith(p) for f in files for p in prefixes
                    ):
                        return True
                elif not source.get("ref"):
                    return True
            for value in source.get("helm", {}).get("valueFiles", []):
                if not isinstance(value, str):
                    return True
                if value.startswith("$"):
                    ref, sep, path = value[1:].partition("/")
                    if ref not in refs or not sep:
                        return True
                    if repo_path(path) in files:
                        return True
                elif registry or owned:
                    # Relative value files are not a recognized owned-ref shape.
                    return True
        return False
    except Exception:  # noqa: BLE001 - unknown mapping includes the Application
        return True


def selected(labels: dict, selector: dict) -> bool:
    if not selector or any(
        labels.get(k) != v for k, v in selector.get("matchLabels", {}).items()
    ):
        return False
    for expression in selector.get("matchExpressions", []):
        key, op, values = (
            expression["key"],
            expression["operator"],
            expression.get("values", []),
        )
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and labels.get(key) in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
        if op not in {"In", "NotIn", "Exists", "DoesNotExist"}:
            raise Pending("unsupported_pod_selector")
    return True


def workload_evidence(workload: dict, pods: list[dict], *, pinned: bool) -> dict:
    meta, spec, status = (
        workload["metadata"],
        workload["spec"],
        workload.get("status", {}),
    )
    if status.get("observedGeneration", -1) < meta["generation"]:
        raise Pending("workload_generation_unobserved")
    kind = workload["kind"]
    if kind == "DaemonSet":
        desired = status.get("desiredNumberScheduled")
        counts = (status.get("updatedNumberScheduled", 0), status.get("numberReady", 0))
        if status.get("numberMisscheduled", 0):
            raise Pending("workload_not_ready")
    else:
        desired = spec.get("replicas", 1)
        counts = (
            status.get("replicas", 0),
            status.get("updatedReplicas", 0),
            status.get("readyReplicas", 0),
        )
    if type(desired) is not int or desired < 0 or any(n != desired for n in counts):
        raise Pending("workload_not_ready")
    if (
        kind == "StatefulSet"
        and desired
        and (
            not status.get("currentRevision")
            or status.get("currentRevision") != status.get("updateRevision")
        )
    ):
        raise Pending("statefulset_revision_mismatch")
    expected = {c["name"]: c["image"] for c in spec["template"]["spec"]["containers"]}
    matching = [
        p
        for p in pods
        if selected(p["metadata"].get("labels", {}), spec.get("selector", {}))
    ]
    if len(matching) != desired:
        raise Pending("pod_count_mismatch")
    images = set()
    for pod in matching:
        if (
            pod["metadata"].get("deletionTimestamp")
            or pod.get("status", {}).get("phase") != "Running"
        ):
            raise Pending("pod_not_running")
        conditions = pod.get("status", {}).get("conditions", [])
        if not any(
            c.get("type") == "Ready" and c.get("status") == "True" for c in conditions
        ):
            raise Pending("pod_not_ready")
        containers = {c["name"]: c["image"] for c in pod["spec"]["containers"]}
        running = {
            c["name"]: c for c in pod.get("status", {}).get("containerStatuses", [])
        }
        for name, image in expected.items():
            live = running.get(name, {})
            image_id = live.get("imageID", "")
            if (
                containers.get(name) != image
                or not live.get("ready")
                or not image_id
                or "running" not in live.get("state", {})
            ):
                raise Pending("running_image_unconfirmed")
            digest = re.search(r"@sha256:([0-9a-f]{64})$", image)
            if (
                pinned
                and image.startswith("ghcr.io/jomcgi/homelab/projects/")
                and digest is None
            ):
                raise Pending("published_image_not_pinned")
            if digest and not image_id.endswith("sha256:" + digest[1]):
                raise Pending("running_digest_mismatch")
            images.add(image_id)
    return {
        "kind": kind,
        "name": meta["name"],
        "namespace": meta["namespace"],
        "generation": meta["generation"],
        "replicas": desired,
        "image_ids": sorted(images),
    }


def evaluate(
    repo: str,
    receipt: dict,
    snapshot: dict,
    versions: dict,
    get,
    scoped_applications: set[str] | None = None,
) -> list[dict]:
    applications = [app for app in snapshot["applications"] if managed(repo, app)]
    if not applications:
        raise Pending("managed_applications_missing")
    evidence = []
    for app in applications:
        name = app["metadata"]["name"]
        if scoped_applications is not None and name not in scoped_applications:
            continue
        try:
            evidence.append(
                application_evidence(repo, receipt, snapshot, versions, get, app)
            )
        except Pending as exc:
            exc.application = name
            raise
    return evidence


def resource_name(resource: dict) -> str:
    return "/".join(
        str(resource[k]) for k in ("kind", "namespace", "name") if resource.get(k)
    )


def application_evidence(repo, receipt, snapshot, versions, get, app):
    name, status = app["metadata"]["name"], app.get("status", {})
    sync = status.get("sync", {})
    if (
        status.get("health", {}).get("status") != "Healthy"
        or sync.get("status") != "Synced"
    ):
        resources = status.get("resources", [])
        unhealthy = next(
            (
                r
                for r in resources
                if r.get("health", {}).get("status")
                and r["health"]["status"] != "Healthy"
            ),
            None,
        )
        unhealthy = unhealthy or next(
            (r for r in resources if r.get("status") == "OutOfSync"), None
        )
        raise Pending(
            "application_not_healthy_and_synced",
            resource=resource_name(unhealthy) if unhealthy else None,
        )
    if name in snapshot.get("unreadable", {}):
        raise Pending("workload_unreadable")
    app_sources = sources(app)
    compared = sync.get("comparedTo", {})
    observed_sources = compared.get("sources") or (
        [compared["source"]] if compared.get("source") else []
    )
    if normalized(observed_sources) != normalized(app_sources):
        raise Pending("application_comparison_stale")
    revisions = sync.get("revisions") or (
        [sync["revision"]] if sync.get("revision") else []
    )
    if len(revisions) != len(app_sources):
        raise Pending("deployed_revision_missing")
    pinned = False
    for source, revision in zip(app_sources, revisions, strict=True):
        if source.get("repoURL") == REGISTRY:
            pinned = True
            if (
                source.get("chart") not in versions
                or versions[source["chart"]] != revision
            ):
                raise Pending("published_chart_not_deployed")
        elif owned_git(repo, source):
            # Unchanged git values need only contain this delivery's source,
            # even if a newer publication proves the current chart version.
            floor = receipt.get("scope_receipt", receipt)["source_sha"]
            if not ancestor(repo, floor, revision, get):
                raise Pending("git_revision_not_deployed")
    if "resources" not in status:
        raise Pending("application_resources_missing")
    workloads = []
    for resource in status["resources"]:
        if resource.get("kind") not in WORKLOADS:
            continue
        key = (resource["kind"], resource["namespace"], resource["name"])
        try:
            workload = snapshot["workloads"].get(key)
            if workload is None:
                raise Pending("workload_missing")
            workloads.append(
                workload_evidence(workload, snapshot["pods"][key[1]], pinned=pinned)
            )
        except Pending as exc:
            exc.resource = resource_name(resource)
            raise
    return {
        "application": name,
        "uid": app["metadata"]["uid"],
        "revisions": revisions,
        "workloads": workloads,
    }


async def application_inventory(repo: str) -> list[dict]:
    from kubernetes_asyncio import client, config

    config.load_incluster_config()
    async with client.ApiClient() as api:
        found = await client.CustomObjectsApi(api).list_namespaced_custom_object(
            "argoproj.io", "v1alpha1", "argocd", "applications", limit=200
        )
        if found.get("metadata", {}).get("continue"):
            raise Pending("application_inventory_incomplete")
        return found["items"]


async def cluster_snapshot(
    repo: str, applications=None, scoped_applications=None
) -> dict:
    from kubernetes_asyncio import client, config

    if applications is None:
        applications = await application_inventory(repo)
    config.load_incluster_config()
    async with client.ApiClient() as api:
        apps, core = client.AppsV1Api(api), client.CoreV1Api(api)
        result = {
            "applications": applications,
            "workloads": {},
            "pods": {},
            "unreadable": {},
        }
        pod_failures = {}
        for app in applications:
            if not managed(repo, app):
                continue
            if (
                scoped_applications is not None
                and app["metadata"]["name"] not in scoped_applications
            ):
                continue
            for resource in app.get("status", {}).get("resources", []):
                kind = resource.get("kind")
                if kind not in WORKLOADS:
                    continue
                ns, name = resource["namespace"], resource["name"]
                reader = {
                    "Deployment": apps.read_namespaced_deployment,
                    "StatefulSet": apps.read_namespaced_stateful_set,
                    "DaemonSet": apps.read_namespaced_daemon_set,
                }[kind]
                try:
                    result["workloads"][(kind, ns, name)] = (
                        api.sanitize_for_serialization(await reader(name, ns))
                    )
                    if ns in pod_failures:
                        raise Pending(pod_failures[ns])
                    if ns not in result["pods"]:
                        try:
                            pods = await core.list_namespaced_pod(ns, limit=500)
                            if pods.metadata._continue:
                                raise Pending("pod_inventory_incomplete")
                            result["pods"][ns] = [
                                api.sanitize_for_serialization(p) for p in pods.items
                            ]
                        except Exception as exc:
                            pod_failures[ns] = (
                                str(exc)
                                if isinstance(exc, Pending)
                                else type(exc).__name__
                            )
                            raise
                except Exception as exc:  # noqa: BLE001 - isolate per-Application reads
                    result["unreadable"][app["metadata"]["name"]] = (
                        str(exc) if isinstance(exc, Pending) else type(exc).__name__
                    )
        result["observed_at"] = datetime.now(timezone.utc).isoformat()
        return result


def verify(
    repo: str, merge_sha: str, pr_number=None, *, get=None, listing=None, snapshot=None
) -> dict:
    get, listing = get or _get, listing or _listing
    try:
        if not isinstance(merge_sha, str) or not SHA.fullmatch(merge_sha):
            raise Pending("merge_identity_missing")
        receipt = receipt_for(repo, merge_sha, get, listing)
        catalog = chart_catalog(repo, receipt, get)
        scope_receipt = receipt["scope_receipt"]
        scope_catalog = (
            chart_catalog(repo, scope_receipt, get)
            if scope_receipt["commit_sha"] != receipt["commit_sha"]
            else catalog
        )
        versions = {name: version for name, (_, version) in catalog.items()}
        files = changed_files(repo, pr_number, listing)
        surface = DeploySurface(repo, merge_sha, get)
        if snapshot is None:

            async def inventory():
                return await asyncio.wait_for(application_inventory(repo), timeout=30)

            applications = _cached(
                ("applications", repo), lambda: asyncio.run(inventory())
            )
        else:
            applications = snapshot["applications"]
        scoped = {
            app["metadata"]["name"]
            for app in applications
            if managed(repo, app)
            and application_in_scope(repo, app, files, scope_catalog, surface)
        }
        if snapshot is None:

            async def observe():
                return await asyncio.wait_for(
                    cluster_snapshot(repo, applications, scoped), timeout=30
                )

            snapshot = _cached(
                ("snapshot", repo, tuple(sorted(scoped))),
                lambda: asyncio.run(observe()),
            )
        apps = evaluate(repo, receipt, snapshot, versions, get, scoped)
        result = {
            "verified": True,
            "merge_commit_sha": merge_sha,
            "publication_commit_sha": receipt["commit_sha"],
            "scope_publication_commit_sha": scope_receipt["commit_sha"],
            "source_commit_sha": receipt["source_sha"],
            "observed_at": snapshot.get("observed_at")
            or datetime.now(timezone.utc).isoformat(),
            "applications": apps,
            "scoped_applications": sorted(scoped),
            "changed_files_count": len(files) if files is not None else None,
        }
        if not scoped:
            result.update(
                scope="no_live_application",
                render_check={
                    "context": "pr-checks",
                    "state": "success",
                    "commit_sha": receipt["source_sha"],
                },
            )
        return result
    except Pending as exc:
        return {
            "verified": False,
            "reason": str(exc),
            "application": exc.application,
            "resource": exc.resource,
        }
    except Exception as exc:  # noqa: BLE001 - failed observation is never proof
        return {
            "verified": False,
            "reason": "observation_unavailable",
            "error_type": type(exc).__name__,
            "application": None,
            "resource": None,
        }
