"""Tests for chat.acl: the generic Discord feature-grant ACL (ADR 029).

DB-backed tests run against in-memory SQLite with the chat schema stripped,
mirroring chat.goosecracker_test.
"""

from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat import acl
from chat.models import DiscordFeatureGrant


@pytest.fixture(name="engine")
def engine_fixture():
    """In-memory SQLite engine with the chat schema stripped for SQLite compat."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    yield engine
    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


def _seed(engine, rows):
    with Session(engine) as session:
        session.add_all(
            [
                DiscordFeatureGrant(
                    guild_id=gid, subject_id=uid, feature=feature, scope=scope
                )
                for gid, uid, feature, scope in rows
            ]
        )
        session.commit()


class TestIsGranted:
    def test_exact_scope_match(self, engine):
        _seed(engine, [("g1", "", "agent", "loom")])
        with patch("chat.acl.get_engine", return_value=engine):
            assert acl.is_granted("g1", "u1", "agent", "loom") is True
            assert acl.is_granted("g1", "u1", "agent", "homelab") is False

    def test_repo_bound_to_server(self, engine):
        # g1 -> homelab, g2 -> loom. Neither can reach the other's repo.
        _seed(engine, [("g1", "", "agent", "homelab"), ("g2", "", "agent", "loom")])
        with patch("chat.acl.get_engine", return_value=engine):
            assert acl.is_granted("g1", "u1", "agent", "homelab") is True
            assert acl.is_granted("g1", "u1", "agent", "loom") is False
            assert acl.is_granted("g2", "u2", "agent", "loom") is True
            assert acl.is_granted("g2", "u2", "agent", "homelab") is False

    def test_subject_specific_grant(self, engine):
        _seed(engine, [("g1", "owner", "artifact", "")])
        with patch("chat.acl.get_engine", return_value=engine):
            assert acl.is_granted("g1", "owner", "artifact") is True
            assert acl.is_granted("g1", "someone", "artifact") is False

    def test_whole_feature_grant_covers_any_scope(self, engine):
        _seed(engine, [("g1", "", "agent", "")])  # scope "" = whole feature
        with patch("chat.acl.get_engine", return_value=engine):
            assert acl.is_granted("g1", "u1", "agent", "loom") is True

    def test_fails_closed_unconfigured(self, engine):
        with patch("chat.acl.get_engine", return_value=engine):
            assert acl.is_granted("g1", "u1", "agent", "loom") is False

    def test_int_ids_are_normalized(self, engine):
        _seed(engine, [("111", "", "agent", "homelab")])
        with patch("chat.acl.get_engine", return_value=engine):
            assert acl.is_granted(111, 222, "agent", "homelab") is True


class TestFeatureEnabledAndScopes:
    def test_feature_enabled(self, engine):
        _seed(engine, [("g1", "", "agent", "homelab")])
        with patch("chat.acl.get_engine", return_value=engine):
            assert acl.feature_enabled("g1", "agent") is True
            assert acl.feature_enabled("g2", "agent") is False
            assert acl.feature_enabled("g1", "artifact") is False

    def test_allowed_scopes(self, engine):
        _seed(
            engine,
            [("g1", "", "agent", "homelab"), ("g1", "", "agent", "loom")],
        )
        with patch("chat.acl.get_engine", return_value=engine):
            assert acl.allowed_scopes("g1", "u1", "agent") == {"homelab", "loom"}
            assert acl.allowed_scopes("g2", "u1", "agent") == set()


class TestBootstrapDefaults:
    def test_seeds_home_owner_and_loom(self, engine, monkeypatch):
        monkeypatch.setenv("MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID", "home1")
        monkeypatch.setenv("OWNER_DISCORD_USER_ID", "owner1")
        with patch("chat.acl.get_engine", return_value=engine):
            acl.bootstrap_defaults()
            # jomcgi/homelab (public) is open to everyone in the home server
            assert acl.is_granted("home1", "anyone", "agent", "jomcgi/homelab") is True
            # loom (private) from the home server is owner-only
            assert acl.is_granted("home1", "owner1", "agent", "weave-hand/loom") is True
            assert (
                acl.is_granted("home1", "anyone", "agent", "weave-hand/loom") is False
            )
            assert acl.is_granted("home1", "owner1", "artifact") is True
            assert acl.is_granted("home1", "someone", "artifact") is False
            # loom-server members get loom, but never homelab
            assert (
                acl.is_granted(acl.LOOM_GUILD_ID, "anyone", "agent", "weave-hand/loom")
                is True
            )
            assert (
                acl.is_granted(acl.LOOM_GUILD_ID, "anyone", "agent", "jomcgi/homelab")
                is False
            )
            # loom server can build artifacts (safe: capability URLs + qwen)
            assert acl.is_granted(acl.LOOM_GUILD_ID, "anyone", "artifact") is True

    def test_idempotent(self, engine, monkeypatch):
        monkeypatch.setenv("MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID", "home1")
        monkeypatch.setenv("OWNER_DISCORD_USER_ID", "owner1")
        with patch("chat.acl.get_engine", return_value=engine):
            acl.bootstrap_defaults()
            acl.bootstrap_defaults()
            with Session(engine) as session:
                rows = session.exec(select(DiscordFeatureGrant)).all()
            # home: 2 agent + 1 artifact, loom: 1 agent + 1 artifact = 5, not doubled
            assert len(rows) == 5

    def test_no_home_env_still_seeds_loom(self, engine, monkeypatch):
        monkeypatch.delenv("MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID", raising=False)
        monkeypatch.delenv("OWNER_DISCORD_USER_ID", raising=False)
        with patch("chat.acl.get_engine", return_value=engine):
            acl.bootstrap_defaults()
            assert (
                acl.is_granted(acl.LOOM_GUILD_ID, "anyone", "agent", "weave-hand/loom")
                is True
            )
