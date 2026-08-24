"""Tests for chat.acl: the generic Discord feature-grant ACL (ADR 029).

DB-backed tests run against in-memory SQLite with the chat schema stripped.
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
    acl._clear_grants_cache()
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


class TestAmbientChannels:
    def test_returns_channel_scopes(self, engine):
        _seed(
            engine,
            [("g1", "", "ambient", "c1"), ("g1", "", "ambient", "c2")],
        )
        with patch("chat.acl.get_engine", return_value=engine):
            assert acl.ambient_channels("g1") == {"c1", "c2"}

    def test_ignores_other_features_and_guilds(self, engine):
        _seed(
            engine,
            [
                ("g1", "", "ambient", "c1"),
                ("g1", "", "agent", "homelab"),
                ("g2", "", "ambient", "c9"),
            ],
        )
        with patch("chat.acl.get_engine", return_value=engine):
            assert acl.ambient_channels("g1") == {"c1"}

    def test_ignores_subject_specific_grant(self, engine):
        # ambient is server-wide (subject_id ""); a per-user grant should not
        # count, even though it happens to share the feature name.
        _seed(engine, [("g1", "u1", "ambient", "c1")])
        with patch("chat.acl.get_engine", return_value=engine):
            assert acl.ambient_channels("g1") == set()

    def test_empty_when_no_grants(self, engine):
        with patch("chat.acl.get_engine", return_value=engine):
            assert acl.ambient_channels("g1") == set()


class TestBootstrapDefaults:
    def test_seeds_home_owner_and_loom(self, engine, monkeypatch):
        monkeypatch.setenv("MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID", "home1")
        monkeypatch.setenv("OWNER_DISCORD_USER_ID", "owner1")
        with patch("chat.acl.get_engine", return_value=engine):
            acl.bootstrap_defaults()
            # The public homelab repo is open to everyone in the home server.
            assert (
                acl.is_granted("home1", "anyone", "agent", "jomcgi-org/homelab") is True
            )
            # loom (private) from the home server is owner-only
            assert acl.is_granted("home1", "owner1", "agent", "weave-hand/loom") is True
            assert (
                acl.is_granted("home1", "anyone", "agent", "weave-hand/loom") is False
            )
            # loom-server members get loom, but never homelab
            assert (
                acl.is_granted(acl.LOOM_GUILD_ID, "anyone", "agent", "weave-hand/loom")
                is True
            )
            assert (
                acl.is_granted(
                    acl.LOOM_GUILD_ID, "anyone", "agent", "jomcgi-org/homelab"
                )
                is False
            )
            # ADR 036: the orchestrator tier is granted server-wide on the home
            # server (any channel), and nowhere else by default.
            assert (
                acl.is_granted("home1", "anyone", "orchestrator", "any-channel") is True
            )
            assert (
                acl.is_granted(acl.LOOM_GUILD_ID, "anyone", "orchestrator", "c")
                is False
            )

    def test_idempotent(self, engine, monkeypatch):
        monkeypatch.setenv("MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID", "home1")
        monkeypatch.setenv("OWNER_DISCORD_USER_ID", "owner1")
        with patch("chat.acl.get_engine", return_value=engine):
            acl.bootstrap_defaults()
            acl.bootstrap_defaults()
            with Session(engine) as session:
                rows = session.exec(select(DiscordFeatureGrant)).all()
            # home: 2 agent + 1 orchestrator, loom: 1 agent = 4, not doubled
            assert len(rows) == 4

    def test_no_home_env_still_seeds_loom(self, engine, monkeypatch):
        monkeypatch.delenv("MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID", raising=False)
        monkeypatch.delenv("OWNER_DISCORD_USER_ID", raising=False)
        with patch("chat.acl.get_engine", return_value=engine):
            acl.bootstrap_defaults()
            assert (
                acl.is_granted(acl.LOOM_GUILD_ID, "anyone", "agent", "weave-hand/loom")
                is True
            )
