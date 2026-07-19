"""Tests for the bazel skyframe query demo core (validation, gate, error mapping).

Mirrors router_test.py's style: no real EmberVM, no real bazel; every
faas.embervm_client.submit call is stubbed.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import ember_public.bazel_core as bazel_core
from ember_public.bazel_models import BazelQuerySavings  # noqa: F401  (registers the table)
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


def test_rate_limit_bucket_keys_are_hashed_not_raw_cookies(monkeypatch):
    clock = {"t": 5000.0}
    monkeypatch.setattr(bazel_core, "monotonic", lambda: clock["t"])

    cookie = "a-real-session-cookie-value"
    bazel_core.check_and_record_query(cookie)

    assert cookie not in bazel_core._rate_bucket
    assert bazel_core._session_tag(cookie) in bazel_core._rate_bucket


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


# ---------------------------------------------------------------------------
# savings accrual: "estimated cold analysis time skipped", credited directly
# from each successful query's wall_ms, no polling and no state machine
# (unlike demo_pg_savings' banked-to-banked credit rule).
# ---------------------------------------------------------------------------


@pytest.fixture()
def _savings_db():
    """In-memory SQLite with bazel_query_savings created, mirroring
    router_test.py's _savings_db fixture."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_savings_first_credit_creates_row(_savings_db):
    total = bazel_core.record_bazel_query_savings_core(_savings_db, wall_ms=310.0)
    assert total == pytest.approx(bazel_core._COLD_ANALYSIS_S - 0.31)


def test_savings_accumulates_across_queries(_savings_db):
    bazel_core.record_bazel_query_savings_core(_savings_db, wall_ms=310.0)
    total = bazel_core.record_bazel_query_savings_core(_savings_db, wall_ms=450.0)
    expected = (bazel_core._COLD_ANALYSIS_S - 0.31) + (
        bazel_core._COLD_ANALYSIS_S - 0.45
    )
    assert total == pytest.approx(expected)


def test_savings_never_credits_negative_even_if_wall_ms_exceeds_cold_baseline(
    _savings_db,
):
    # A pathological slow query (wall_ms > cold baseline) must not subtract
    # from the counter; the credit floors at 0 for that query.
    huge_wall_ms = (bazel_core._COLD_ANALYSIS_S + 5) * 1000
    total = bazel_core.record_bazel_query_savings_core(
        _savings_db, wall_ms=huge_wall_ms
    )
    assert total == 0.0


# cached_bazel_query_savings's cache-hit/TTL-expiry behavior is covered in
# bazel_router_test.py's GET /savings tests instead of here: that module
# drives the async cache read through a single TestClient (one running event
# loop for the whole test), whereas two independent asyncio.run() calls in
# one test (as this file's other async coverage does per-call) rebinds the
# module-level asyncio.Lock() across event loops and fails; see the postgres
# demo's savings tests for the same TestClient-only pattern.
