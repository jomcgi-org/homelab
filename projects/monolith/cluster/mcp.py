"""MCP tools for curated Kubernetes debugging — the ``k8s-*`` surface.

Eight tools, all read-only except ``k8s-sync-argocd-app`` and
``kargo-promote``: cluster health rollups, generic resource list/get over a
curated kind allowlist, filtered pod logs, deduped events, ArgoCD sync,
``verify-deployment``, the rollout verdict shared with the agents tier
(``shared.rollout``), and ``kargo-promote``, which re-runs a Kargo Promotion
(``cluster.kargo``). Output is shaped by ``cluster.summarize``
for token efficiency — never a raw manifest dump unless explicitly requested.

Tool names follow the codebase convention: the FastMCP name is the function
name (``k8s_*``, and ``verify_deployment``, which keeps its name so it matches
the agents tier). The gateway converts underscores to dashes and (today) adds
the ``monolith-`` federation prefix, so these surface as ``k8s-*`` once that
prefix is dropped.
"""

from __future__ import annotations

import json
import logging

from core.mcp_app import mcp
from cluster import kargo as kargo_plan
from cluster import summarize
from cluster.kubernetes import RESOURCE_KINDS, KubernetesClient, UnknownKindError
from kubernetes_asyncio.client.exceptions import ApiException

from shared import rollout

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


@mcp.tool
async def verify_deployment(app: str, expected_revision: str | None = None) -> dict:
    """Say whether an ArgoCD Application has finished rolling out.

    Reads the Application and returns a verdict of verified, in_progress or
    failed, with the checks behind it: sync, health, the last operation,
    ArgoCD error conditions, the live revision, and up to ten unhealthy
    resources. Poll while in_progress.

    For a chart app pass expected_revision, the chart version the write-back
    produced: it passes once that version or a later one is live. For a
    git-tracked app omit it: verified with reconciled_at at least five minutes
    after your merge (past ArgoCD's cache of HEAD) means your merge is live (a commit sha also works, but
    matches only while it is still the head). A version for a git app, or a
    sha for a chart app, is rejected as an error rather than left pending.
    For an app Kargo promotes, a ``kargo`` block and check say why a revision
    is not live yet: a Promotion running or failed (Kargo never retries a
    failed one), Freight waiting on upstream verification, or drift where
    something reverted a promotion. The expected Freight's ``approved_for``
    shows a manual approval.
    The agents tier serves the same tool with the same verdict rules.

    Args:
        app: ArgoCD Application name in the argocd namespace.
        expected_revision: Optional chart version or commit sha that must be live.
    """
    k8s = KubernetesClient()
    kargo = None
    try:
        obj = await k8s.get_argocd_application(app)
        ref = rollout.kargo_stage_ref(obj) if obj is not None else None
        if ref is not None:
            try:
                kargo = await k8s.get_kargo_context(*ref)
            except ApiException as exc:
                kargo = {
                    "error": f"reading Kargo stage {ref[1]!r} in {ref[0]} failed: HTTP {exc.status}"
                }
            except Exception as exc:
                # Kargo context only explains the verdict; never let it fail one.
                kargo = {
                    "error": f"reading Kargo stage {ref[1]!r} in {ref[0]} failed: {exc}"
                }
    except ApiException as exc:
        return {"error": f"reading application {app!r} failed: HTTP {exc.status}"}
    finally:
        await k8s.close()
    if obj is None:
        return {"error": f"application {app!r} not found in argocd"}
    try:
        return rollout.verdict(obj, expected_revision=expected_revision, kargo=kargo)
    except rollout.RevisionMismatch as exc:
        return {"error": str(exc)}


def _api_message(exc: ApiException) -> str:
    try:
        message = json.loads(exc.body or "").get("message")
    except (TypeError, ValueError, AttributeError):
        message = None
    return str(message or exc.reason)[:500]


@mcp.tool
async def kargo_promote(
    app: str, chart_version: str, dry_run: bool = False, rollback: bool = False
) -> dict:
    """Promote a chart version to a Kargo-owned app's Stage again.

    The lever for a Promotion that failed or errored: Kargo never retries one,
    so the Freight waits until something promotes it again. Run
    verify_deployment first: its kargo block says whether a failed Promotion
    is what is holding the rollout.

    Refuses rather than overriding a gate: the Freight must already be
    available to the Stage (verified and soaked upstream, approved for the
    Stage, or direct from the Warehouse), and no Promotion for the Stage may
    be running or queued. A version older than the Stage runs or last
    promoted is refused unless rollback is true. It never approves Freight. The Promotion runs the Stage's own steps, so
    it is exactly the Promotion auto-promotion would have created. Poll
    verify_deployment with expected_revision afterwards.

    Args:
        app: ArgoCD Application name in the argocd namespace.
        chart_version: The exact chart version to promote, such as 0.547.0.
        dry_run: Plan and submit through admission (Kargo's webhook
            included) without creating anything.
        rollback: Allow a version older than the Stage runs or last promoted.
    """
    k8s = KubernetesClient()
    try:
        obj = await k8s.get_argocd_application(app)
        if obj is None:
            return {"error": f"application {app!r} not found in argocd"}
        ref = rollout.kargo_stage_ref(obj)
        if ref is None:
            return {"error": f"application {app!r} is not promoted by Kargo"}
        namespace, stage = ref
        context = await k8s.get_kargo_context(namespace, stage)
        promotions = await k8s.list_kargo_promotions(namespace)
        body = kargo_plan.plan_promotion(
            context.get("stage"),
            context.get("freights") or [],
            namespace=namespace,
            stage_name=stage,
            chart=rollout.app_chart(obj),
            version=chart_version,
            promotions=promotions,
            rollback=rollback,
        )
        created = await k8s.create_kargo_promotion(namespace, body, dry_run=dry_run)
    except kargo_plan.PromotionRefused as exc:
        return {"error": f"not promoting: {exc}"}
    except ApiException as exc:
        # Kargo's webhook explains a denial in the body, not the reason.
        return {"error": f"promotion failed: HTTP {exc.status}: {_api_message(exc)}"}
    except Exception as exc:
        return {"error": f"promotion failed: {exc}"}
    finally:
        await k8s.close()
    logger.info(
        "kargo_promote: %s %s to %s/%s (dry_run=%s, rollback=%s)",
        app,
        chart_version,
        namespace,
        stage,
        dry_run,
        rollback,
    )
    return {
        "app": app,
        "namespace": namespace,
        "stage": stage,
        "freight": body["spec"]["freight"],
        "chart_version": chart_version,
        "promotion": (created.get("metadata") or {}).get("name"),
        "dry_run": dry_run,
    }
