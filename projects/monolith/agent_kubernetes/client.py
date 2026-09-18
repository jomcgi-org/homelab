"""Strict, read-only Kubernetes client for the guest-facing agents tier.

This module deliberately does not reuse ``cluster.kubernetes``. The private
monolith client includes cluster-wide reads and mutation helpers that must not
enter the pruned agents image. Every path here is constructed from a closed
resource table, and every request is a GET made through a generated client
method.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiClient


OBSERVATION_NAMESPACES = frozenset(
    {
        "argocd",
        "authentik",
        "embervm",
        "inference",
        "kargo",
        "kargo-embervm",
        "kargo-monolith",
        "mcp",
        "monolith",
        "monolith-agents",
        "otel-collector",
    }
)
ARGOCD_NAMESPACE = "argocd"
KARGO_NAMESPACES = frozenset({"kargo-embervm", "kargo-monolith"})

LIST_LIMIT_DEFAULT = 50
LIST_LIMIT_MAX = 100
LOG_TAIL_DEFAULT = 200
LOG_TAIL_MAX = 500
LOG_SINCE_DEFAULT_SECONDS = 3600
LOG_SINCE_MAX_SECONDS = 86400
LOG_BYTES_MAX = 32_000
CONTINUE_TOKEN_MAX = 1024
REQUEST_TIMEOUT_SECONDS = 10.0
OPERATION_TIMEOUT_SECONDS = 12.0

_DNS_SUBDOMAIN = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?$")
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_CONTINUE_TOKEN = re.compile(r"^[A-Za-z0-9_+./=-]+$")


class InvalidObservationRequest(ValueError):
    """Raised before I/O when a request is outside the explicit contract."""


class ObservationFailure(RuntimeError):
    """Safe, classified failure returned to an MCP caller."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ResourceRule:
    api_group: str
    version: str
    resource: str
    singular: str
    api: Literal["core", "apps", "custom"]
    namespaced: bool
    namespaces: frozenset[str] | None


_GENERAL_RULES = (
    ResourceRule("core", "v1", "pods", "pod", "core", True, OBSERVATION_NAMESPACES),
    ResourceRule(
        "core", "v1", "services", "service", "core", True, OBSERVATION_NAMESPACES
    ),
    ResourceRule(
        "core",
        "v1",
        "configmaps",
        "config_map",
        "core",
        True,
        OBSERVATION_NAMESPACES,
    ),
    ResourceRule("core", "v1", "events", "event", "core", True, OBSERVATION_NAMESPACES),
    ResourceRule(
        "apps",
        "v1",
        "deployments",
        "deployment",
        "apps",
        True,
        OBSERVATION_NAMESPACES,
    ),
    ResourceRule(
        "apps",
        "v1",
        "statefulsets",
        "stateful_set",
        "apps",
        True,
        OBSERVATION_NAMESPACES,
    ),
    ResourceRule(
        "apps",
        "v1",
        "daemonsets",
        "daemon_set",
        "apps",
        True,
        OBSERVATION_NAMESPACES,
    ),
    ResourceRule(
        "apps",
        "v1",
        "replicasets",
        "replica_set",
        "apps",
        True,
        OBSERVATION_NAMESPACES,
    ),
    ResourceRule(
        "metrics.k8s.io",
        "v1beta1",
        "pods",
        "pod",
        "custom",
        True,
        OBSERVATION_NAMESPACES,
    ),
)
_SPECIAL_RULES = (
    ResourceRule("core", "v1", "nodes", "node", "core", False, None),
    ResourceRule("core", "v1", "namespaces", "namespace", "core", False, None),
    ResourceRule(
        "metrics.k8s.io",
        "v1beta1",
        "nodes",
        "node",
        "custom",
        False,
        None,
    ),
    ResourceRule(
        "argoproj.io",
        "v1alpha1",
        "applications",
        "application",
        "custom",
        True,
        frozenset({ARGOCD_NAMESPACE}),
    ),
    ResourceRule(
        "kargo.akuity.io",
        "v1alpha1",
        "freights",
        "freight",
        "custom",
        True,
        KARGO_NAMESPACES,
    ),
)
RESOURCE_RULES = {
    (rule.api_group, rule.resource): rule for rule in (*_GENERAL_RULES, *_SPECIAL_RULES)
}
ALLOWED_RESOURCES = tuple(
    f"{group}/{resource}" for group, resource in sorted(RESOURCE_RULES)
)


