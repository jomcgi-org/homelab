"""Unit tests for the jobs Typer entrypoint (``app/jobs_main.py``).

These verify command dispatch and the internal POST retry policy. External
network calls and the DB are replaced with mocks, so no real session or HTTP
call happens.
"""

from __future__ import annotations

import json
import logging
from unittest import mock

import httpx
import pytest
from typer.testing import CliRunner

import app.jobs_main as jobs_main
from faas.reconcile import ReconcileReport

runner = CliRunner()


def test_agent_drain_trigger_posts_internal_endpoint(monkeypatch):
    response = mock.Mock()
    response.json.return_value = {"status": "started"}
    monkeypatch.setenv("MONOLITH_INTERNAL_URL", "http://monolith:8000")

    with mock.patch("httpx.post", return_value=response) as post:
        result = runner.invoke(jobs_main.app, ["agent-drain-trigger"])

    assert result.exit_code == 0, result.output
    post.assert_called_once_with(
        "http://monolith:8000/internal/agent/drain", timeout=90
    )
    response.raise_for_status.assert_called_once_with()


def test_ember_spark_trigger_posts_internal_endpoint(monkeypatch):
    response = mock.Mock()
    response.json.return_value = {"spark": {"ok": True}}
    monkeypatch.setenv("MONOLITH_INTERNAL_URL", "http://monolith:8000")

    with mock.patch("httpx.post", return_value=response) as post:
        result = runner.invoke(jobs_main.app, ["ember-spark-synthetic-trigger"])

    assert result.exit_code == 0, result.output
    post.assert_called_once_with(
        "http://monolith:8000/internal/ember/spark-session-probe", timeout=420
    )
    response.raise_for_status.assert_called_once_with()


def test_worldcup_sim_dispatches_to_refresh_handler():
    handler = mock.AsyncMock(return_value=None)
    with (
        mock.patch("core.db.get_engine", return_value=object()),
        mock.patch("sqlmodel.Session"),
        mock.patch("worldcup.jobs.refresh_handler", new=handler),
    ):
        result = runner.invoke(jobs_main.app, ["worldcup-sim"])

    assert result.exit_code == 0, result.output
    handler.assert_awaited_once()


def test_moving_gcal_sync_dispatches_to_handler():
    handler = mock.AsyncMock(return_value=None)
    with (
        mock.patch("core.db.get_engine", return_value=object()),
        mock.patch("sqlmodel.Session"),
        mock.patch("moving.gcal_sync.gcal_sync_handler", new=handler),
    ):
        result = runner.invoke(jobs_main.app, ["moving-gcal-sync"])

    assert result.exit_code == 0, result.output
    handler.assert_awaited_once()


def test_cluster_snapshot_refresh_dispatches_to_refresh():
    refresh = mock.AsyncMock(return_value=None)
    with mock.patch("home.cluster_snapshot.refresh_cluster_snapshot", new=refresh):
        result = runner.invoke(jobs_main.app, ["home-cluster-snapshot-refresh"])

    assert result.exit_code == 0, result.output
    refresh.assert_awaited_once()


def test_faas_reconcile_dispatches_and_prints_json():
    report = ReconcileReport(
        scanned=2,
        orphans=["orphan"],
        deleted=[],
        kept=["serving"],
        skipped_unmarked=0,
        skipped_young=0,
    )
    reconcile = mock.AsyncMock(return_value=report)
    with (
        mock.patch("faas.reconcile.reconcile_orphan_workloads", new=reconcile),
        mock.patch.object(jobs_main, "configure_logging"),
    ):
        result = runner.invoke(jobs_main.app, ["faas-reconcile", "--dry-run"])

    assert result.exit_code == 0, result.output
    reconcile.assert_awaited_once_with(dry_run=True)
    assert json.loads(result.stdout) == {
        "deleted": [],
        "kept": ["serving"],
        "orphans": ["orphan"],
        "scanned": 2,
        "skipped_unmarked": 0,
        "skipped_young": 0,
    }


