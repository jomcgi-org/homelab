"""Unit tests for MessageStore.get_blob() -- happy path and cache miss."""

import hashlib

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool
from unittest.mock import AsyncMock, patch

from chat.models import Blob
from chat.store import MessageStore


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session with schema stripped for SQLite compatibility."""
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


class TestGetBlobHappyPath:
    @pytest.mark.asyncio
    async def test_get_blob_returns_stored_blob(self, store, session):
        """get_blob returns a Blob when one with that SHA256 exists."""
        image_data = b"\x89PNG\r\n\x1a\n"
        sha = hashlib.sha256(image_data).hexdigest()

        # Save a message that creates the blob via save_message
        await store.save_message(
            discord_message_id="blob-happy-1",
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            content="check image",
            is_bot=False,
            attachments=[
                {
                    "data": image_data,
                    "content_type": "image/png",
                    "filename": "test.png",
                    "description": "A test image",
                }
            ],
        )

        result = store.get_blob(sha)

        assert result is not None
        assert isinstance(result, Blob)
        assert result.sha256 == sha
        assert result.description == "A test image"
        assert result.content_type == "image/png"

    def test_get_blob_direct_insert_then_retrieve(self, store, session):
        """get_blob retrieves a blob inserted directly into the session."""
        raw = b"\xff\xd8\xff\xe0"
        sha = hashlib.sha256(raw).hexdigest()

        blob = Blob(
            sha256=sha,
            content_type="image/jpeg",
            description="A direct blob",
        )
        session.add(blob)
        session.commit()

        result = store.get_blob(sha)

        assert result is not None
        assert result.sha256 == sha
        assert result.content_type == "image/jpeg"
        assert result.description == "A direct blob"


class TestGetBlobMissPath:
    def test_get_blob_returns_none_for_unknown_sha(self, store):
        """get_blob returns None when no blob has the given SHA256."""
        unknown_sha = "a" * 64  # valid-looking but nonexistent SHA256
        result = store.get_blob(unknown_sha)
        assert result is None

    def test_get_blob_returns_none_for_empty_db(self, store):
        """get_blob returns None when the blobs table is empty."""
        sha = hashlib.sha256(b"some data").hexdigest()
        result = store.get_blob(sha)
        assert result is None

    def test_get_blob_returns_none_for_wrong_sha(self, store, session):
        """get_blob returns None when the SHA256 does not match the stored blob."""
        raw = b"some image bytes"
        correct_sha = hashlib.sha256(raw).hexdigest()
        wrong_sha = hashlib.sha256(b"different data").hexdigest()

        session.add(
            Blob(
                sha256=correct_sha,
                content_type="image/png",
                description="real blob",
            )
        )
        session.commit()

        result = store.get_blob(wrong_sha)
        assert result is None


class TestBlobBytesRoutedToSeaweedFS:
    @pytest.mark.asyncio
    async def test_save_uploads_bytes_to_s3_and_keeps_metadata_row(self, store):
        """Saving a new attachment uploads bytes to SeaweedFS and keeps the metadata row."""
        image_data = b"\x89PNG\r\n\x1a\nrouted"
        sha = hashlib.sha256(image_data).hexdigest()

        with patch("chat.store._blob_s3_put", return_value=True) as put:
            await store.save_message(
                discord_message_id="blob-s3-1",
                channel_id="ch1",
                user_id="u1",
                username="Alice",
                content="img",
                is_bot=False,
                attachments=[
                    {
                        "data": image_data,
                        "content_type": "image/png",
                        "filename": "x.png",
                        "description": "d",
                    }
                ],
            )

        put.assert_called_once_with(sha, image_data, "image/png")
        blob = store.get_blob(sha)
        assert blob is not None
        assert blob.sha256 == sha