@dataclass(frozen=True, slots=True)
class ReadRequest:
    verb: Literal["get", "list"]
    rule: ResourceRule
    namespace: str | None
    name: str | None
    limit: int
    continue_token: str | None


@dataclass(frozen=True, slots=True)
class ReadResult:
    items: list[dict[str, Any]]
    next_continue_token: str | None


def _valid_name(value: object, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise InvalidObservationRequest(f"{field} must be a Kubernetes DNS name")
    if pattern is _DNS_SUBDOMAIN and any(
        not _DNS_LABEL.fullmatch(label) for label in value.split(".")
    ):
        raise InvalidObservationRequest(f"{field} must be a Kubernetes DNS name")
    return value


def validate_read_request(
    *,
    verb: object,
    api_group: object,
    resource: object,
    namespace: object = None,
    name: object = None,
    subresource: object = None,
    limit: object = LIST_LIMIT_DEFAULT,
    continue_token: object = None,
) -> ReadRequest:
    """Return a normalized request only when every field is in the allowlist."""

    if verb not in ("get", "list"):
        raise InvalidObservationRequest("verb must be get or list")
    if not isinstance(api_group, str) or not isinstance(resource, str):
        raise InvalidObservationRequest("api_group and resource must be strings")
    if "*" in api_group or "*" in resource:
        raise InvalidObservationRequest("wildcards are not allowed")
    if subresource is not None:
        raise InvalidObservationRequest(
            "subresources are not available through kubernetes_read"
        )
    rule = RESOURCE_RULES.get((api_group, resource))
    if rule is None:
        raise InvalidObservationRequest(
            f"resource is not allowed; allowed: {', '.join(ALLOWED_RESOURCES)}"
        )

    normalized_namespace: str | None
    if rule.namespaced:
        normalized_namespace = _valid_name(namespace, "namespace", _DNS_LABEL)
        if normalized_namespace not in (rule.namespaces or frozenset()):
            raise InvalidObservationRequest(
                "namespace is not allowed for this resource"
            )
    else:
        if namespace is not None:
            raise InvalidObservationRequest(
                "namespace must be omitted for a cluster-scoped resource"
            )
        normalized_namespace = None

    normalized_name: str | None = None
    if verb == "get":
        normalized_name = _valid_name(name, "name", _DNS_SUBDOMAIN)
        if continue_token is not None:
            raise InvalidObservationRequest("continue_token is only valid for list")
    elif name is not None:
        raise InvalidObservationRequest("name must be omitted for list")

    if isinstance(limit, bool) or not isinstance(limit, int):
        raise InvalidObservationRequest("limit must be an integer")
    if limit < 1 or limit > LIST_LIMIT_MAX:
        raise InvalidObservationRequest(f"limit must be between 1 and {LIST_LIMIT_MAX}")

    normalized_continue: str | None = None
    if continue_token is not None:
        if (
            not isinstance(continue_token, str)
            or len(continue_token) > CONTINUE_TOKEN_MAX
            or not _CONTINUE_TOKEN.fullmatch(continue_token)
        ):
            raise InvalidObservationRequest("continue_token is malformed")
        normalized_continue = continue_token

    return ReadRequest(
        verb=verb,
        rule=rule,
        namespace=normalized_namespace,
        name=normalized_name,
        limit=limit,
        continue_token=normalized_continue,
    )


def validate_log_request(
    *,
    namespace: object,
    pod: object,
    container: object,
    tail_lines: object,
    since_seconds: object,
    previous: object,
) -> tuple[str, str, str | None, int, int, bool]:
    normalized_namespace = _valid_name(namespace, "namespace", _DNS_LABEL)
    if normalized_namespace not in OBSERVATION_NAMESPACES:
        raise InvalidObservationRequest("namespace is not allowed for pod logs")
    normalized_pod = _valid_name(pod, "pod", _DNS_SUBDOMAIN)
    normalized_container = None
    if container is not None:
        normalized_container = _valid_name(container, "container", _DNS_LABEL)
    if isinstance(tail_lines, bool) or not isinstance(tail_lines, int):
        raise InvalidObservationRequest("tail_lines must be an integer")
    if tail_lines < 1 or tail_lines > LOG_TAIL_MAX:
        raise InvalidObservationRequest(
            f"tail_lines must be between 1 and {LOG_TAIL_MAX}"
        )
    if isinstance(since_seconds, bool) or not isinstance(since_seconds, int):
        raise InvalidObservationRequest("since_seconds must be an integer")
    if since_seconds < 1 or since_seconds > LOG_SINCE_MAX_SECONDS:
        raise InvalidObservationRequest(
            f"since_seconds must be between 1 and {LOG_SINCE_MAX_SECONDS}"
        )
    if not isinstance(previous, bool):
        raise InvalidObservationRequest("previous must be a boolean")
    return (
        normalized_namespace,
        normalized_pod,
        normalized_container,
        tail_lines,
        since_seconds,
        previous,
    )


def _classify_api_error(exc: Exception) -> ObservationFailure:
    status = getattr(exc, "status", None)
    if status == 403:
        return ObservationFailure("forbidden", "Kubernetes denied the observation")
    if status == 404:
        return ObservationFailure("not_found", "Kubernetes resource was not found")
    if status in (408, 429, 504):
        return ObservationFailure("timeout", "Kubernetes observation timed out")
    return ObservationFailure("unavailable", "Kubernetes observation failed")


def _metadata(obj: dict[str, Any]) -> dict[str, Any]:
    metadata = obj.get("metadata") or {}
    projected = {
        key: metadata[key]
        for key in (
            "name",
            "namespace",
            "creationTimestamp",
            "generation",
            "resourceVersion",
        )
        if metadata.get(key) is not None
    }
    projected["labels"] = {
        str(key)[:128]: str(value)[:256]
        for key, value in list((metadata.get("labels") or {}).items())[:32]
    }
    if not projected["labels"]:
        projected.pop("labels")
    return projected


def _clip(value: Any, *, depth: int = 0) -> Any:
    """Bound nested API data before it reaches a model context."""

    if depth >= 6:
        return "<depth limit>"
    if isinstance(value, str):
        return value[:2000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_clip(item, depth=depth + 1) for item in value[:32]]
    if isinstance(value, dict):
        return {
            str(key)[:128]: _clip(item, depth=depth + 1)
            for key, item in list(value.items())[:64]
            if key != "managedFields"
        }
    return str(value)[:2000]


def _list_item(rule: ResourceRule, obj: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {"metadata": _metadata(obj)}
    status = obj.get("status") or {}
    if rule.resource == "events":
        for key in (
            "involvedObject",
            "type",
            "reason",
            "message",
            "count",
            "firstTimestamp",
            "lastTimestamp",
            "eventTime",
        ):
            if obj.get(key) is not None:
                row[key] = _clip(obj[key])
    elif rule.api_group == "metrics.k8s.io":
        for key in ("usage", "containers", "timestamp", "window"):
            if obj.get(key) is not None:
                row[key] = _clip(obj[key])
    else:
        selected: dict[str, Any] = {
            key: status[key]
            for key in (
                "phase",
                "readyReplicas",
                "availableReplicas",
                "currentReplicas",
                "updatedReplicas",
                "numberReady",
                "desiredNumberScheduled",
                "sync",
                "health",
            )
            if status.get(key) is not None
        }
        if status.get("conditions"):
            selected["conditions"] = [
                {
                    key: (
                        str(condition[key])[:512]
                        if key == "message"
                        else condition[key]
                    )
                    for key in ("type", "status", "reason", "message")
                    if condition.get(key) is not None
                }
                for condition in status["conditions"][:12]
                if isinstance(condition, dict)
            ]
        if selected:
            row["status"] = _clip(selected)
        if rule.resource == "freights" and obj.get("charts") is not None:
            row["charts"] = _clip(obj["charts"])
    return row


def _container_images(spec: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        key: [
            {field: container.get(field) for field in ("name", "image")}
            for container in (spec.get(key) or [])[:32]
        ]
        for key in ("initContainers", "containers")
        if spec.get(key)
    }


def _application_source(source: dict[str, Any]) -> dict[str, Any]:
    return {
        key: source[key]
        for key in ("repoURL", "path", "chart", "targetRevision", "ref")
        if source.get(key) is not None
    }


def _safe_spec(rule: ResourceRule, obj: dict[str, Any]) -> dict[str, Any] | None:
    """Project operational shape without env values or arbitrary config data."""

    spec = obj.get("spec") or {}
    if rule.resource == "pods":
        return {
            **{
                key: spec[key]
                for key in (
                    "nodeName",
                    "serviceAccountName",
                    "restartPolicy",
                    "priorityClassName",
                )
                if spec.get(key) is not None
            },
            **_container_images(spec),
        }
    if rule.resource in {
        "deployments",
        "statefulsets",
        "daemonsets",
        "replicasets",
    }:
        pod_spec = ((spec.get("template") or {}).get("spec")) or {}
        return {
            **{
                key: _clip(spec[key])
                for key in (
                    "replicas",
                    "selector",
                    "strategy",
                    "updateStrategy",
                    "serviceName",
                )
                if spec.get(key) is not None
            },
            "template": {
                "labels": ((spec.get("template") or {}).get("metadata") or {}).get(
                    "labels", {}
                ),
                **_container_images(pod_spec),
            },
        }
    if rule.resource == "services":
        return {
            key: _clip(spec[key])
            for key in (
                "type",
                "selector",
                "ports",
                "clusterIP",
                "clusterIPs",
                "externalTrafficPolicy",
            )
            if spec.get(key) is not None
        }
    if rule.resource == "nodes":
        return {
            key: _clip(spec[key])
            for key in ("unschedulable", "taints")
            if spec.get(key) is not None
        }
    if rule.resource == "applications":
        result = {
            key: _clip(spec[key])
            for key in ("project", "destination")
            if spec.get(key) is not None
        }
        if isinstance(spec.get("source"), dict):
            result["source"] = _application_source(spec["source"])
        if isinstance(spec.get("sources"), list):
            result["sources"] = [
                _application_source(source)
                for source in spec["sources"][:16]
                if isinstance(source, dict)
            ]
        sync_policy = spec.get("syncPolicy") or {}
        if isinstance(sync_policy, dict):
            result["syncPolicy"] = {
                key: _clip(sync_policy[key])
                for key in ("automated", "syncOptions")
                if sync_policy.get(key) is not None
            }
        return result
    return None


def _detail(rule: ResourceRule, obj: dict[str, Any]) -> dict[str, Any]:
    detail = {
        "apiVersion": obj.get("apiVersion"),
        "kind": obj.get("kind"),
        "metadata": _metadata(obj),
    }
    if rule.resource == "configmaps":
        detail["dataKeys"] = sorted((obj.get("data") or {}).keys())[:128]
        detail["binaryDataKeys"] = sorted((obj.get("binaryData") or {}).keys())[:128]
        detail["immutable"] = obj.get("immutable", False)
    elif rule.resource == "events":
        detail.update(_list_item(rule, obj))
    else:
        safe_spec = _safe_spec(rule, obj)
        if safe_spec:
            detail["spec"] = safe_spec
        if obj.get("status") is not None:
            detail["status"] = _clip(obj["status"])
        for key in ("charts", "commits", "images", "usage", "containers"):
            if obj.get(key) is not None:
                detail[key] = _clip(obj[key])

    encoded = json.dumps(detail, separators=(",", ":"), default=str).encode()
    if len(encoded) <= 48_000:
        return detail
    return {
        "apiVersion": detail.get("apiVersion"),
        "kind": detail.get("kind"),
        "metadata": detail["metadata"],
        "contentTruncated": True,
    }


class RestrictedKubernetesClient:
    """One-page, timeout-bounded client over the fixed read allowlist."""

    def __init__(self) -> None:
        self._api: ApiClient | None = None

    async def _ensure_client(self) -> ApiClient:
        if self._api is None:
            config.load_incluster_config()
            self._api = ApiClient()
        return self._api

    async def read(self, request: ReadRequest) -> ReadResult:
        try:
            async with asyncio.timeout(OPERATION_TIMEOUT_SECONDS):
                api = await self._ensure_client()
                if request.rule.api == "custom":
                    raw, next_token = await self._read_custom(api, request)
                else:
                    raw, next_token = await self._read_typed(api, request)
        except TimeoutError as exc:
            raise ObservationFailure(
                "timeout", "Kubernetes observation timed out"
            ) from exc
        except ObservationFailure:
            raise
        except Exception as exc:
            raise _classify_api_error(exc) from exc

        if request.verb == "get":
            return ReadResult([_detail(request.rule, raw)], None)
        return ReadResult([_list_item(request.rule, item) for item in raw], next_token)

    async def _read_typed(
        self, api: ApiClient, request: ReadRequest
    ) -> tuple[Any, str | None]:
        typed = (
            client.CoreV1Api(api)
            if request.rule.api == "core"
            else client.AppsV1Api(api)
        )
        if request.verb == "get":
            try:
                if request.rule.namespaced:
                    result = await getattr(
                        typed, f"read_namespaced_{request.rule.singular}"
                    )(
                        request.name,
                        request.namespace,
                        _request_timeout=REQUEST_TIMEOUT_SECONDS,
                    )
                else:
                    result = await getattr(typed, f"read_{request.rule.singular}")(
                        request.name, _request_timeout=REQUEST_TIMEOUT_SECONDS
                    )
            except Exception as exc:
                raise _classify_api_error(exc) from exc
            return api.sanitize_for_serialization(result), None

        kwargs = {
            "limit": request.limit,
            "_request_timeout": REQUEST_TIMEOUT_SECONDS,
        }
        if request.continue_token:
            kwargs["_continue"] = request.continue_token
        if request.rule.namespaced:
            result = await getattr(typed, f"list_namespaced_{request.rule.singular}")(
                request.namespace, **kwargs
            )
        else:
            result = await getattr(typed, f"list_{request.rule.singular}")(**kwargs)
        serialized = api.sanitize_for_serialization(result)
        return serialized.get("items", []), _next_token(serialized)

    async def _read_custom(
        self, api: ApiClient, request: ReadRequest
    ) -> tuple[Any, str | None]:
        custom = client.CustomObjectsApi(api)
        common = {
            "group": request.rule.api_group,
            "version": request.rule.version,
            "plural": request.rule.resource,
            "_request_timeout": REQUEST_TIMEOUT_SECONDS,
        }
        if request.verb == "get":
            try:
                if request.rule.namespaced:
                    result = await custom.get_namespaced_custom_object(
                        namespace=request.namespace, name=request.name, **common
                    )
                else:
                    result = await custom.get_cluster_custom_object(
                        name=request.name, **common
                    )
            except Exception as exc:
                raise _classify_api_error(exc) from exc
            return result, None

        common["limit"] = request.limit
        if request.continue_token:
            common["_continue"] = request.continue_token
        if request.rule.namespaced:
            result = await custom.list_namespaced_custom_object(
                namespace=request.namespace, **common
            )
        else:
            result = await custom.list_cluster_custom_object(**common)
        return result.get("items", []), _next_token(result)

    async def pod_logs(
        self,
        *,
        namespace: str,
        pod: str,
        container: str | None,
        tail_lines: int,
        since_seconds: int,
        previous: bool,
    ) -> dict[str, Any]:
        try:
            async with asyncio.timeout(OPERATION_TIMEOUT_SECONDS):
                api = await self._ensure_client()
                text = await client.CoreV1Api(api).read_namespaced_pod_log(
                    name=pod,
                    namespace=namespace,
                    container=container,
                    tail_lines=tail_lines,
                    since_seconds=since_seconds,
                    previous=previous,
                    timestamps=True,
                    _request_timeout=REQUEST_TIMEOUT_SECONDS,
                )
        except TimeoutError as exc:
            raise ObservationFailure(
                "timeout", "Kubernetes log read timed out"
            ) from exc
        except Exception as exc:
            raise _classify_api_error(exc) from exc

        lines = str(text).splitlines()
        line_truncated = len(lines) > tail_lines
        body = "\n".join(lines[-tail_lines:])
        encoded = body.encode("utf-8")
        byte_truncated = len(encoded) > LOG_BYTES_MAX
        if byte_truncated:
            body = encoded[-LOG_BYTES_MAX:].decode("utf-8", "ignore")
        return {
            "logs": body,
            "lines": len(body.splitlines()),
            "truncated": line_truncated or byte_truncated,
        }

    async def close(self) -> None:
        if self._api is not None:
            await self._api.close()
            self._api = None


def _next_token(result: dict[str, Any]) -> str | None:
    metadata = result.get("metadata") or {}
    token = metadata.get("continue") or metadata.get("_continue")
    return token if isinstance(token, str) and token else None
