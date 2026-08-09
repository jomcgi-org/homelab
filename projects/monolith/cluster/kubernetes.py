"""Async Kubernetes client wrapper for cluster queries.

Thin layer over kubernetes_asyncio, shared by the home observability stats
and the ``k8s-*`` debug MCP tools (``cluster/``). Read-only except for one
explicitly-named mutation: ``sync_argocd_app`` patches an Application's
``.operation`` field to trigger a sync (the same mechanism as
``argocd app sync``). Keep the interface minimal."""

from __future__ import annotations

import asyncio
import json
import logging

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiClient

logger = logging.getLogger(__name__)


_CPU_SUFFIXES = {"n": 1e-9, "u": 1e-6, "m": 1e-3}
_MEM_SUFFIXES = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
}


def _parse_cpu(s: str) -> float:
    """Parse a Kubernetes CPU quantity to cores (e.g. '618m' → 0.618)."""
    if not s:
        return 0.0
    if s[-1] in _CPU_SUFFIXES:
        return float(s[:-1]) * _CPU_SUFFIXES[s[-1]]
    return float(s)


def _parse_memory(s: str) -> float:
    """Parse a Kubernetes memory quantity to bytes."""
    if not s:
        return 0.0
    for suffix, mult in _MEM_SUFFIXES.items():
        if s.endswith(suffix):
            return float(s[: -len(suffix)]) * mult
    return float(s)


# Curated allowlist of debuggable kinds. alias -> (api group "core"|"apps",
# method singular, namespaced). The method singular is the snake_case form the
# kubernetes client bakes into method names (e.g. config_map ->
# read_namespaced_config_map). ArgoCD Applications are handled separately via
# the custom-object API.
_KINDS: dict[str, tuple[str, str, bool]] = {
    "pods": ("core", "pod", True),
    "services": ("core", "service", True),
    "configmaps": ("core", "config_map", True),
    "events": ("core", "event", True),
    "namespaces": ("core", "namespace", False),
    "nodes": ("core", "node", False),
    "deployments": ("apps", "deployment", True),
    "statefulsets": ("apps", "stateful_set", True),
    "daemonsets": ("apps", "daemon_set", True),
    "replicasets": ("apps", "replica_set", True),
}

_ARGO = ("argoproj.io", "v1alpha1", "applications")

# Kinds usable from the generic list/get tools, plus the argo alias.
RESOURCE_KINDS = sorted([*_KINDS.keys(), "applications"])


class UnknownKindError(ValueError):
    """Raised when a caller asks for a kind outside the curated allowlist."""


