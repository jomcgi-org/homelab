"""Tests for reconcile_external_in_review_stubs in knowledge/service.py.

Covers the three code paths described in the docstring:
  1. vault sync not ready → returns 0
  2. no stale stubs (no matching DB rows) → returns 0
  3. stale stubs found → rewrites frontmatter status to in_review, returns count

Also covers edge cases: idempotent rewrites, missing stub files, soft-deleted
gaps, wrong gap_class, wrong state, and per-gap exception handling.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from knowledge.gap_stubs import RESEARCHING_DIR
from knowledge.gaps import GAPS_PIPELINE_VERSION
from knowledge.models import Gap
from knowledge.service import reconcile_external_in_review_stubs


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _vault_sync_ready_by_default():
    """Default: vault sync is complete so tests exercise the real logic."""
    with patch("knowledge.service._vault_sync_ready", return_value=True):
        yield


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session with Postgres schema annotations stripped.

    Mirrors the fixture pattern used in gap_review_endpoints_test.py and
    service_test.py so the full DB flow works against a real (SQLite) engine.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas: dict[str, str] = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as s:
            yield s
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gap(
    session: Session,
    *,
    term: str,
    gap_class: str = "external",
    state: str = "in_review",
    deleted_at: datetime | None = None,
) -> Gap:
    """Insert a Gap row and return it (refreshed with its auto-assigned id)."""
    gap = Gap(
        term=term,
        context="",
        gap_class=gap_class,
        state=state,
        pipeline_version=GAPS_PIPELINE_VERSION,
        deleted_at=deleted_at,
    )
    session.add(gap)
    session.commit()
    session.refresh(gap)
    return gap


def _write_stub(vault_root: Path, slug: str, status: str = "classified") -> Path:
    """Write a minimal gap stub with the given frontmatter status field."""
    stub_dir = vault_root / RESEARCHING_DIR
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / f"{slug}.md"
    stub.write_text(
        "---\n"
        f"id: {slug}\n"
        f"title: {slug}\n"
        "type: gap\n"
        f"status: {status}\n"
        "gap_class: external\n"
        "referenced_by:\n  - src\n"
        'discovered_at: "2026-05-01T08:00:00Z"\n'
        "---\n\n"
        "Body text.\n"
    )
    return stub


# ---------------------------------------------------------------------------
# Path 1: vault sync not ready
# ---------------------------------------------------------------------------


class TestVaultSyncNotReady:
    def test_returns_zero_when_vault_not_ready(self, session, caplog):
        """When vault sync sentinel is absent the function returns 0 immediately."""
        with patch("knowledge.service._vault_sync_ready", return_value=False):
            with caplog.at_level(logging.INFO, logger="knowledge.service"):
                result = reconcile_external_in_review_stubs(session)

        assert result == 0

    def test_logs_info_when_vault_not_ready(self, session, caplog):
        """A deferral info log is emitted when vault sync is not ready."""
        with patch("knowledge.service._vault_sync_ready", return_value=False):
            with caplog.at_level(logging.INFO, logger="knowledge.service"):
                reconcile_external_in_review_stubs(session)

        assert any(
            "vault sync not ready" in r.message for r in caplog.records
        ), "Expected 'vault sync not ready' in logs"


# ---------------------------------------------------------------------------
# Path 2: no stale stubs
# ---------------------------------------------------------------------------


class TestNoStaleStubs:
    def test_returns_zero_when_no_external_in_review_gaps(
        self, session, tmp_path, monkeypatch
    ):
        """Empty DB → no gaps to reconcile → returns 0."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))

        result = reconcile_external_in_review_stubs(session)

        assert result == 0

    def test_returns_zero_when_stub_already_in_review(
        self, session, tmp_path, monkeypatch
    ):
        """Stub already has status: in_review → _set_stub_status is idempotent,
        mtime is unchanged, and the stub is not counted as rewritten."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
        _make_gap(session, term="Linkerd mTLS")
        # Slug derived from term: "linkerd-mtls"
        _write_stub(tmp_path, "linkerd-mtls", status="in_review")

        result = reconcile_external_in_review_stubs(session)

        assert result == 0
        # Stub still exists and status is in_review
        stub = tmp_path / RESEARCHING_DIR / "linkerd-mtls.md"
        assert "status: in_review" in stub.read_text()

    def test_ignores_soft_deleted_gaps(self, session, tmp_path, monkeypatch):
        """Gaps with deleted_at set are excluded from the query → returns 0."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
        _make_gap(
            session,
            term="Soft Deleted Gap",
            deleted_at=datetime.now(timezone.utc),
        )
        _write_stub(tmp_path, "soft-deleted-gap")

        result = reconcile_external_in_review_stubs(session)

        assert result == 0

    def test_ignores_non_external_gap_class(self, session, tmp_path, monkeypatch):
        """Only gap_class='external' rows are reconciled; 'internal' is skipped."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
        _make_gap(session, term="Internal Gap", gap_class="internal")
        _write_stub(tmp_path, "internal-gap")

        result = reconcile_external_in_review_stubs(session)

        assert result == 0

    def test_ignores_non_in_review_state(self, session, tmp_path, monkeypatch):
        """Only state='in_review' rows are reconciled; 'classified' is skipped."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
        _make_gap(session, term="Classified Gap", state="classified")
        _write_stub(tmp_path, "classified-gap")

        result = reconcile_external_in_review_stubs(session)

        assert result == 0

    def test_returns_zero_when_stub_file_missing(self, session, tmp_path, monkeypatch):
        """A gap with no corresponding stub on disk is silently skipped."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
        _make_gap(session, term="No Stub")
        # Intentionally do NOT write the stub file.
        (tmp_path / RESEARCHING_DIR).mkdir(parents=True, exist_ok=True)

        result = reconcile_external_in_review_stubs(session)

        assert result == 0


# ---------------------------------------------------------------------------
# Path 3: stale stubs found → rewrite
# ---------------------------------------------------------------------------


class TestStaleStubsRewritten:
    def test_rewrites_single_stub_and_returns_one(
        self, session, tmp_path, monkeypatch
    ):
        """One stale stub (status: classified) is rewritten to in_review."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
        _make_gap(session, term="Linkerd mTLS")
        stub = _write_stub(tmp_path, "linkerd-mtls", status="classified")

        result = reconcile_external_in_review_stubs(session)

        assert result == 1
        assert "status: in_review" in stub.read_text()

    def test_rewrites_multiple_stubs_and_returns_count(
        self, session, tmp_path, monkeypatch
    ):
        """Multiple stale stubs are each rewritten; count equals stubs changed."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
        terms = ["Service Mesh", "eBPF Tracing", "Zero Trust Auth"]
        slugs = ["service-mesh", "ebpf-tracing", "zero-trust-auth"]
        for term, slug in zip(terms, slugs):
            _make_gap(session, term=term)
            _write_stub(tmp_path, slug, status="classified")

        result = reconcile_external_in_review_stubs(session)

        assert result == 3
        for slug in slugs:
            stub = tmp_path / RESEARCHING_DIR / f"{slug}.md"
            assert "status: in_review" in stub.read_text(), (
                f"{slug}.md should have status: in_review"
            )

    def test_logs_rewrite_count(self, session, tmp_path, monkeypatch, caplog):
        """Info log reports number of stubs rewritten when count > 0."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
        _make_gap(session, term="Linkerd mTLS")
        _write_stub(tmp_path, "linkerd-mtls", status="classified")

        with caplog.at_level(logging.INFO, logger="knowledge.service"):
            reconcile_external_in_review_stubs(session)

        assert any(
            "rewrote" in r.message and "1" in r.message for r in caplog.records
        ), "Expected a 'rewrote N stubs' log line"

    def test_mixed_stubs_only_stale_counted(self, session, tmp_path, monkeypatch):
        """Only stubs with non-matching status are counted; already-in_review ones are not."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
        _make_gap(session, term="Stale Gap")
        _write_stub(tmp_path, "stale-gap", status="classified")

        _make_gap(session, term="Clean Gap")
        _write_stub(tmp_path, "clean-gap", status="in_review")

        result = reconcile_external_in_review_stubs(session)

        assert result == 1

    def test_uses_vault_root_env_var(self, session, tmp_path, monkeypatch):
        """The VAULT_ROOT env var controls which directory is scanned."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
        _make_gap(session, term="Linkerd mTLS")
        _write_stub(tmp_path, "linkerd-mtls", status="classified")

        result = reconcile_external_in_review_stubs(session)

        assert result == 1


