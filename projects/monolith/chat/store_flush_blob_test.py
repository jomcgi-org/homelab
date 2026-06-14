"""Regression test: Blob row must be flushed before Attachment FK reference.

Commit af58981e added ``self.session.flush()`` after ``session.add(Blob(…))``
but *before* creating the Attachment row.  Without the flush the blob row had
not yet been written to the database when the attachment FK insert occurred,
which causes a FK IntegrityError on PostgreSQL (FK checked at statement level).

These tests assert the correct ordering at the session level so that removing
the flush() call would cause failures even on SQLite.
"""

import hashlib
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat.models import Attachment, Blob, Message
from chat.store import MessageStore


# ---------------------------------------------------------------------------
# Fixtures — mirror store_attachments_test.py conventions
# ---------------------------------------------------------------------------


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session with postgres schemas stripped for compat."""
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
    with Session(engine) as session:
        yield session
    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


@pytest.fixture
def store(session):
    embed_client = AsyncMock()
    embed_client.embed_batch.return_value = [[0.0] * 1024]
    return MessageStore(session=session, embed_client=embed_client)


# ---------------------------------------------------------------------------
# TestBlobFlushBeforeAttachment
# ---------------------------------------------------------------------------


class TestBlobFlushBeforeAttachment:
    """Regression suite for the flush-before-FK fix in store.save_message()."""

    @pytest.mark.asyncio
    async def test_blob_is_flushed_before_attachment_fk_reference(self, store, session):
        """Core regression: when a *new* Blob is created the blob row must be
        flushed to the database before the Attachment FK row is added.

        ``session.new`` holds ORM objects that have been ``add()``-ed but not
        yet flushed to the DB.  After ``session.flush()`` the object moves from
        ``session.new`` to the "persistent" state.

        If the flush() call between add(Blob) and add(Attachment) is removed,
        the Blob will still be in ``session.new`` when the Attachment is added,
        meaning the blob row does not yet exist in the database at that point —
        a FK violation on PostgreSQL.
        """
        raw_data = b"\x89PNG\r\n\x1a\nRegressionTestData"
        expected_sha = hashlib.sha256(raw_data).hexdigest()

        # Track whether the Blob was still "unflushed" (in session.new) when
        # the Attachment row was added.
        blob_still_unflushed_at_attachment_add: list[bool] = []

        original_add = session.add

        def spy_add(instance):
            if (
                isinstance(instance, Attachment)
                and instance.blob_sha256 == expected_sha
            ):
                unflushed_sha256s = {
                    obj.sha256 for obj in session.new if isinstance(obj, Blob)
                }
                blob_still_unflushed_at_attachment_add.append(
                    expected_sha in unflushed_sha256s
                )
            original_add(instance)

        session.add = spy_add

        msg = await store.save_message(
            discord_message_id="flush_regression_1",
            channel_id="ch_reg",
            user_id="u1",
            username="Regressor",
            content="Testing blob flush order",
            is_bot=False,
            attachments=[
                {
                    "data": raw_data,
                    "content_type": "image/png",
                    "filename": "regression.png",
                    "description": "Regression blob",
                }
            ],
        )

        assert msg is not None, "save_message() must not return None"
        assert blob_still_unflushed_at_attachment_add, (
            "spy_add was never triggered for the Attachment — test logic error"
        )
        assert not blob_still_unflushed_at_attachment_add[0], (
            "Blob was still in session.new (not flushed to DB) when the "
            "Attachment FK row was add()ed — regression: flush() call is "
            "missing after session.add(Blob(...))"
        )

    @pytest.mark.asyncio
    async def test_operation_order_add_flush_blob_flush_add_attachment(
        self, store, session
    ):
        """Regression: the add/flush sequence for a new blob must follow:
        add(Message) → flush() → add(Blob) → flush() → add(Attachment).

        Captures a call-log of every ``session.add`` and ``session.flush``
        invocation and asserts that a flush appears between
        ``add(Blob)`` and ``add(Attachment)``.
        """
        raw_data = b"\xff\xd8\xff\xe0OrderTestData"

        # call_log entries: ("add", classname) or ("flush",)
        call_log: list[tuple[str, ...]] = []
        original_add = session.add
        original_flush = session.flush

        def logging_add(instance):
            call_log.append(("add", type(instance).__name__))
            original_add(instance)

        def logging_flush():
            call_log.append(("flush",))
            original_flush()

        session.add = logging_add
        session.flush = logging_flush

        msg = await store.save_message(
            discord_message_id="order_test",
            channel_id="ch_reg",
            user_id="u1",
            username="Alice",
            content="Operation order test",
            is_bot=False,
            attachments=[
                {
                    "data": raw_data,
                    "content_type": "image/jpeg",
                    "filename": "order.jpg",
                    "description": "Order test",
                }
            ],
        )

        assert msg is not None

        # Find positions of key operations directly in call_log.
        # Entries are ("add", classname) or ("flush",) — no unpacking needed.
        add_blob_pos = next(
            (i for i, e in enumerate(call_log) if e == ("add", "Blob")), None
        )
        flush_after_blob_pos = next(
            (
                i
                for i, e in enumerate(call_log)
                if e == ("flush",) and i > (add_blob_pos or -1)
            ),
            None,
        )
        add_attachment_pos = next(
            (i for i, e in enumerate(call_log) if e == ("add", "Attachment")), None
        )

        assert add_blob_pos is not None, f"Blob was never added — call_log: {call_log}"
        assert add_attachment_pos is not None, "Attachment was never added"
        assert flush_after_blob_pos is not None, (
            "No flush() call found after add(Blob) — regression: blob flush is missing"
        )
        assert flush_after_blob_pos < add_attachment_pos, (
            f"flush() (pos {flush_after_blob_pos}) did not occur before "
            f"add(Attachment) (pos {add_attachment_pos}) — FK violation risk. "
            f"call_log: {call_log}"
        )

    @pytest.mark.asyncio
    async def test_save_message_with_attachment_succeeds_no_integrity_error(
        self, store, session
    ):
        """Happy path: save_message() with a brand-new blob attachment must
        complete without raising IntegrityError and persist all rows.
        """
        raw_data = b"\x89PNG\r\n\x1a\nHappyPathBlob"
        expected_sha = hashlib.sha256(raw_data).hexdigest()

        msg = await store.save_message(
            discord_message_id="happy_flush",
            channel_id="ch_reg",
            user_id="u1",
            username="Alice",
            content="Happy path with blob attachment",
            is_bot=False,
            attachments=[
                {
                    "data": raw_data,
                    "content_type": "image/png",
                    "filename": "happy.png",
                    "description": "Happy path image",
                }
            ],
        )

        assert msg is not None, "save_message() must not return None"

        blob = session.get(Blob, expected_sha)
        assert blob is not None, "Blob must exist after save_message()"

        atts = session.exec(
            select(Attachment).where(Attachment.message_id == msg.id)
        ).all()
        assert len(atts) == 1, "Exactly one Attachment row must be created"
        assert atts[0].blob_sha256 == expected_sha, (
            "Attachment.blob_sha256 must reference the correct Blob"
        )

    @pytest.mark.asyncio
    async def test_no_blob_flush_when_blob_already_exists(self, store, session):
        """When a blob is already in the DB (dedup path), the blob SHA256 is
        never in ``session.new`` at flush time — confirming the
        ``if not existing_blob:`` branch is fully skipped and no new blob row
        is created.
        """
        raw_data = b"\x47\x49\x46\x38ExistingBlobData"
        sha = hashlib.sha256(raw_data).hexdigest()

        # Pre-insert the blob so it already exists in the DB
        session.add(
            Blob(
                sha256=sha,
                content_type="image/gif",
                description="Pre-existing",
            )
        )
        session.flush()

        # Spy on flush: record whether the target blob SHA appears in
        # session.new (objects added but not yet written) at each flush call.
        blob_new_at_flush: list[bool] = []
        original_flush = session.flush

        def spying_flush():
            new_blob_shas = {obj.sha256 for obj in session.new if isinstance(obj, Blob)}
            blob_new_at_flush.append(sha in new_blob_shas)
            original_flush()

        session.flush = spying_flush

        msg = await store.save_message(
            discord_message_id="dedup_no_extra_flush",
            channel_id="ch_reg",
            user_id="u1",
            username="Alice",
            content="Dedup path — blob already in DB",
            is_bot=False,
            attachments=[
                {
                    "data": raw_data,
                    "content_type": "image/gif",
                    "filename": "dedup.gif",
                    "description": "Pre-existing blob",
                }
            ],
        )

        assert msg is not None
        # The pre-existing blob SHA must never appear in session.new at any
        # flush.  If it did, that would mean a duplicate Blob insert was
        # attempted instead of reusing the existing row.
        assert not any(blob_new_at_flush), (
            "Blob appeared in session.new during a flush on the dedup path — "
            "the existing-blob check may have failed, risking a duplicate insert"
        )

    @pytest.mark.asyncio
    async def test_attachment_fk_points_to_persisted_blob(self, store, session):
        """After save_message() the Attachment.blob_sha256 must reference a Blob
        row that is committed and retrievable in a fresh query.
        """
        raw_data = b"\x89PNG\r\n\x1a\nFKIntegrityCheck"
        expected_sha = hashlib.sha256(raw_data).hexdigest()

        msg = await store.save_message(
            discord_message_id="fk_integrity",
            channel_id="ch_reg",
            user_id="u1",
            username="Alice",
            content="FK integrity check",
            is_bot=False,
            attachments=[
                {
                    "data": raw_data,
                    "content_type": "image/png",
                    "filename": "fk.png",
                    "description": "FK test",
                }
            ],
        )

        assert msg is not None

        # Verify via get_attachments() that the join succeeds — if the Blob
        # row didn't exist at insert time the FK would be dangling and the
        # JOIN would return nothing.
        result = store.get_attachments([msg.id])
        assert msg.id in result, (
            "get_attachments() must return results for this message"
        )
        assert len(result[msg.id]) == 1, "Exactly one (Attachment, Blob) pair expected"

        att, blob = result[msg.id][0]
        assert att.blob_sha256 == expected_sha
        assert blob.sha256 == expected_sha