class KubernetesClient:
    """Lightweight async k8s client scoped to list operations."""

    def __init__(self) -> None:
        self._api: ApiClient | None = None

    async def _ensure_client(self) -> ApiClient:
        if self._api is None:
            config.load_incluster_config()
            self._api = ApiClient()
        return self._api

    async def count_nodes(self) -> int:
        api = await self._ensure_client()
        v1 = client.CoreV1Api(api)
        nodes = await v1.list_node()
        return len(nodes.items)

    async def count_pods(self) -> int:
        api = await self._ensure_client()
        v1 = client.CoreV1Api(api)
        pods = await v1.list_pod_for_all_namespaces()
        return len(pods.items)

    async def count_deployments(self) -> int:
        api = await self._ensure_client()
        apps = client.AppsV1Api(api)
        deps = await apps.list_deployment_for_all_namespaces()
        return len(deps.items)

    async def count_argocd_applications(self) -> int:
        api = await self._ensure_client()
        custom = client.CustomObjectsApi(api)
        result = await custom.list_namespaced_custom_object(
            group="argoproj.io",
            version="v1alpha1",
            namespace="argocd",
            plural="applications",
        )
        return len(result.get("items", []))

    async def list_argocd_app_health(self, namespace: str = "argocd") -> list[dict]:
        """Return {name, sync, health, finished_at} for every ArgoCD Application.

        One list call rather than N gets, because the cd health component reads
        every app on each probe. `finished_at` is the last sync operation's
        finish time, which is what dates a fault: an app that has been
        not-Synced since a timestamp is distinguishable from one mid-rollout.
        """
        api = await self._ensure_client()
        custom = client.CustomObjectsApi(api)
        result = await custom.list_namespaced_custom_object(
            group="argoproj.io",
            version="v1alpha1",
            namespace=namespace,
            plural="applications",
        )
        apps = []
        for item in result.get("items", []):
            status = item.get("status") or {}
            apps.append(
                {
                    "name": (item.get("metadata") or {}).get("name", "?"),
                    "sync": (status.get("sync") or {}).get("status"),
                    "health": (status.get("health") or {}).get("status"),
                    "finished_at": (status.get("operationState") or {}).get(
                        "finishedAt"
                    ),
                }
            )
        return apps

    async def get_argocd_app_status(
        self, name: str, namespace: str = "argocd"
    ) -> dict | None:
        """Return the raw status block of a single ArgoCD Application, or None on miss.

        Lets callers pull whatever subfield they need (operationState.finishedAt,
        sync.revision, health.status, ...) without baking field choices into the
        client.
        """
        api = await self._ensure_client()
        custom = client.CustomObjectsApi(api)
        try:
            result = await custom.get_namespaced_custom_object(
                group="argoproj.io",
                version="v1alpha1",
                namespace=namespace,
                plural="applications",
                name=name,
            )
        except client.exceptions.ApiException:
            return None
        return result.get("status")

    async def _node_rss_bytes(self, v1: "client.CoreV1Api", node: str) -> float | None:
        """Anonymous resident memory for one node, from the kubelet Summary API.

        Returns node.memory.rssBytes from /stats/summary, or None when the
        summary (or the rssBytes field) is unavailable so the caller can fall
        back. rssBytes excludes ALL file-backed page cache, unlike the
        metrics-server "working set" which counts active page cache (e.g. the
        multi-GB model-weight file reads on the GPU node) and overstates real
        memory consumption by tens of GiB.
        """
        try:
            raw = await v1.connect_get_node_proxy_with_path(
                name=node, path="stats/summary"
            )
            data = json.loads(raw) if isinstance(raw, str) else raw
            rss = (data.get("node", {}).get("memory", {}) or {}).get("rssBytes")
            return float(rss) if rss is not None else None
        except Exception:
            logger.warning(
                "kubelet summary unavailable for node %s; falling back to "
                "working-set memory",
                node,
                exc_info=False,
            )
            return None

    async def aggregate_node_resources(self) -> dict[str, float]:
        """Sum CPU and memory across all nodes.

        Returns cores and bytes. Capacity comes from node.status.allocatable
        (what the scheduler can actually assign), not status.capacity. CPU usage
        comes from the metrics API. Memory "used" is anonymous RSS from the
        kubelet Summary API (see _node_rss_bytes) rather than the metrics-server
        working set, because working set includes reclaimable page cache and
        wildly overstates real usage on nodes that mmap/read large files.
        """
        api = await self._ensure_client()
        v1 = client.CoreV1Api(api)
        custom = client.CustomObjectsApi(api)

        nodes_resp, metrics_resp = await asyncio.gather(
            v1.list_node(),
            custom.list_cluster_custom_object(
                group="metrics.k8s.io", version="v1beta1", plural="nodes"
            ),
        )

        cpu_cap = mem_cap = 0.0
        for n in nodes_resp.items:
            alloc = n.status.allocatable or {}
            cpu_cap += _parse_cpu(alloc.get("cpu", "0"))
            mem_cap += _parse_memory(alloc.get("memory", "0"))

        # CPU usage and a per-node working-set fallback for memory.
        cpu_used = 0.0
        mem_ws_by_node: dict[str, float] = {}
        for item in metrics_resp.get("items", []):
            usage = item.get("usage", {})
            cpu_used += _parse_cpu(usage.get("cpu", "0"))
            name = item.get("metadata", {}).get("name", "")
            mem_ws_by_node[name] = _parse_memory(usage.get("memory", "0"))

        # Honest memory: kubelet rssBytes per node, fall back to working set on miss.
        node_names = [n.metadata.name for n in nodes_resp.items]
        rss_list = await asyncio.gather(
            *(self._node_rss_bytes(v1, name) for name in node_names)
        )
        mem_used = 0.0
        for name, rss in zip(node_names, rss_list):
            mem_used += rss if rss is not None else mem_ws_by_node.get(name, 0.0)

        return {
            "cpu_used_cores": cpu_used,
            "cpu_capacity_cores": cpu_cap,
            "memory_used_bytes": mem_used,
            "memory_capacity_bytes": mem_cap,
        }

    async def pod_metrics(self, namespace: str) -> list[dict]:
        """Return raw pod-metrics objects for a namespace (metrics.k8s.io).

        Each item carries ``metadata.name`` and a ``containers`` list of
        ``{name, usage: {cpu, memory}}``. Used by the load-test sampler to read
        the fc-invoke pod's cpu/rss footprint. Requires the ``metrics.k8s.io``
        ``pods`` ``get``/``list`` RBAC verbs (see chart/templates/rbac.yaml).
        """
        api = await self._ensure_client()
        custom = client.CustomObjectsApi(api)
        result = await custom.list_namespaced_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            namespace=namespace,
            plural="pods",
        )
        return result.get("items", [])

    def _typed_api(self, group: str, api: ApiClient):
        return client.CoreV1Api(api) if group == "core" else client.AppsV1Api(api)

    async def list_resources(
        self,
        kind: str,
        namespace: str | None = None,
        label_selector: str | None = None,
    ) -> list[dict]:
        """List a curated kind, returning sanitized dicts (raw, untrimmed).

        Cluster-scoped kinds (nodes, namespaces) ignore ``namespace``.
        ``applications`` lists ArgoCD apps from the argocd namespace.
        """
        api = await self._ensure_client()
        if kind == "applications":
            custom = client.CustomObjectsApi(api)
            result = await custom.list_namespaced_custom_object(
                group=_ARGO[0],
                version=_ARGO[1],
                namespace=namespace or "argocd",
                plural=_ARGO[2],
                label_selector=label_selector,
            )
            return result.get("items", [])

        if kind not in _KINDS:
            raise UnknownKindError(kind)
        group, singular, namespaced = _KINDS[kind]
        typed = self._typed_api(group, api)
        kwargs = {"label_selector": label_selector} if label_selector else {}
        if not namespaced:
            resp = await getattr(typed, f"list_{singular}")(**kwargs)
        elif namespace:
            resp = await getattr(typed, f"list_namespaced_{singular}")(
                namespace, **kwargs
            )
        else:
            resp = await getattr(typed, f"list_{singular}_for_all_namespaces")(**kwargs)
        return [api.sanitize_for_serialization(item) for item in resp.items]

    async def get_resource(
        self, kind: str, name: str, namespace: str | None = None
    ) -> dict | None:
        """Get a single curated resource as a sanitized dict, or None on miss."""
        api = await self._ensure_client()
        if kind == "applications":
            try:
                return await client.CustomObjectsApi(api).get_namespaced_custom_object(
                    group=_ARGO[0],
                    version=_ARGO[1],
                    namespace=namespace or "argocd",
                    plural=_ARGO[2],
                    name=name,
                )
            except client.exceptions.ApiException:
                return None

        if kind not in _KINDS:
            raise UnknownKindError(kind)
        group, singular, namespaced = _KINDS[kind]
        typed = self._typed_api(group, api)
        try:
            if namespaced:
                obj = await getattr(typed, f"read_namespaced_{singular}")(
                    name, namespace or "default"
                )
            else:
                obj = await getattr(typed, f"read_{singular}")(name)
        except client.exceptions.ApiException:
            return None
        return api.sanitize_for_serialization(obj)

    async def get_pod_logs(
        self,
        namespace: str,
        name: str,
        container: str | None = None,
        tail_lines: int = 200,
        since_seconds: int | None = None,
        previous: bool = False,
    ) -> str:
        """Read a pod's logs. Caller is responsible for any further trimming."""
        api = await self._ensure_client()
        v1 = client.CoreV1Api(api)
        return await v1.read_namespaced_pod_log(
            name=name,
            namespace=namespace,
            container=container,
            tail_lines=tail_lines,
            since_seconds=since_seconds,
            previous=previous,
            timestamps=True,
        )

    async def list_events(
        self, namespace: str | None = None, involved_object: str | None = None
    ) -> list[dict]:
        """List core events, optionally scoped to a namespace and/or object name."""
        api = await self._ensure_client()
        v1 = client.CoreV1Api(api)
        field_selector = (
            f"involvedObject.name={involved_object}" if involved_object else None
        )
        if namespace:
            resp = await v1.list_namespaced_event(
                namespace, field_selector=field_selector
            )
        else:
            resp = await v1.list_event_for_all_namespaces(field_selector=field_selector)
        return [api.sanitize_for_serialization(item) for item in resp.items]

    async def sync_argocd_app(
        self,
        name: str,
        namespace: str = "argocd",
        prune: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Trigger an ArgoCD sync by patching the Application's ``.operation``.

        This is the same mechanism ``argocd app sync`` uses under the hood: the
        application controller watches ``.operation`` and executes it. Note it
        bypasses ArgoCD's own RBAC/audit — gated only by the K8s ``patch`` verb.
        """
        api = await self._ensure_client()
        custom = client.CustomObjectsApi(api)
        body = {
            "operation": {
                "initiatedBy": {"username": "monolith-k8s-mcp"},
                "sync": {"prune": prune, "dryRun": dry_run},
            }
        }
        await custom.patch_namespaced_custom_object(
            group=_ARGO[0],
            version=_ARGO[1],
            namespace=namespace,
            plural=_ARGO[2],
            name=name,
            body=body,
            _content_type="application/merge-patch+json",
        )
        return {"app": name, "synced": True, "prune": prune, "dry_run": dry_run}

    async def create_workflow(self, namespace: str, body: dict) -> str:
        """Create an Argo Workflow custom resource; return its server-assigned name.

        Used by the scheduler to dispatch a batch job to the Argo controller in
        the monolith-workflows namespace (off-pod execution). ``body`` is a full
        Workflow manifest (e.g. from Hera's ``Workflow.to_dict()``).
        """
        api = await self._ensure_client()
        custom = client.CustomObjectsApi(api)
        created = await custom.create_namespaced_custom_object(
            group="argoproj.io",
            version="v1alpha1",
            namespace=namespace,
            plural="workflows",
            body=body,
        )
        return created["metadata"]["name"]

    async def close(self) -> None:
        if self._api:
            await self._api.close()
            self._api = None
