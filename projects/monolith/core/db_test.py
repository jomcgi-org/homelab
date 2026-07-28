"""Unit tests for core.db — DATABASE_URL rewriting, engine caching, session."""

import importlib
import os
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, create_engine
from sqlmodel.pool import StaticPool

import core.db as db_module


def teardown_module(module):
    """Reload db module after all tests to restore any env-driven state."""
    importlib.reload(db_module)


# ---------------------------------------------------------------------------
# DATABASE_URL rewriting
# ---------------------------------------------------------------------------


class TestDatabaseUrlRewrite:
    def test_postgresql_scheme_rewritten_to_psycopg(self):
        """postgresql:// is rewritten to postgresql+psycopg:// for psycopg v3."""
        with patch.dict(
            os.environ,
            {"DATABASE_URL": "postgresql://user:pass@host:5432/mydb"},
            clear=False,
        ):
            importlib.reload(db_module)
        assert db_module.DATABASE_URL == "postgresql+psycopg://user:pass@host:5432/mydb"

    def test_only_first_occurrence_replaced(self):
        """Only the first postgresql:// prefix is replaced (maxreplace=1)."""
        # This tests the same replace logic used in db.py
        raw = "postgresql://user:pass@host:5432/db?opts=postgresql://extra"
        rewritten = raw.replace("postgresql://", "postgresql+psycopg://", 1)
        assert rewritten.startswith("postgresql+psycopg://")
        # The second occurrence is left as-is
        assert rewritten.count("postgresql+psycopg://") == 1
        assert "postgresql://extra" in rewritten

    def test_already_psycopg_url_not_double_rewritten(self):
        """A URL already using postgresql+psycopg:// is left unchanged."""
        original = "postgresql+psycopg://user:pass@host:5432/mydb"
        with patch.dict(os.environ, {"DATABASE_URL": original}, clear=False):
            importlib.reload(db_module)
        # replace("postgresql://", ...) does NOT match "postgresql+psycopg://"
        assert db_module.DATABASE_URL == original

    def test_non_postgresql_scheme_left_unchanged(self):
        """A non-postgresql:// URL (e.g. sqlite://) is passed through without modification.

        The rewrite only targets the 'postgresql://' prefix; other schemes such as
        sqlite://, mysql://, or mssql:// are left intact so that test fixtures and
        alternative drivers continue to work without side effects.
        """
        sqlite_url = "sqlite:///path/to/test.db"
        with patch.dict(os.environ, {"DATABASE_URL": sqlite_url}, clear=False):
            importlib.reload(db_module)
        assert db_module.DATABASE_URL == sqlite_url

    def test_default_url_uses_localhost_when_env_unset(self):
        """Without DATABASE_URL in the environment the default targets localhost:5432."""
        env_without_db = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
        with patch.dict(os.environ, env_without_db, clear=True):
            importlib.reload(db_module)
        assert "localhost" in db_module.DATABASE_URL
        assert "5432" in db_module.DATABASE_URL

    def test_module_url_never_uses_bare_postgresql_scheme(self):
        """The module-level DATABASE_URL always has the psycopg driver suffix."""
        # Regardless of which URL was active, the rewrite always ran
        assert not db_module.DATABASE_URL.startswith("postgresql://")
        assert db_module.DATABASE_URL.startswith("postgresql+psycopg://")


# ---------------------------------------------------------------------------
# get_engine() — LRU caching
# ---------------------------------------------------------------------------