# ---------------------------------------------------------------------------
# Exception handling: per-gap errors are caught and logged
# ---------------------------------------------------------------------------


class TestExceptionHandling:
    def test_skips_gap_on_exception_and_continues(
        self, session, tmp_path, monkeypatch, caplog
    ):
        """An exception for one gap is caught; processing continues for others.

        The function catches all exceptions per gap, logs a WARNING, and
        advances to the next gap rather than aborting the whole run.
        """
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
        _make_gap(session, term="Failing Gap")
        _write_stub(tmp_path, "failing-gap", status="classified")

        _make_gap(session, term="Succeeding Gap")
        _write_stub(tmp_path, "succeeding-gap", status="classified")

        real_set_stub_status = None

        def _set_stub_status_sometimes_raises(
            vault_root: Path, gap: Gap, status: str
        ) -> None:
            if gap.term == "Failing Gap":
                raise RuntimeError("simulated write error")
            # Call through to the real implementation for the succeeding gap.
            # We captured the real function before patching to avoid recursion.
            real_set_stub_status(vault_root, gap, status)

        import knowledge.gaps as _gaps_mod

        real_set_stub_status = _gaps_mod._set_stub_status

        with (
            patch(
                "knowledge.gaps._set_stub_status",
                side_effect=_set_stub_status_sometimes_raises,
            ),
            caplog.at_level(logging.WARNING, logger="knowledge.service"),
        ):
            result = reconcile_external_in_review_stubs(session)

        # One gap raised — it's skipped; one succeeded — it's counted.
        assert result == 1
        # The failure must be logged at WARNING level.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "reconcile_external_in_review_stubs" in r.message for r in warnings
        ), "Expected a warning about the failed gap"

    def test_returns_zero_on_all_gaps_failing(
        self, session, tmp_path, monkeypatch, caplog
    ):
        """If every gap raises, the function returns 0 and logs a warning per gap."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
        _make_gap(session, term="Gap One")
        _make_gap(session, term="Gap Two")

        with (
            patch(
                "knowledge.gaps._set_stub_status",
                side_effect=OSError("disk full"),
            ),
            caplog.at_level(logging.WARNING, logger="knowledge.service"),
        ):
            result = reconcile_external_in_review_stubs(session)

        assert result == 0
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2, "Expected one warning per failing gap"
