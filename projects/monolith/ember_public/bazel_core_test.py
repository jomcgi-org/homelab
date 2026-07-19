"""Tests for the bazel skyframe query demo core (validation, gate, error mapping).

Mirrors router_test.py's style: no real EmberVM, no real bazel; every
faas.embervm_client.submit call is stubbed.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

import ember_public.bazel_core as bazel_core
from faas.embervm_client import EmberVMTimeout, EmberVMTransportError


@pytest.fixture(autouse=True)
def _reset_bazel_core_module_state():
    """The semaphore and rate-limit bucket are process-global; reset around
    every test so back-to-back tests do not leak acquired slots or bucket
    entries into each other."""

    def _reset():
        bazel_core._rate_bucket.clear()
        while bazel_core._query_semaphore._value < bazel_core._QUERY_SEMAPHORE_SIZE:
            bazel_core._query_semaphore.release()

    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# validate_expr
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "deps(//absl/strings)",
        'kind("cc_library", //absl/...)',
        "somepath(//absl/base, //absl/time)",
    ],
)
def test_validate_expr_accepts_well_formed_queries(expr):
    assert bazel_core.validate_expr(expr) is None


@pytest.mark.parametrize(
    "expr",
    [
        "--output=starlark",
        "foo --flag",
        "x" * 600,
        "deps(//absl/strings)\ndeps(//absl/time)",
        "",
        "   ",
    ],
)
def test_validate_expr_rejects_bad_queries(expr):
    assert bazel_core.validate_expr(expr) is not None


def test_validate_expr_rejects_disallowed_characters():
    assert bazel_core.validate_expr("deps(//absl/strings); rm -rf /") is not None


def test_validate_expr_rejects_flag_smuggling_mid_expression():
    # A whitespace-delimited token starting with "-" anywhere must be rejected,
    # not just when the expression starts with one.
    assert bazel_core.validate_expr("deps(//absl/strings) -k") is not None


def test_validate_expr_accepts_exactly_max_length():
    expr = "deps(//absl/" + ("a" * (512 - len("deps(//absl/") - 1)) + ")"
    assert len(expr) == 512
    assert bazel_core.validate_expr(expr) is None


def test_validate_expr_rejects_over_max_length():
    expr = "deps(//absl/" + ("a" * (513 - len("deps(//absl/") - 1)) + ")"
    assert len(expr) == 513
    assert bazel_core.validate_expr(expr) is not None


# ---------------------------------------------------------------------------
# rate limit (token bucket: 1 query per 3s per session)
# ---------------------------------------------------------------------------


def test_rate_limit_allows_first_query_then_blocks_within_window(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(bazel_core, "monotonic", lambda: clock["t"])

    assert bazel_core.check_and_record_query("session-a") is True
    assert bazel_core.check_and_record_query("session-a") is False


def test_rate_limit_allows_again_after_window_elapses(monkeypatch):
    clock = {"t": 2000.0}
    monkeypatch.setattr(bazel_core, "monotonic", lambda: clock["t"])

    assert bazel_core.check_and_record_query("session-b") is True
    clock["t"] += bazel_core._RATE_LIMIT_WINDOW_S + 0.01
    assert bazel_core.check_and_record_query("session-b") is True


def test_rate_limit_is_per_session(monkeypatch):
    clock = {"t": 3000.0}
    monkeypatch.setattr(bazel_core, "monotonic", lambda: clock["t"])

    assert bazel_core.check_and_record_query("session-a") is True
    assert bazel_core.check_and_record_query("session-b") is True


def test_rate_limit_prunes_stale_entries(monkeypatch):
    clock = {"t": 4000.0}
    monkeypatch.setattr(bazel_core, "monotonic", lambda: clock["t"])

    assert bazel_core.check_and_record_query("stale-session") is True
    clock["t"] += bazel_core._RATE_LIMIT_PRUNE_AGE_S + 1
    assert bazel_core.check_and_record_query("other-session") is True
    assert "stale-session" not in bazel_core._rate_bucket


# ---------------------------------------------------------------------------
# semaphore
# ---------------------------------------------------------------------------


def test_semaphore_size_matches_workload_cap():
    assert bazel_core._QUERY_SEMAPHORE_SIZE == 2


def test_try_acquire_and_release_query_slot():
    acquired = [bazel_core.try_acquire_query_slot() for _ in range(2)]
    assert all(acquired)
    assert bazel_core.try_acquire_query_slot() is False

    bazel_core.release_query_slot()
    assert bazel_core.try_acquire_query_slot() is True


# ---------------------------------------------------------------------------
# run_query: submit call shape + response mapping
# ---------------------------------------------------------------------------


def _guest_response(status_code: int, payload) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=json.dumps(payload).encode(),
        request=httpx.Request(
            "POST", "http://embervm.test/v1/workloads/bazel-query/tasks"
        ),
    )


@pytest.mark.asyncio
async def test_run_query_submits_expected_shape(monkeypatch):
    captured = {}

    async def fake_submit(name, *, body, guest_path, read_timeout, **kwargs):
        captured["name"] = name
        captured["body"] = body
        captured["guest_path"] = guest_path
        captured["read_timeout"] = read_timeout
        return _guest_response(
            200,
            {
                "labels": "//absl/strings:strings",
                "truncated": False,
                "analyzed_line": "Analyzed 13 targets (0 packages loaded, 0 targets configured).",
                "wall_ms": 240,
            },
        )

    monkeypatch.setattr(bazel_core.embervm_client, "submit", fake_submit)

    status, payload = await bazel_core.run_query("deps(//absl/strings)")

    assert status == 200
    assert captured["name"] == "bazel-query"
    assert json.loads(captured["body"]) == {"expression": "deps(//absl/strings)"}
    assert captured["guest_path"] == "/query"
    assert captured["read_timeout"] == 25.0
    assert payload["labels"] == "//absl/strings:strings"
    assert payload["wall_ms"] == 240


@pytest.mark.asyncio
async def test_run_query_forwards_guest_422_with_error_text(monkeypatch):
    async def fake_submit(name, *, body, guest_path, read_timeout, **kwargs):
        return httpx.Response(
            422,
            content=b"ERROR: no such package '@absl//nope'",
            request=httpx.Request("POST", "http://embervm.test"),
        )

    monkeypatch.setattr(bazel_core.embervm_client, "submit", fake_submit)

    status, payload = await bazel_core.run_query("deps(//nope)")

    assert status == 422
    assert "no such package" in payload["error"]


@pytest.mark.asyncio
async def test_run_query_maps_timeout_to_504(monkeypatch):
    async def fake_submit(name, *, body, guest_path, read_timeout, **kwargs):
        raise EmberVMTimeout("read timed out")

    monkeypatch.setattr(bazel_core.embervm_client, "submit", fake_submit)

    status, payload = await bazel_core.run_query("deps(//absl/strings)")

    assert status == 504
    assert "timed out" in payload["error"]


@pytest.mark.asyncio
async def test_run_query_maps_transport_error_to_502(monkeypatch):
    async def fake_submit(name, *, body, guest_path, read_timeout, **kwargs):
        raise EmberVMTransportError("connection refused")

    monkeypatch.setattr(bazel_core.embervm_client, "submit", fake_submit)

    status, payload = await bazel_core.run_query("deps(//absl/strings)")

    assert status == 502
    assert "connection refused" in payload["error"]


@pytest.mark.asyncio
async def test_run_query_warns_on_drift_when_packages_loaded_nonzero(
    monkeypatch, caplog
):
    async def fake_submit(name, *, body, guest_path, read_timeout, **kwargs):
        return _guest_response(
            200,
            {
                "labels": "//absl/strings:strings",
                "truncated": False,
                "analyzed_line": "Analyzed 13 targets (4 packages loaded, 4 targets configured).",
                "wall_ms": 9000,
            },
        )

    monkeypatch.setattr(bazel_core.embervm_client, "submit", fake_submit)

    with caplog.at_level(logging.WARNING):
        status, payload = await bazel_core.run_query("deps(//absl/strings)")

    assert status == 200
    assert any("0 packages loaded" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_run_query_no_warning_when_packages_loaded_is_zero(monkeypatch, caplog):
    async def fake_submit(name, *, body, guest_path, read_timeout, **kwargs):
        return _guest_response(
            200,
            {
                "labels": "//absl/strings:strings",
                "truncated": False,
                "analyzed_line": "Analyzed 13 targets (0 packages loaded, 0 targets configured).",
                "wall_ms": 240,
            },
        )

    monkeypatch.setattr(bazel_core.embervm_client, "submit", fake_submit)

    with caplog.at_level(logging.WARNING):
        status, payload = await bazel_core.run_query("deps(//absl/strings)")

    assert status == 200
    assert not any("0 packages loaded" in rec.message for rec in caplog.records)
