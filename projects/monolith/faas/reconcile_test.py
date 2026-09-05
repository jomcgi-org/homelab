"""Tests for orphan FaaS Workload reconciliation."""

from __future__ import annotations

import logging

import pytest

from faas import reconcile, workload


class _FakeApi:
    """Fake CustomObjectsApi returning a fixed Workload list."""

    def __init__(self, items: list[dict]) -> None:
        self.items = items
        self.listed = []

    async def list_namespaced_custom_object(self, **kwargs):
        self.listed.append(kwargs)
        return {"items": self.items}


def _workload(name: str, *, marked: bool = True, spec: dict | None = None) -> dict:
    labels = {workload.MANAGED_BY_LABEL: workload.MANAGED_BY_VALUE} if marked else {}
    return {
        "metadata": {"name": name, "labels": labels},
        "spec": spec or {"class": "task"},
    }


def _install(monkeypatch, api: _FakeApi, *, function_names: set[str]) -> None:
    async def _factory():
        return api

    monkeypatch.setattr(workload, "_custom_objects_api", _factory)
    monkeypatch.setattr(reconcile, "_registered_function_names", lambda: function_names)


@pytest.mark.asyncio
async def test_marked_orphan_is_deleted(monkeypatch):
    api = _FakeApi([_workload("orphan")])
    _install(monkeypatch, api, function_names=set())
    deleted = []

    async def _delete(name: str) -> None:
        deleted.append(name)

    monkeypatch.setattr(workload, "delete_workload", _delete)

    report = await reconcile.reconcile_orphan_workloads()

    assert report == reconcile.ReconcileReport(
        scanned=1,
        orphans=["orphan"],
        deleted=["orphan"],
        kept=[],
        skipped_unmarked=0,
    )
    assert deleted == ["orphan"]
    assert api.listed == [
        {
            "group": workload.GROUP,
            "version": workload.VERSION,
            "namespace": workload.NAMESPACE,
            "plural": workload.PLURAL,
        }
    ]


@pytest.mark.asyncio
async def test_marked_workload_with_row_is_kept_despite_spec_drift(monkeypatch):
    api = _FakeApi([_workload("serving", spec={"different": True})])
    _install(monkeypatch, api, function_names={"serving"})
    deleted = []

    async def _delete(name: str) -> None:
        deleted.append(name)

    monkeypatch.setattr(workload, "delete_workload", _delete)

    report = await reconcile.reconcile_orphan_workloads()

    assert report.kept == ["serving"]
    assert report.orphans == []
    assert deleted == []


@pytest.mark.asyncio
async def test_unmarked_workload_is_ignored_and_counted(monkeypatch):
    api = _FakeApi([_workload("foreign", marked=False)])
    _install(monkeypatch, api, function_names=set())
    deleted = []

    async def _delete(name: str) -> None:
        deleted.append(name)

    monkeypatch.setattr(workload, "delete_workload", _delete)

    report = await reconcile.reconcile_orphan_workloads()

    assert report.scanned == 1
    assert report.skipped_unmarked == 1
    assert report.orphans == []
    assert deleted == []


@pytest.mark.asyncio
async def test_dry_run_reports_orphan_without_deleting(monkeypatch):
    api = _FakeApi([_workload("orphan")])
    _install(monkeypatch, api, function_names=set())
    deleted = []

    async def _delete(name: str) -> None:
        deleted.append(name)

    monkeypatch.setattr(workload, "delete_workload", _delete)

    report = await reconcile.reconcile_orphan_workloads(dry_run=True)

    assert report.orphans == ["orphan"]
    assert report.deleted == []
    assert deleted == []


@pytest.mark.asyncio
async def test_failed_delete_does_not_stop_sweep(monkeypatch, caplog):
    api = _FakeApi([_workload("broken"), _workload("deleted")])
    _install(monkeypatch, api, function_names=set())
    attempted = []
    caplog.set_level(logging.INFO, logger="faas.reconcile")

    async def _delete(name: str) -> None:
        attempted.append(name)
        if name == "broken":
            raise RuntimeError("delete failed")

    monkeypatch.setattr(workload, "delete_workload", _delete)

    report = await reconcile.reconcile_orphan_workloads()

    assert attempted == ["broken", "deleted"]
    assert report.orphans == ["broken", "deleted"]
    assert report.deleted == ["deleted"]
    orphan_logs = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("faas orphan workload")
    ]
    assert orphan_logs == [
        "faas orphan workload name=broken deleted=false",
        "faas orphan workload name=deleted deleted=true",
    ]
