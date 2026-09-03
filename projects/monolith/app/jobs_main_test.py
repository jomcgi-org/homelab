"""Unit tests for the jobs Typer entrypoint (``app/jobs_main.py``).

These verify command dispatch only: the network (worldcup poll) and the DB are
patched, so no real session or HTTP call happens. They exist to prove the CLI
wiring stays intact as commands are added.
"""

from __future__ import annotations

from unittest import mock

from typer.testing import CliRunner

import app.jobs_main as jobs_main

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


def test_post_internal_retries_follower_503(monkeypatch):
    """A follower replica answers 503; the trigger retries on a fresh
    connection until the leader answers (#5590)."""
    import httpx

    from app import jobs_main

    monkeypatch.setenv("MONOLITH_INTERNAL_URL", "http://monolith")
    monkeypatch.setattr(jobs_main, "_INTERNAL_POST_RETRY_SECONDS", 0)
    responses = iter(
        [
            httpx.Response(503, request=httpx.Request("POST", "http://monolith/x")),
            httpx.Response(503, request=httpx.Request("POST", "http://monolith/x")),
            httpx.Response(
                200,
                json={"ok": True},
                request=httpx.Request("POST", "http://monolith/x"),
            ),
        ]
    )
    calls = []

    def fake_post(url, timeout):
        calls.append(url)
        return next(responses)

    monkeypatch.setattr(httpx, "post", fake_post)
    jobs_main._post_internal("/internal/agent/drain", "agent-drain-trigger", timeout=1)
    assert len(calls) == 3


def test_post_internal_gives_up_after_attempts(monkeypatch):
    import httpx
    import pytest

    from app import jobs_main

    monkeypatch.setenv("MONOLITH_INTERNAL_URL", "http://monolith")
    monkeypatch.setattr(jobs_main, "_INTERNAL_POST_RETRY_SECONDS", 0)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, timeout: httpx.Response(
            503, request=httpx.Request("POST", "http://monolith/x")
        ),
    )
    with pytest.raises(httpx.HTTPStatusError):
        jobs_main._post_internal(
            "/internal/agent/drain", "agent-drain-trigger", timeout=1
        )
