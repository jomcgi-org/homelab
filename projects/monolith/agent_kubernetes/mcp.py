"""MCP tools for bounded Kubernetes observations by the broker principal."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent_kubernetes.client import (
    LIST_LIMIT_DEFAULT,
    LOG_BYTES_MAX,
    LOG_SINCE_DEFAULT_SECONDS,
    LOG_TAIL_DEFAULT,
    InvalidObservationRequest,
    ObservationFailure,
    RestrictedKubernetesClient,
    validate_log_request,
    validate_read_request,
)
from auth.api import Authority, PrincipalKind, current_principal


AUTHORIZED_SUBJECT = "kg-agent-sa"
AUTHORIZED_GROUP = "kg-agents"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _authorization_error() -> dict[str, Any] | None:
    principal = current_principal()
    if (
        principal.authority is Authority.STANDING
        and principal.kind is PrincipalKind.WORKLOAD
        and principal.subject == AUTHORIZED_SUBJECT
        and not principal.actor
        and principal.has_group(AUTHORIZED_GROUP)
    ):
        return None
    return _error(
        "unauthorized",
        "standing kg-agent-sa workload authorization is required",
    )


def _error(
    code: str, message: str, coverage: dict[str, Any] | None = None
) -> dict[str, Any]:
    result = {
        "ok": False,
        "error": {"code": code, "message": message},
        "freshness": {
            "observed_at": _now(),
            "source": "kubernetes_api",
            "successful": False,
        },
    }
    if coverage is not None:
        result["coverage"] = coverage
    return result


def _read_coverage(request, next_continue_token: str | None) -> dict[str, Any]:
    return {
        "verb": request.verb,
        "api_group": request.rule.api_group,
        "api_version": request.rule.version,
        "resource": request.rule.resource,
        "namespace": request.namespace,
        "cluster_scoped": not request.rule.namespaced,
        "page_limit": request.limit if request.verb == "list" else None,
        "complete": next_continue_token is None,
        "next_continue_token": next_continue_token,
    }


async def kubernetes_read(
    verb: str,
    api_group: str,
    resource: str,
    namespace: str | None = None,
    name: str | None = None,
    subresource: str | None = None,
    limit: int = LIST_LIMIT_DEFAULT,
    continue_token: str | None = None,
) -> dict[str, Any]:
    """Get or list one explicitly allowlisted Kubernetes resource.

    The API group is ``core`` for Kubernetes' empty core group. Namespaced
    resources require one of the recorded observation namespaces. Cluster
    scope exists only for nodes, namespaces, and node metrics. Lists return a
    single server-bounded page and a continuation token when another page is
    available. Subresources, wildcard values, and every verb except get/list
    are rejected. Use ``kubernetes_pod_logs`` for the sole allowed subresource.
    """

    if denial := _authorization_error():
        return denial
    try:
        request = validate_read_request(
            verb=verb,
            api_group=api_group,
            resource=resource,
            namespace=namespace,
            name=name,
            subresource=subresource,
            limit=limit,
            continue_token=continue_token,
        )
    except InvalidObservationRequest as exc:
        return _error("invalid_request", str(exc))

    observer = RestrictedKubernetesClient()
    try:
        result = await observer.read(request)
    except ObservationFailure as exc:
        coverage = _read_coverage(request, None)
        coverage["complete"] = False
        return _error(exc.code, exc.message, coverage)
    finally:
        await observer.close()

    observed_at = _now()
    return {
        "ok": True,
        "items": result.items,
        "freshness": {
            "observed_at": observed_at,
            "source": "kubernetes_api",
            "successful": True,
        },
        "coverage": _read_coverage(request, result.next_continue_token),
    }


async def kubernetes_pod_logs(
    namespace: str,
    pod: str,
    container: str | None = None,
    tail_lines: int = LOG_TAIL_DEFAULT,
    since_seconds: int = LOG_SINCE_DEFAULT_SECONDS,
    previous: bool = False,
) -> dict[str, Any]:
    """Read a time-, line-, and byte-bounded pod log from an allowed namespace."""

    if denial := _authorization_error():
        return denial
    try:
        (
            namespace,
            pod,
            container,
            tail_lines,
            since_seconds,
            previous,
        ) = validate_log_request(
            namespace=namespace,
            pod=pod,
            container=container,
            tail_lines=tail_lines,
            since_seconds=since_seconds,
            previous=previous,
        )
    except InvalidObservationRequest as exc:
        return _error("invalid_request", str(exc))

    observer = RestrictedKubernetesClient()
    try:
        result = await observer.pod_logs(
            namespace=namespace,
            pod=pod,
            container=container,
            tail_lines=tail_lines,
            since_seconds=since_seconds,
            previous=previous,
        )
    except ObservationFailure as exc:
        return _error(
            exc.code,
            exc.message,
            {
                "api_group": "core",
                "api_version": "v1",
                "resource": "pods/log",
                "verb": "get",
                "namespace": namespace,
                "pod": pod,
                "container": container,
                "tail_lines": tail_lines,
                "since_seconds": since_seconds,
                "previous": previous,
                "max_bytes": LOG_BYTES_MAX,
                "complete": False,
            },
        )
    finally:
        await observer.close()

    return {
        "ok": True,
        **result,
        "freshness": {
            "observed_at": _now(),
            "source": "kubernetes_api",
            "successful": True,
        },
        "coverage": {
            "api_group": "core",
            "api_version": "v1",
            "resource": "pods/log",
            "verb": "get",
            "namespace": namespace,
            "pod": pod,
            "container": container,
            "tail_lines": tail_lines,
            "since_seconds": since_seconds,
            "previous": previous,
            "max_bytes": LOG_BYTES_MAX,
        },
    }