def test_faas_reconcile_failed_delete_exits_nonzero():
    report = ReconcileReport(
        scanned=1,
        orphans=["stuck"],
        deleted=[],
        kept=[],
        skipped_unmarked=0,
        skipped_young=0,
    )
    reconcile = mock.AsyncMock(return_value=report)
    with (
        mock.patch("faas.reconcile.reconcile_orphan_workloads", new=reconcile),
        mock.patch.object(jobs_main, "configure_logging"),
    ):
        result = runner.invoke(jobs_main.app, ["faas-reconcile"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["orphans"] == ["stuck"]


def test_no_args_lists_commands():
    result = runner.invoke(jobs_main.app, [])
    # no_args_is_help exits non-zero and prints the command list.
    assert "worldcup-sim" in result.output


def test_setup_otel_skips_when_endpoint_is_absent(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)

    assert jobs_main._setup_otel() is None


def test_setup_otel_installs_http_exporter_when_endpoint_is_present(monkeypatch):
    endpoint = "http://collector.example:4318/v1/traces"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", endpoint)
    provider = mock.Mock()
    processor = mock.Mock()

    with (
        mock.patch(
            "opentelemetry.sdk.trace.TracerProvider", return_value=provider
        ) as provider_class,
        mock.patch(
            "opentelemetry.sdk.trace.export.BatchSpanProcessor",
            return_value=processor,
        ) as processor_class,
        mock.patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"
        ) as exporter_class,
        mock.patch("opentelemetry.trace.set_tracer_provider") as set_provider,
    ):
        result = jobs_main._setup_otel()

    assert result is provider
    exporter_class.assert_called_once_with(endpoint=endpoint)
    processor_class.assert_called_once_with(exporter_class.return_value)
    provider.add_span_processor.assert_called_once_with(processor)
    set_provider.assert_called_once_with(provider)
    resource = provider_class.call_args.kwargs["resource"]
    assert resource.attributes["service.name"] == "monolith-jobs"


def test_shutdown_otel_flushes_and_shuts_down():
    provider = mock.Mock()
    provider.force_flush.return_value = True

    jobs_main._shutdown_otel(provider)

    provider.force_flush.assert_called_once_with()
    provider.shutdown.assert_called_once_with()


def test_post_internal_retries_remote_protocol_errors(monkeypatch, caplog):
    monkeypatch.setenv("MONOLITH_INTERNAL_URL", "http://monolith")
    monkeypatch.setattr(jobs_main, "configure_logging", lambda: None)
    monkeypatch.setattr(jobs_main.time, "sleep", lambda _delay: None)
    caplog.set_level(logging.INFO, logger="monolith.jobs")
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise httpx.RemoteProtocolError("rollout disconnect", request=request)
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(httpx, "post", client.post)
        jobs_main._post_internal("/internal/agent/drain", "agent-drain-trigger")

    warnings = [
        record
        for record in caplog.records
        if record.name == "monolith.jobs" and record.levelno == logging.WARNING
    ]
    assert calls == 3
    assert len(warnings) == 2
    assert all("RemoteProtocolError" in record.getMessage() for record in warnings)
    assert "attempt 1/6" in warnings[0].getMessage()
    assert "in 2s" in warnings[0].getMessage()
    assert "attempt 2/6" in warnings[1].getMessage()
    assert "in 5s" in warnings[1].getMessage()
    assert "succeeded after 2 retries" in caplog.text


def test_post_internal_retries_503(monkeypatch):
    monkeypatch.setenv("MONOLITH_INTERNAL_URL", "http://monolith")
    monkeypatch.setattr(jobs_main, "configure_logging", lambda: None)
    monkeypatch.setattr(jobs_main.time, "sleep", lambda _delay: None)
    statuses = iter([503, 200])
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(next(statuses), json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(httpx, "post", client.post)
        jobs_main._post_internal("/internal/agent/drain", "agent-drain-trigger")

    assert calls == 2


def test_post_internal_persistent_connect_error_exhausts_budget(monkeypatch):
    monkeypatch.setenv("MONOLITH_INTERNAL_URL", "http://monolith")
    monkeypatch.setattr(jobs_main, "configure_logging", lambda: None)
    sleeps = []
    monkeypatch.setattr(jobs_main.time, "sleep", sleeps.append)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("backend unavailable", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(httpx, "post", client.post)
        with pytest.raises(httpx.ConnectError, match="backend unavailable"):
            jobs_main._post_internal("/internal/agent/drain", "agent-drain-trigger")

    assert calls == len(jobs_main._INTERNAL_POST_RETRY_DELAYS_S) + 1
    assert sleeps == list(jobs_main._INTERNAL_POST_RETRY_DELAYS_S)


def test_post_internal_does_not_retry_400(monkeypatch):
    monkeypatch.setenv("MONOLITH_INTERNAL_URL", "http://monolith")
    monkeypatch.setattr(jobs_main, "configure_logging", lambda: None)
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"detail": "bad request"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(httpx, "post", client.post)
        with pytest.raises(httpx.HTTPStatusError):
            jobs_main._post_internal("/internal/agent/drain", "agent-drain-trigger")

    assert calls == 1
