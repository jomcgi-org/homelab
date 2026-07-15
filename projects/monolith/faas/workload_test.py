"""Tests for the EmberVM Workload CR client (faas.workload).

The kubernetes_asyncio CustomObjectsApi is faked and injected via a monkeypatched
``_custom_objects_api`` factory. Covers build_workload_spec's shape (must match
the CRD sample), the create-then-409-patch upsert path, and wait_ready's
condition handling (Ready True, Ready False early exit, timeout).
"""

from __future__ import annotations

import os

import pytest
import yaml
from kubernetes_asyncio.client import exceptions as k8s_exceptions

from faas import workload


class _FakeApi:
    """Fake CustomObjectsApi recording calls and returning scripted results."""

    def __init__(
        self,
        *,
        create_conflict: bool = False,
        get_objects: list | None = None,
    ) -> None:
        self.create_conflict = create_conflict
        self._get_objects = list(get_objects or [])
        self.created = []
        self.patched = []
        self.deleted = []
        self.get_count = 0

    async def create_namespaced_custom_object(self, **kwargs):
        self.created.append(kwargs)
        if self.create_conflict:
            raise k8s_exceptions.ApiException(status=409, reason="Conflict")
        return kwargs["body"]

    async def patch_namespaced_custom_object(self, **kwargs):
        self.patched.append(kwargs)
        return kwargs["body"]

    async def get_namespaced_custom_object(self, **kwargs):
        self.get_count += 1
        if self._get_objects:
            return self._get_objects.pop(0)
        return {}

    async def delete_namespaced_custom_object(self, **kwargs):
        self.deleted.append(kwargs)


def _install(monkeypatch, api: _FakeApi):
    async def _factory():
        return api

    monkeypatch.setattr(workload, "_custom_objects_api", _factory)


# --------------------------------------------------------------------------- #
# build_workload_spec shape
# --------------------------------------------------------------------------- #


def test_build_workload_spec_matches_crd_sample():
    spec = workload.build_workload_spec(
        code_uri="http://s3/faas/echo-fn/abc.zip",
        sha256="abc",
        handler="app.handle",
    )
    assert spec == {
        "class": "task",
        "source": {
            "zip": {
                "runtime": "python312",
                "codeUri": "http://s3/faas/echo-fn/abc.zip",
                "sha256": "abc",
                "handler": "app.handle",
                "invokePath": "/invoke",
                "readyPath": "/shim/ready",
            }
        },
        "resources": {"vcpus": 1, "memMib": 512},
        "concurrency": {"floor": 1, "cap": 4},
        "invocation": {"timeoutSeconds": 30},
    }


def test_build_workload_spec_shape_equals_checked_in_sample():
    # Guard against drift from the committed echo sample's spec block.
    sample_path = "projects/embervm/crd/samples/workload-echo-fn.yaml"

    # Walk up to the repo root so the test runs from the bazel runfiles too.
    here = os.path.dirname(os.path.abspath(__file__))
    root = here
    for _ in range(8):
        candidate = os.path.join(root, sample_path)
        if os.path.exists(candidate):
            sample = yaml.safe_load(open(candidate))
            zip_src = sample["spec"]["source"]["zip"]
            built = workload.build_workload_spec(
                code_uri=zip_src["codeUri"],
                sha256=zip_src["sha256"],
                handler=zip_src["handler"],
            )
            assert built["source"]["zip"].keys() == zip_src.keys()
            assert built["resources"] == sample["spec"]["resources"]
            assert built["concurrency"] == sample["spec"]["concurrency"]
            assert built["invocation"] == sample["spec"]["invocation"]
            return
        root = os.path.dirname(root)
    pytest.skip("crd sample not reachable from runfiles")


# --------------------------------------------------------------------------- #
# upsert_workload
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_upsert_creates_when_absent(monkeypatch):
    api = _FakeApi(create_conflict=False)
    _install(monkeypatch, api)
    await workload.upsert_workload("echo-fn", {"class": "task"})
    assert len(api.created) == 1
    assert api.patched == []
    body = api.created[0]["body"]
    assert body["metadata"]["name"] == "echo-fn"
    assert body["metadata"]["namespace"] == "embervm"
    assert body["apiVersion"] == "embervm.dev/v1alpha1"


@pytest.mark.asyncio
async def test_upsert_patches_on_conflict(monkeypatch):
    api = _FakeApi(create_conflict=True)
    _install(monkeypatch, api)
    await workload.upsert_workload("echo-fn", {"class": "task"})
    assert len(api.created) == 1  # attempted
    assert len(api.patched) == 1  # then merge-patched
    assert api.patched[0]["_content_type"] == "application/merge-patch+json"
    assert api.patched[0]["body"] == {"spec": {"class": "task"}}


# --------------------------------------------------------------------------- #
# wait_ready
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wait_ready_true(monkeypatch):
    api = _FakeApi(
        get_objects=[{"status": {"conditions": [{"type": "Ready", "status": "True"}]}}]
    )
    _install(monkeypatch, api)
    ok, msg = await workload.wait_ready("echo-fn", timeout_s=5, poll_s=0)
    assert ok is True
    assert msg == ""


@pytest.mark.asyncio
async def test_wait_ready_false_exits_early_with_message(monkeypatch):
    api = _FakeApi(
        get_objects=[
            {
                "status": {
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "False",
                            "message": "import failed: no module named app",
                        }
                    ]
                }
            }
        ]
    )
    _install(monkeypatch, api)
    ok, msg = await workload.wait_ready("echo-fn", timeout_s=5, poll_s=0)
    assert ok is False
    assert "import failed" in msg
    assert api.get_count == 1  # early exit, did not keep polling


@pytest.mark.asyncio
async def test_wait_ready_times_out(monkeypatch):
    # No Ready condition ever appears; wait_ready gives up at the deadline.
    api = _FakeApi(get_objects=[])
    _install(monkeypatch, api)
    ok, msg = await workload.wait_ready("echo-fn", timeout_s=0, poll_s=0)
    assert ok is False
    assert "timed out" in msg


# --------------------------------------------------------------------------- #
# delete_workload
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_delete_workload_ignores_404(monkeypatch):
    class _NotFoundApi(_FakeApi):
        async def delete_namespaced_custom_object(self, **kwargs):
            raise k8s_exceptions.ApiException(status=404, reason="Not Found")

    api = _NotFoundApi()
    _install(monkeypatch, api)
    # Should not raise.
    await workload.delete_workload("gone")


@pytest.mark.asyncio
async def test_delete_workload_deletes(monkeypatch):
    api = _FakeApi()
    _install(monkeypatch, api)
    await workload.delete_workload("echo-fn")
    assert api.deleted[0]["name"] == "echo-fn"
    assert api.deleted[0]["namespace"] == "embervm"
