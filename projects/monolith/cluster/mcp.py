"""MCP tools for curated Kubernetes debugging — the ``k8s-*`` surface.

Six tools, all read-only except ``k8s-sync-argocd-app``: cluster health
rollups, generic resource list/get over a curated kind allowlist, filtered pod
logs, deduped events, and ArgoCD sync. Output is shaped by ``cluster.summarize``
for token efficiency — never a raw manifest dump unless explicitly requested.

Tool names follow the codebase convention: the FastMCP name is the function
name (``k8s_*``). The gateway converts underscores to dashes and (today) adds
the ``monolith-`` federation prefix, so these surface as ``k8s-*`` once that
prefix is dropped.
"""

from __future__ import annotations

import logging

from core.mcp_app import mcp
from cluster import summarize
from cluster.kubernetes import RESOURCE_KINDS, KubernetesClient, UnknownKindError

logger = logging.getLogger(__name__)

# Workload kinds scanned by the health rollup (replicasets excluded as noise).
_HEALTH_KINDS = ("deployments", "statefulsets", "daemonsets", "pods", "applications")


@mcp.tool
async def k8s_health_summary() -> dict:
    """Cluster health rollup: only the unhealthy workloads, pods and ArgoCD apps.

    Scans deployments, statefulsets, daemonsets, pods and ArgoCD applications
    across all namespaces and returns just what is not-ready / CrashLooping /
    OutOfSync / Degraded, grouped by kind. ``healthy=true`` when nothing is
    wrong. The fastest way to answer "what's broken right now?".
    """
    k8s = KubernetesClient()
    try:
        resources: dict[str, list[dict]] = {}
        for kind in _HEALTH_KINDS:
            try:
                resources[kind] = await k8s.list_resources(kind)
            except Exception:
                logger.exception("k8s-health-summary: listing %s failed", kind)
                resources[kind] = []
        return summarize.build_health(resources)
    finally:
        await k8s.close()


@mcp.tool
async def k8s_list_resources(
    kind: str,
    namespace: str | None = None,
    label_selector: str | None = None,
    limit: int = 100,
) -> dict:
    """List resources of a curated ``kind`` as lean one-line rows.

    Args:
        kind: One of the allowed kinds (see error message for the full set,
            e.g. pods, deployments, services, events, applications).
        namespace: Restrict to a namespace, or omit for all namespaces
            (ignored for cluster-scoped kinds like nodes/namespaces).
        label_selector: Standard label selector, e.g. "app=foo".
        limit: Max rows returned (default 100).
    """
    k8s = KubernetesClient()
    try:
        objs = await k8s.list_resources(kind, namespace, label_selector)
    except UnknownKindError:
        return {"error": f"unknown kind {kind!r}; allowed: {RESOURCE_KINDS}"}
    finally:
        await k8s.close()
    rows = [summarize.resource_row(kind, o) for o in objs]
    return {"kind": kind, "count": len(rows), "items": rows[:limit]}


@mcp.tool
async def k8s_get_resource(
    kind: str,
    name: str,
    namespace: str | None = None,
    full: bool = False,
) -> dict:
    """Get one resource, trimmed to key status/conditions by default.

    Args:
        kind: One of the allowed kinds.
        name: Resource name.
        namespace: Namespace (defaults to "default" for namespaced kinds,
            "argocd" for applications).
        full: Return the entire manifest instead of the trimmed view.
    """
    k8s = KubernetesClient()
    try:
        obj = await k8s.get_resource(kind, name, namespace)
    except UnknownKindError:
        return {"error": f"unknown kind {kind!r}; allowed: {RESOURCE_KINDS}"}
    finally:
        await k8s.close()
    if obj is None:
        return {"error": f"{kind}/{name} not found"}
    return summarize.resource_detail(kind, obj, full=full)


@mcp.tool
async def k8s_get_pod_logs(
    namespace: str,
    pod: str,
    container: str | None = None,
    tail_lines: int = 200,
    grep: str | None = None,
    previous: bool = False,
) -> dict:
    """Read a pod's logs, optionally regex-filtered, tailed and byte-capped.

    Args:
        namespace: Pod namespace.
        pod: Pod name.
        container: Container name (required only for multi-container pods).
        tail_lines: Lines to fetch from the end (default 200).
        grep: Optional regex, only matching lines are returned.
        previous: Read the previous (crashed) container instance instead.
    """
    k8s = KubernetesClient()
    try:
        text = await k8s.get_pod_logs(
            namespace,
            pod,
            container=container,
            tail_lines=tail_lines,
            previous=previous,
        )
    except Exception as exc:
        return {"error": f"log fetch failed: {exc}"}
    finally:
        await k8s.close()
    return summarize.filter_logs(text, grep=grep, max_lines=tail_lines)


@mcp.tool
async def k8s_get_events(
    namespace: str | None = None,
    involved_object: str | None = None,
) -> dict:
    """List cluster events, deduplicated by (object, type, reason, message).

    Args:
        namespace: Restrict to a namespace, or omit for all namespaces.
        involved_object: Restrict to events about a specific object name.
    """
    k8s = KubernetesClient()
    try:
        events = await k8s.list_events(namespace, involved_object)
    finally:
        await k8s.close()
    deduped = summarize.dedupe_events(events)
    return {"count": len(deduped), "events": deduped}


@mcp.tool
async def k8s_sync_argocd_app(
    name: str,
    prune: bool = False,
    dry_run: bool = False,
) -> dict:
    """Trigger an ArgoCD sync for an Application by patching its ``.operation``.

    Args:
        name: ArgoCD Application name (in the argocd namespace).
        prune: Delete resources no longer tracked by Git.
        dry_run: Server-side dry run only (no changes applied).
    """
    k8s = KubernetesClient()
    try:
        return await k8s.sync_argocd_app(name, prune=prune, dry_run=dry_run)
    except Exception as exc:
        return {"error": f"sync failed: {exc}"}
    finally:
        await k8s.close()