class TestGetEngine:
    def setup_method(self):
        """Reload module before each test to get a clean LRU cache."""
        importlib.reload(db_module)

    def test_returns_engine_produced_by_create_engine(self):
        """get_engine() returns whatever create_engine() produces."""
        mock_engine = MagicMock()
        with patch("core.db.create_engine", return_value=mock_engine):
            db_module.get_engine.cache_clear()
            result = db_module.get_engine()
        assert result is mock_engine

    def test_same_engine_returned_on_repeated_calls(self):
        """get_engine() returns the identical engine object each call (LRU cache)."""
        mock_engine = MagicMock()
        with patch("core.db.create_engine", return_value=mock_engine):
            db_module.get_engine.cache_clear()
            engine1 = db_module.get_engine()
            engine2 = db_module.get_engine()
        assert engine1 is engine2

    def test_create_engine_called_only_once_despite_many_calls(self):
        """create_engine is invoked exactly once however many times get_engine is called."""
        mock_engine = MagicMock()
        with patch("core.db.create_engine", return_value=mock_engine) as mock_create:
            db_module.get_engine.cache_clear()
            for _ in range(5):
                db_module.get_engine()
        assert mock_create.call_count == 1

    def test_cache_can_be_cleared(self):
        """Clearing the LRU cache causes create_engine to be called again."""
        mock_engine_a = MagicMock(name="engine_a")
        mock_engine_b = MagicMock(name="engine_b")
        with patch("core.db.create_engine", side_effect=[mock_engine_a, mock_engine_b]):
            db_module.get_engine.cache_clear()
            first = db_module.get_engine()
            db_module.get_engine.cache_clear()
            second = db_module.get_engine()
        assert first is mock_engine_a
        assert second is mock_engine_b

    def test_database_url_is_passed_as_argument_to_create_engine(self):
        """get_engine() passes DATABASE_URL as the positional argument to create_engine.

        A wrong-URL regression would otherwise be undetectable: create_engine could
        be called with any string and the return-value assertion would still pass.
        """
        mock_engine = MagicMock()
        with patch("core.db.create_engine", return_value=mock_engine) as mock_create:
            db_module.get_engine.cache_clear()
            db_module.get_engine()
        mock_create.assert_called_once_with(db_module.DATABASE_URL)


# ---------------------------------------------------------------------------
# get_session() — FastAPI dependency injection
# ---------------------------------------------------------------------------


class TestGetSession:
    def _sqlite_engine(self):
        return create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

    def test_yields_a_session_instance(self):
        """get_session() yields a SQLModel Session."""
        with patch.object(db_module, "get_engine", return_value=self._sqlite_engine()):
            gen = db_module.get_session()
            session = next(gen)
            assert isinstance(session, Session)
            # Exhaust the generator so the context manager closes cleanly
            try:
                next(gen)
            except StopIteration:
                pass

    def test_generator_yields_exactly_one_value(self):
        """get_session() is a single-yield generator (one session per call)."""
        with patch.object(db_module, "get_engine", return_value=self._sqlite_engine()):
            yielded = list(db_module.get_session())
        assert len(yielded) == 1

    def test_yielded_value_is_usable_session(self):
        """The yielded Session is bound to the engine and can execute queries."""
        engine = self._sqlite_engine()
        with patch.object(db_module, "get_engine", return_value=engine):
            gen = db_module.get_session()
            session = next(gen)
            # A bound session can execute a trivial query without error
            result = session.exec(__import__("sqlmodel").text("SELECT 1")).fetchone()
            assert result is not None
            try:
                next(gen)
            except StopIteration:
                pass

    def test_session_closes_after_exception_in_caller(self):
        """get_session() closes the session even when the caller raises an exception.

        FastAPI dependency injection calls generator.throw() with the request
        exception; the session must still be closed (and the transaction rolled
        back) to avoid connection leaks.
        """
        engine = self._sqlite_engine()
        with patch.object(db_module, "get_engine", return_value=engine):
            gen = db_module.get_session()
            session = next(gen)
            assert isinstance(session, Session)
            # Wrap close() to track whether it is called during cleanup.
            # SQLAlchemy 2.x Session.is_active does NOT become False after close()
            # (it resets to True for potential re-use), so we track the call directly.
            with patch.object(session, "close", wraps=session.close) as mock_close:
                # Simulate an exception raised inside the caller (e.g. inside a route)
                with pytest.raises(RuntimeError, match="caller error"):
                    gen.throw(RuntimeError("caller error"))
                # The with-Session context manager must have called close() on exit
                mock_close.assert_called_once()
