"""Hermetic persistence, validation, filtering, and cooldown tests for triggers."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from chat.agent import ChatDeps, create_agent
from chat.models import MessageTrigger
from chat.triggers import (
    TriggerValidationError,
    claim_matching,
    create_trigger,
    render_template,
    validate_pattern,
)


@pytest.fixture(name="engine")
def engine_fixture(tmp_path):
    """File-backed SQLite allows independent connections for contention."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'triggers.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    table = MessageTrigger.__table__
    saved_schema = table.schema
    table.schema = None
    SQLModel.metadata.create_all(engine, tables=[table])
    yield engine
    table.schema = saved_schema


def _create(session: Session, **overrides) -> MessageTrigger:
    values = {
        "name": "deploy alert",
        "pattern": r"deploy\s+(failed|broken)",
        "channel_ids": ["100"],
        "user_ids": ["200"],
        "action_type": "respond",
        "action_config": {"content": "Looking at {content}"},
        "cooldown_secs": 60,
        "created_by_user_id": "200",
    }
    values.update(overrides)
    return create_trigger(session, **values)


def test_model_round_trip_and_database_constraints(engine):
    with Session(engine) as session:
        created = _create(session)
        session.commit()
        created_id = created.id

    with Session(engine) as session:
        row = session.get(MessageTrigger, created_id)
        assert row is not None
        assert row.channel_ids == ["100"]
        assert row.user_ids == ["200"]
        assert row.action_config == {"content": "Looking at {content}"}
        assert row.enabled is True
        assert row.last_fired_at is None
        assert isinstance(row.created_at, datetime)
        assert isinstance(row.updated_at, datetime)

        session.add(
            MessageTrigger(
                name="bad action",
                pattern="x",
                action_type="delete_everything",
                action_config={},
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize("pattern", ["[", "(a+)+$", "(a|aa)+$"])
def test_invalid_or_pathological_regex_is_rejected(pattern):
    with pytest.raises(TriggerValidationError):
        validate_pattern(pattern)


def test_invalid_action_and_cooldown_are_rejected(engine):
    with Session(engine) as session:
        with pytest.raises(TriggerValidationError, match="target_channel_id"):
            _create(
                session,
                action_type="crosspost",
                action_config={"content": "missing target"},
            )
        with pytest.raises(TriggerValidationError, match="cooldown_secs"):
            _create(session, name="negative", cooldown_secs=-1)


def test_positive_and_negative_regex_channel_user_and_inactive_matches(engine):
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        _create(session)
        _create(
            session,
            name="inactive",
            pattern="deploy",
            channel_ids=[],
            user_ids=[],
            enabled=False,
        )
        session.commit()

    with Session(engine) as session:
        assert claim_matching(session, "999", "200", "deploy failed", now=now) == []
    with Session(engine) as session:
        assert claim_matching(session, "100", "999", "deploy failed", now=now) == []
    with Session(engine) as session:
        assert claim_matching(session, "100", "200", "deploy succeeded", now=now) == []
    with Session(engine) as session:
        claims = claim_matching(session, "100", "200", "DEPLOY failed", now=now)
    assert [claim.name for claim in claims] == ["deploy alert"]


def test_cooldown_suppresses_until_expiry(engine):
    started = datetime.now(timezone.utc)
    with Session(engine) as session:
        _create(session)
        session.commit()

    with Session(engine) as session:
        assert (
            len(claim_matching(session, "100", "200", "deploy failed", now=started))
            == 1
        )
    with Session(engine) as session:
        assert (
            claim_matching(
                session,
                "100",
                "200",
                "deploy failed",
                now=started + timedelta(seconds=59),
            )
            == []
        )
    with Session(engine) as session:
        assert (
            len(
                claim_matching(
                    session,
                    "100",
                    "200",
                    "deploy failed",
                    now=started + timedelta(seconds=60),
                )
            )
            == 1
        )


def test_concurrent_cooldown_claim_has_one_winner(engine):
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        _create(session)
        session.commit()

    def worker() -> int:
        with Session(engine) as session:
            return len(claim_matching(session, "100", "200", "deploy failed", now=now))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: worker(), range(2)))
    assert sorted(results) == [0, 1]


def test_small_template_vocabulary_is_expanded_and_bounded():
    rendered = render_template(
        "{author} in {channel_id}: {content}",
        content="hello",
        author="<@200>",
        channel_id="100",
    )
    assert rendered == "<@200> in 100: hello"
    assert (
        len(render_template("x" * 3000, content="", author="", channel_id="")) == 2000
    )


@pytest.mark.asyncio
async def test_manage_triggers_agent_tool_crud_and_owner_gate(engine):
    agent = create_agent(base_url="http://fake:8080")
    tool = agent._function_toolset.tools["manage_triggers"].function
    deps = ChatDeps(
        channel_id="100",
        store=MagicMock(),
        embed_client=MagicMock(),
        author_id="200",
    )
    ctx = SimpleNamespace(deps=deps)
    with (
        patch("chat.acl.is_owner", return_value=True),
        patch("chat.triggers.get_engine", return_value=engine),
    ):
        created = await tool(
            ctx,
            operation="create",
            name="hello",
            pattern="hello",
            action_type="respond",
            action_config={"message": "hi {author}"},
            cooldown_secs=5,
        )
        listed = await tool(ctx, operation="list")
        updated = await tool(
            ctx,
            operation="update",
            name="hello",
            channel_ids=[],
            user_ids=["300"],
            action_config={"content": "updated"},
        )
        disabled = await tool(ctx, operation="disable", name="hello")
        enabled = await tool(ctx, operation="enable", name="hello")
        deleted = await tool(ctx, operation="delete", name="hello")

    assert "Created trigger" in created
    assert "channels=100" in listed
    assert "Updated trigger" in updated
    assert "Disabled trigger" in disabled
    assert "Enabled trigger" in enabled
    assert "Deleted trigger" in deleted
    with Session(engine) as session:
        assert session.exec(select(MessageTrigger)).all() == []

    with patch("chat.acl.is_owner", return_value=False):
        denied = await tool(ctx, operation="list")
    assert denied == "Only the configured owner can manage message triggers."


def test_duplicate_name_is_reported_as_validation_error(engine):
    with Session(engine) as session:
        _create(session)
        session.commit()
    with (
        Session(engine) as session,
        pytest.raises(TriggerValidationError, match="already exists"),
    ):
        _create(session)
