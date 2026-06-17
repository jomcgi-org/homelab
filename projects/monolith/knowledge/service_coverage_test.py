"""Coverage tests for service.py exception-propagation paths."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from knowledge import service


@pytest.fixture(autouse=True)
def _vault_sync_ready_by_default():
    """Most handler tests assume the vault sync is complete."""
    with patch("knowledge.service._vault_sync_ready", return_value=True):
        yield


class TestReconcileHandlerExceptionPropagation:
    @pytest.mark.asyncio
    async def test_propagates_exception_from_reconciler_run(
        self, monkeypatch, tmp_path
    ):
        """Unhandled exceptions from Reconciler.run() propagate out of
        reconcile_handler() so the scheduler can handle the failure."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))

        mock_reconciler = AsyncMock()
        mock_reconciler.run.side_effect = RuntimeError("database gone")

        with (
            patch("knowledge.service.Reconciler") as MockReconciler,
            patch("knowledge.service.KnowledgeStore"),
            patch("knowledge.service.EmbeddingClient"),
        ):
            MockReconciler.return_value = mock_reconciler
            with pytest.raises(RuntimeError, match="database gone"):
                await service.reconcile_handler(MagicMock())

    @pytest.mark.asyncio
    async def test_propagates_os_error_from_reconciler_run(self, monkeypatch, tmp_path):
        """OSError from Reconciler.run() (e.g. vault is unmounted mid-run)
        propagates out of reconcile_handler()."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))

        mock_reconciler = AsyncMock()
        mock_reconciler.run.side_effect = OSError("vault read-only")

        with (
            patch("knowledge.service.Reconciler") as MockReconciler,
            patch("knowledge.service.KnowledgeStore"),
            patch("knowledge.service.EmbeddingClient"),
        ):
            MockReconciler.return_value = mock_reconciler
            with pytest.raises(OSError, match="vault read-only"):
                await service.reconcile_handler(MagicMock())

    @pytest.mark.asyncio
    async def test_propagates_value_error_from_reconciler_run(
        self, monkeypatch, tmp_path
    ):
        """ValueError from Reconciler.run() propagates correctly."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))

        mock_reconciler = AsyncMock()
        mock_reconciler.run.side_effect = ValueError("schema mismatch")

        with (
            patch("knowledge.service.Reconciler") as MockReconciler,
            patch("knowledge.service.KnowledgeStore"),
            patch("knowledge.service.EmbeddingClient"),
        ):
            MockReconciler.return_value = mock_reconciler
            with pytest.raises(ValueError, match="schema mismatch"):
                await service.reconcile_handler(MagicMock())
