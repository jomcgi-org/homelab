"""Async Kubernetes client for the EmberVM ``Workload`` custom resource (FaaS).

The FaaS ingestion API (Task 10) owns a function's ``Workload`` CR: it upserts
it on registration and deletes it on removal / rollback. A function IS a Workload
(standing decision 1); these CRs are data, created dynamically in the ``embervm``
namespace and NOT tracked by any ArgoCD Application (standing decision 5).

This is a self-contained ``kubernetes_asyncio`` wrapper mirroring
``cluster/kubernetes.py``'s ``_ensure_client``. It deliberately does NOT import
``cluster.*`` internals: cross-domain imports are forbidden by
``import_boundaries_test``. The CR coordinates are verified against
``projects/embervm/chart/crds/workload-crd.yaml`` (group ``embervm.dev``, version
``v1alpha1``, plural ``workloads``, namespaced, status is a subresource).

The client factory is a module-level function so tests can monkeypatch it to
return a fake exposing the async CR methods.
"""

from __future__ import annotations

import asyncio
import logging

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiClient

logger = logging.getLogger(__name__)

GROUP = "embervm.dev"
VERSION = "v1alpha1"
PLURAL = "workloads"
NAMESPACE = "embervm"
MANAGED_BY_LABEL = "monolith.jomcgi.dev/managed-by"
MANAGED_BY_VALUE = "faas"


async def _custom_objects_api() -> "client.CustomObjectsApi":
    """Build a namespaced CustomObjectsApi for the embervm CR group.

    Mirrors cluster/kubernetes.py ``_ensure_client``: in-cluster config, a fresh
    ApiClient, a CustomObjectsApi over it. Tests monkeypatch this factory to
    return a fake with the same async method surface.
    """
    config.load_incluster_config()
    return client.CustomObjectsApi(ApiClient())


def build_workload_spec(
    *,
    code_uri: str,
    sha256: str,
    handler: str,
    runtime: str = "python312",
    invoke_path: str = "/invoke",
    ready_path: str = "/shim/ready",
) -> dict:
    """Build a ``Workload.spec`` for a zip-source function.

    Shape matches projects/embervm/crd/samples/workload-echo-fn.yaml exactly: a
    ``task``-class zip source with the runtime base, code URI + sha256, handler,
    and the guest invoke/ready paths, plus the R1 default resource/concurrency/
    invocation blocks (1 vcpu, 512 MiB, floor 1 / cap 4, 30s timeout).
    """
    return {
        "class": "task",
        "source": {
            "zip": {
                "runtime": runtime,
                "codeUri": code_uri,
                "sha256": sha256,
                "handler": handler,
                "invokePath": invoke_path,
                "readyPath": ready_path,
            }
        },
        "resources": {"vcpus": 1, "memMib": 512},
        "concurrency": {"floor": 1, "cap": 4},
        "invocation": {"timeoutSeconds": 30},
    }


async def upsert_workload(name: str, spec: dict) -> None:
    """Create the Workload CR, or merge-patch its spec if it already exists.

    Last-write-wins registration (standing decision 6): a re-registered name
    replaces the spec, which the control plane change-detects (new zip sha256)
    and rebuilds. On the first registration ``create`` succeeds; on a subsequent
    one it 409s and we patch.
    """
    api = await _custom_objects_api()
    body = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "Workload",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {MANAGED_BY_LABEL: MANAGED_BY_VALUE},
        },
        "spec": spec,
    }
    try:
        await api.create_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=NAMESPACE,
            plural=PLURAL,
            body=body,
        )
    except client.exceptions.ApiException as exc:
        if exc.status != 409:
            raise
        # Already exists: merge-patch the spec (last-write-wins).
        await api.patch_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=NAMESPACE,
            plural=PLURAL,
            name=name,
            body={
                "metadata": {
                    "labels": {MANAGED_BY_LABEL: MANAGED_BY_VALUE},
                },
                "spec": spec,
            },
            _content_type="application/merge-patch+json",
        )


async def wait_ready(
    name: str, timeout_s: int = 180, poll_s: int = 3
) -> tuple[bool, str]:
    """Poll the Workload's status until a ``Ready`` condition resolves.

    Returns ``(True, "")`` once a ``type=Ready`` condition has ``status=True``.
    Returns ``(False, message)`` early if a ``Ready`` condition is
    ``status=False`` (the base build / import failed; the condition message is
    surfaced to the caller). Returns ``(False, "timed out waiting for Ready")``
    if no terminal Ready condition appears within ``timeout_s``.

    Uses ``asyncio.sleep`` between polls so it never blocks the event loop.
    """
    api = await _custom_objects_api()
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        try:
            obj = await api.get_namespaced_custom_object(
                group=GROUP,
                version=VERSION,
                namespace=NAMESPACE,
                plural=PLURAL,
                name=name,
            )
        except client.exceptions.ApiException as exc:
            # A transient 404 right after create is possible; keep polling until
            # the deadline rather than failing the registration outright.
            logger.debug("wait_ready get failed for %s: %s", name, exc)
            obj = None

        conditions = ((obj or {}).get("status") or {}).get("conditions") or []
        for cond in conditions:
            if cond.get("type") != "Ready":
                continue
            status = cond.get("status")
            if status == "True":
                return True, ""
            if status == "False":
                return False, cond.get("message", "Ready=False")

        if asyncio.get_event_loop().time() >= deadline:
            return False, "timed out waiting for Ready"
        await asyncio.sleep(poll_s)


async def delete_workload(name: str) -> None:
    """Delete the Workload CR, ignoring a 404 (already gone)."""
    api = await _custom_objects_api()
    try:
        await api.delete_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=NAMESPACE,
            plural=PLURAL,
            name=name,
        )
    except client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise
