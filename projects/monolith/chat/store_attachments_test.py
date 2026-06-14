"""Additional tests for get_attachments() edge cases and save_message() with attachments."""

from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat.models import Attachment, Blob, Message
from chat.store import MessageStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session (schema-stripped for SQLite compat)."""
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
# TestGetAttachmentsEdgeCases
# ---------------------------------------------------------------------------


class TestGetAttachmentsEdgeCases:
    def test_empty_message_ids_returns_empty_dict(self, store):
        """get_attachments([]) returns an empty dict without querying the DB."""
        result = store.get_attachments([])
        assert result == {}

    @pytest.mark.asyncio
    async def test_multiple_messages_each_with_own_attachments(self, store, session):
        """get_attachments returns correct lists keyed by message_id for multiple messages."""
        msg1 = await store.save_message(
            discord_message_id="m1",
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            content="Photo 1",
            is_bot=False,
            attachments=[
                {
                    "data": b"\x89PNG",
                    "content_type": "image/png",
                    "filename": "a.png",
                    "description": "A cat",
                }
            ],
        )
        msg2 = await store.save_message(
            discord_message_id="m2",
            channel_id="ch1",
            user_id="u2",
            username="Bob",
            content="Photo 2",
            is_bot=False,
            attachments=[
                {
                    "data": b"\xff\xd8\xff",
                    "content_type": "image/jpeg",
                    "filename": "b.jpg",
                    "description": "A dog",
                },
                {
                    "data": b"\x47\x49\x46",
                    "content_type": "image/gif",
                    "filename": "c.gif",
                    "description": "A bird",
                },
            ],
        )

        result = store.get_attachments([msg1.id, msg2.id])

        assert msg1.id in result
        assert len(result[msg1.id]) == 1
        att, blob = result[msg1.id][0]
        assert blob.description == "A cat"

        assert msg2.id in result
        assert len(result[msg2.id]) == 2
        descriptions = {blob.description for _att, blob in result[msg2.id]}
        assert descriptions == {"A dog", "A bird"}

    @pytest.mark.asyncio
    async def test_message_with_no_attachments_not_in_result(self, store, session):
        """get_attachments excludes message IDs that have no attachments."""
        msg_no_att = await store.save_message(
            discord_message_id="plain",
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            content="Just text",
            is_bot=False,
        )
        msg_with_att = await store.save_message(
            discord_message_id="withatt",
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            content="With image",
            is_bot=False,
            attachments=[
                {
                    "data": b"\x89PNG",
                    "content_type": "image/png",
                    "filename": "x.png",
                    "description": "An image",
                }
            ],
        )

        result = store.get_attachments([msg_no_att.id, msg_with_att.id])

        assert msg_no_att.id not in result
        assert msg_with_att.id in result

    @pytest.mark.asyncio
    async def test_nonexistent_message_ids_return_empty(self, store):
        """get_attachments for IDs not in the DB returns an empty dict."""
        result = store.get_attachments([99999, 88888])
        assert result == {}


# ---------------------------------------------------------------------------
# TestSaveMessageWithAttachmentsAdditional
# ---------------------------------------------------------------------------


class TestSaveMessageWithAttachmentsAdditional:
    @pytest.mark.asyncio
    async def test_saves_multiple_attachments_for_single_message(self, store, session):
        """save_message() persists multiple attachments all linked to the same message."""
        attachments = [
            {
                "data": b"\x89PNG",
                "content_type": "image/png",
                "filename": "first.png",
                "description": "First image",
            },
            {
                "data": b"\xff\xd8\xff",
                "content_type": "image/jpeg",
                "filename": "second.jpg",
                "description": "Second image",
            },
            {
                "data": b"\x47\x49\x46",
                "content_type": "image/gif",
                "filename": "third.gif",
                "description": "Third image",
            },
        ]
        msg = await store.save_message(
            discord_message_id="multi",
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            content="Three images!",
            is_bot=False,
            attachments=attachments,
        )

        assert msg is not None
        saved = session.exec(
            select(Attachment).where(Attachment.message_id == msg.id)
        ).all()
        assert len(saved) == 3
        filenames = {a.filename for a in saved}
        assert filenames == {"first.png", "second.jpg", "third.gif"}

    @pytest.mark.asyncio
    async def test_empty_attachments_list_saves_no_attachment_rows(
        self, store, session
    ):
        """save_message() with attachments=[] stores the message but no Attachment rows."""
        msg = await store.save_message(
            discord_message_id="noatt",
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            content="Text only",
            is_bot=False,
            attachments=[],
        )

        assert msg is not None
        saved = session.exec(select(Attachment)).all()
        assert len(saved) == 0

    @pytest.mark.asyncio
    async def test_blob_data_is_preserved_exactly(self, store, session):
        """save_message() stores attachment bytes in the Blob without modification."""
        raw_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        msg = await store.save_message(
            discord_message_id="bytes_check",
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            content="Image",
            is_bot=False,
            attachments=[
                {
                    "data": raw_bytes,
                    "content_type": "image/png",
                    "filename": "test.png",
                    "description": "A test",
                }
            ],
        )

        saved_att = session.exec(
            select(Attachment).where(Attachment.message_id == msg.id)
        ).first()
        assert saved_att is not None
        saved_blob = session.get(Blob, saved_att.blob_sha256)
        assert saved_blob is not None
        # Bytes now go to SeaweedFS (S3 unconfigured in tests, so not uploaded);
        # the row persists with a NULL data column.
        assert saved_blob.data is None


# ---------------------------------------------------------------------------
# TestAttachmentModel -- basic instantiation
# ---------------------------------------------------------------------------


class TestAttachmentModelInstantiation:
    def test_attachment_instantiation_with_required_fields(self):
        """Attachment can be instantiated with all required fields set correctly."""
        att = Attachment(
            message_id=42,
            blob_sha256="abc123",
            filename="screenshot.png",
        )
        assert att.message_id == 42
        assert att.blob_sha256 == "abc123"
        assert att.filename == "screenshot.png"
        assert att.id is None  # Not persisted yet; primary key is None

    def test_attachment_id_defaults_to_none(self):
        """Attachment.id is None before DB persistence."""
        att = Attachment(
            message_id=1,
            blob_sha256="def456",
            filename="anim.gif",
        )
        assert att.id is None


# ---------------------------------------------------------------------------
# TestBlobDeduplication -- same image data in two messages
# ---------------------------------------------------------------------------


class TestBlobDeduplication:
    @pytest.mark.asyncio
    async def test_same_image_data_deduplicates_blob(self, store, session):
        """Saving the same image bytes in two messages creates one Blob but two Attachments."""
        shared_data = b"\x89PNG\x00SHARED_IMAGE_DATA"
        attachment_dict = {
            "data": shared_data,
            "content_type": "image/png",
            "filename": "photo.png",
            "description": "A shared photo",
        }

        msg1 = await store.save_message(
            discord_message_id="dedup1",
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            content="First post",
            is_bot=False,
            attachments=[attachment_dict],
        )
        msg2 = await store.save_message(
            discord_message_id="dedup2",
            channel_id="ch1",
            user_id="u2",
            username="Bob",
            content="Repost",
            is_bot=False,
            attachments=[{**attachment_dict, "filename": "repost.png"}],
        )

        assert msg1 is not None
        assert msg2 is not None

        # Two attachment rows, one per message
        all_atts = session.exec(select(Attachment)).all()
        att_for_msg1 = [a for a in all_atts if a.message_id == msg1.id]
        att_for_msg2 = [a for a in all_atts if a.message_id == msg2.id]
        assert len(att_for_msg1) == 1
        assert len(att_for_msg2) == 1

        # Both attachments point to the same blob
        assert att_for_msg1[0].blob_sha256 == att_for_msg2[0].blob_sha256

        # Only one blob row exists
        all_blobs = session.exec(select(Blob)).all()
        assert len(all_blobs) == 1
        assert all_blobs[0].data is None  # bytes now in SeaweedFS
