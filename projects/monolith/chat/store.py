"""Message store -- persist and recall chat messages with pgvector."""

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from shared.embedding import EmbeddingClient
from chat.models import (
    Attachment,
    Blob,
    ChannelSummary,
    Message,
    MessageLock,
    UserChannelSummary,
)

logger = logging.getLogger(__name__)

# Allow-listed enums for MessageStore.query_stats -- the security seam. A
# metric or group_by value outside these sets is rejected before any SQL is
# built, and group_by's value is only ever used as a dict lookup into a
# hard-coded fragment, never interpolated from the caller's string. See
# projects/monolith/ARCHITECTURE.md, section 5.
_STATS_METRICS = {"count", "first", "latest"}
_STATS_GROUP_BY = {
    "author": "user_id",
    "day": "date_trunc('day', created_at)",
}


def _blob_s3_put(sha256: str, data: bytes, content_type: str) -> bool:
    """Best-effort upload of a content-addressed attachment blob to SeaweedFS.

    Stored at ``s3://<CHAT_BLOB_S3_BUCKET>/blobs/<sha256>``. Content-addressed,
    so writes are idempotent and dedup for free. Mirrors stars.grid._s3_client:
    dummy creds (SeaweedFS auth is disabled cluster-wide), path-style addressing,
    scheme prepended to the endpoint. The bucket is auto-created on first write.

    The caller treats failures as non-fatal: blob bytes are write-only today
    (no read path consumes them), so an upload miss is at worst an archival gap,
    never a chat-breaking error.
    """
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    bucket = os.environ.get("CHAT_BLOB_S3_BUCKET", "")
    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT", "")
    if not bucket or not endpoint:
        logger.warning("chat blob S3 not configured; skipping upload of %s", sha256)
        return False
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "http://" + endpoint
    # Scheme guaranteed by the guard above; inline nosemgrep clears the pre-commit
    # boto3-endpoint-url-missing-scheme hook (the Bazel main_semgrep_test, which
    # ignores nosemgrep, is covered by exclude_rules in projects/monolith/BUILD).
    client = boto3.client(  # nosemgrep: boto3-endpoint-url-missing-scheme
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID", "duckdb"),
        aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY", "duckdb"),
        config=Config(s3={"addressing_style": "path"}),
    )
    key = f"blobs/{sha256}"
    try:
        client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchBucket", "404"):
            client.create_bucket(Bucket=bucket)
            client.put_object(
                Bucket=bucket, Key=key, Body=data, ContentType=content_type
            )
        else:
            raise
    return True


@dataclass
class SaveResult:
    stored: int
    skipped: int


def _build_embed_text(content: str, descriptions: list[str]) -> str:
    """Combine message text with image descriptions for embedding."""
    if not descriptions:
        return content
    image_parts = "\n".join(f"[Image: {d}]" for d in descriptions)
    return f"{content}\n\n{image_parts}"


class MessageStore:
    def __init__(self, session: Session, embed_client: EmbeddingClient):
        self.session = session
        self.embed_client = embed_client

    async def save_messages(self, messages: list[dict]) -> SaveResult:
        """Embed and persist a batch of messages. Skips duplicates via savepoints."""
        if not messages:
            return SaveResult(stored=0, skipped=0)

        # Build embed texts for the whole batch
        embed_texts = []
        for m in messages:
            descriptions = [
                a["description"]
                for a in (m.get("attachments") or [])
                if a.get("description")
            ]
            embed_texts.append(_build_embed_text(m["content"], descriptions))

        # Single batch embedding call
        embeddings = await self.embed_client.embed_batch(embed_texts)

        stored = 0
        skipped = 0

        for m, embedding in zip(messages, embeddings, strict=True):
            nested = self.session.begin_nested()
            try:
                msg = Message(
                    discord_message_id=m["discord_message_id"],
                    channel_id=m["channel_id"],
                    user_id=m["user_id"],
                    username=m["username"],
                    content=m["content"],
                    is_bot=m["is_bot"],
                    thinking=m.get("thinking"),
                    embedding=embedding,
                )
                self.session.add(msg)
                self.session.flush()
                for a in m.get("attachments") or []:
                    if a["data"] is None:
                        continue
                    sha = hashlib.sha256(a["data"]).hexdigest()
                    existing_blob = self.session.get(Blob, sha)
                    if not existing_blob:
                        # Raw bytes go to SeaweedFS (content-addressed); the row
                        # keeps only metadata. Upload failure is non-fatal (bytes
                        # have no read path), so log and store the row regardless.
                        try:
                            _blob_s3_put(sha, a["data"], a["content_type"])
                        except Exception:
                            logger.exception("chat blob S3 upload failed for %s", sha)
                        self.session.add(
                            Blob(
                                sha256=sha,
                                content_type=a["content_type"],
                                description=a.get("description", ""),
                            )
                        )
                        self.session.flush()
                    self.session.add(
                        Attachment(
                            message_id=msg.id,
                            blob_sha256=sha,
                            filename=a["filename"],
                        )
                    )
                nested.commit()
                stored += 1
            except IntegrityError:
                nested.rollback()
                skipped += 1

        self.session.commit()
        return SaveResult(stored=stored, skipped=skipped)

    async def save_message(
        self,
        discord_message_id: str,
        channel_id: str,
        user_id: str,
        username: str,
        content: str,
        is_bot: bool,
        attachments: list[dict] | None = None,
        thinking: str | None = None,
    ) -> Message | None:
        """Embed and persist a message. Returns None if already stored."""
        msg_dict = {
            "discord_message_id": discord_message_id,
            "channel_id": channel_id,
            "user_id": user_id,
            "username": username,
            "content": content,
            "is_bot": is_bot,
            "attachments": attachments,
            "thinking": thinking,
        }
        result = await self.save_messages([msg_dict])
        if result.skipped:
            return None
        saved = self.session.exec(
            select(Message).where(Message.discord_message_id == discord_message_id)
        ).first()
        return saved

    def get_recent(self, channel_id: str, limit: int = 20) -> list[Message]:
        """Return the most recent messages in a channel, oldest first."""
        stmt = (
            select(Message)
            .where(Message.channel_id == channel_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        messages = list(self.session.exec(stmt).all())
        messages.reverse()
        return messages

    def fetch_window(
        self,
        channel_id: str,
        *,
        max_messages: int = 300,
        max_chars: int = 30_000,
    ) -> list[Message]:
        """Return a bounded chronological (oldest first) window for one channel.

        Feeds summarization tools that need honest caps: walks newest-first,
        stopping at whichever cap (message count or cumulative content chars)
        is hit first, then reverses to chronological order. The newest message
        is always kept even if its content alone exceeds max_chars, so a
        non-empty channel never yields an empty window.
        """
        stmt = (
            select(Message)
            .where(Message.channel_id == channel_id)
            .order_by(Message.created_at.desc())
            .limit(max_messages)
        )
        newest_first = list(self.session.exec(stmt).all())

        window: list[Message] = []
        total_chars = 0
        for msg in newest_first:
            total_chars += len(msg.content)
            if total_chars > max_chars and window:
                break
            window.append(msg)

        window.reverse()
        return window

    def search_similar(
        self,
        channel_id: str,
        query_embedding: list[float],
        limit: int = 5,
        exclude_ids: list[int] | None = None,
        user_id: str | None = None,
    ) -> list[Message]:
        """Semantic search over channel history using pgvector cosine distance.

        Note: This uses raw SQL because SQLModel doesn't natively support
        pgvector's <=> operator. Falls back gracefully in SQLite tests.
        """
        exclude = exclude_ids or []
        params: dict[str, object] = {
            "channel_id": channel_id,
            "embedding": str(query_embedding),
            "limit": limit,
        }

        # Raw SQL is required here because pgvector's <=> cosine distance
        # operator has no SQLModel/SQLAlchemy ORM equivalent.
        filters = "channel_id = :channel_id"
        if exclude:
            # Bind each excluded ID as a separate parameter to avoid
            # string interpolation in the SQL statement.
            placeholders = []
            for idx, eid in enumerate(exclude):
                key = f"excl_{idx}"
                placeholders.append(f":{key}")
                params[key] = int(eid)
            filters += f" AND id NOT IN ({', '.join(placeholders)})"
        if user_id:
            filters += " AND user_id = :user_id"
            params["user_id"] = user_id

        sql = text(
            f"SELECT * FROM chat.messages WHERE {filters} "
            "ORDER BY embedding <=> :embedding LIMIT :limit"
        )
        result = self.session.exec(sql, params=params)
        return [Message.model_validate(row) for row in result]

    def lexical_search(
        self,
        channel_id: str,
        query_text: str,
        limit: int = 5,
        exclude_ids: list[int] | None = None,
        user_id: str | None = None,
    ) -> list[Message]:
        """Full-text keyword search over channel history using the generated
        content_tsv column (GIN indexed). Complements search_similar: vector
        search finds semantic matches, this finds exact tokens (a username, a
        URL, an error code, a rare noun) that embeddings routinely miss.

        Raw SQL because tsvector/websearch_to_tsquery have no ORM equivalent.
        Returns [] on an empty or stop-word-only query rather than every row.
        """
        if not query_text or not query_text.strip():
            return []
        exclude = exclude_ids or []
        params: dict[str, object] = {
            "channel_id": channel_id,
            "q": query_text,
            "limit": limit,
        }
        filters = "m.channel_id = :channel_id AND m.content_tsv @@ q.q"
        if exclude:
            placeholders = []
            for idx, eid in enumerate(exclude):
                key = f"excl_{idx}"
                placeholders.append(f":{key}")
                params[key] = int(eid)
            filters += f" AND m.id NOT IN ({', '.join(placeholders)})"
        if user_id:
            filters += " AND m.user_id = :user_id"
            params["user_id"] = user_id

        # websearch_to_tsquery tolerates arbitrary free text (never raises on
        # punctuation the way to_tsquery does), so a user's raw question is a
        # safe query. Compute it once in a CTE (q.q) and reuse for both the @@
        # filter and the rank.
        sql = text(
            "WITH q AS (SELECT websearch_to_tsquery('english', :q) AS q) "
            f"SELECT m.* FROM chat.messages m, q WHERE {filters} "
            "ORDER BY ts_rank_cd(m.content_tsv, q.q) DESC, m.created_at DESC "
            "LIMIT :limit"
        )
        result = self.session.exec(sql, params=params)
        return [Message.model_validate(row) for row in result]

    def search_hybrid(
        self,
        channel_id: str,
        query_text: str,
        query_embedding: list[float],
        limit: int = 5,
        exclude_ids: list[int] | None = None,
        user_id: str | None = None,
        candidate_k: int = 20,
        rrf_k: int = 60,
    ) -> list[Message]:
        """Hybrid history search: fuse semantic (pgvector) and lexical
        (full-text) results with Reciprocal Rank Fusion.

        RRF scores each message by the sum of 1/(rrf_k + rank) across the lists
        it appears in, so a message ranked highly by either retriever floats up
        and one ranked by both wins. RRF fuses by rank order alone, so the two
        incomparable scores (cosine distance vs ts_rank) never need
        normalizing. Falls back to whichever list is non-empty when the other
        returns nothing (e.g. a stop-word-only query yields no lexical hits).
        """
        vector_hits = self.search_similar(
            channel_id=channel_id,
            query_embedding=query_embedding,
            limit=candidate_k,
            exclude_ids=exclude_ids,
            user_id=user_id,
        )
        lexical_hits = self.lexical_search(
            channel_id=channel_id,
            query_text=query_text,
            limit=candidate_k,
            exclude_ids=exclude_ids,
            user_id=user_id,
        )
        scores: dict[int, float] = {}
        by_id: dict[int, Message] = {}
        for ranked in (vector_hits, lexical_hits):
            for rank, msg in enumerate(ranked):
                if msg.id is None:
                    continue
                scores[msg.id] = scores.get(msg.id, 0.0) + 1.0 / (rrf_k + rank)
                by_id.setdefault(msg.id, msg)
        fused = sorted(
            by_id.values(),
            key=lambda m: (scores[m.id], m.created_at),
            reverse=True,
        )
        return fused[:limit]

    def query_stats(
        self,
        channel_id: str,
        *,
        metric: str,
        group_by: str | None = None,
        user_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        contains: str | None = None,
        message_id: str | None = None,
        limit: int = 25,
    ) -> list[dict]:
        """Structured, scope-locked aggregation/lookup over channel history.

        The model chooses the filter; the caller (never the model) chooses the
        scope via channel_id. metric and group_by are allow-listed enums mapped
        to fixed SQL fragments/columns -- an unknown value is rejected with a
        ValueError before any SQL is built, so there is no path for a
        model-supplied identifier to reach the statement. Every filter value is
        a bound parameter, exactly like lexical_search. See
        projects/monolith/ARCHITECTURE.md, section 5.
        """
        if metric not in _STATS_METRICS:
            raise ValueError(
                f"Unknown metric {metric!r}; must be one of {sorted(_STATS_METRICS)}"
            )
        if group_by is not None and group_by not in _STATS_GROUP_BY:
            raise ValueError(
                f"Unknown group_by {group_by!r}; must be one of "
                f"{sorted(_STATS_GROUP_BY)} or None"
            )
        if metric in ("first", "latest") and group_by is not None:
            raise ValueError(f"metric={metric!r} is only valid with group_by=None")

        params: dict[str, object] = {"channel_id": channel_id, "limit": limit}
        filters = "channel_id = :channel_id"
        if user_id:
            filters += " AND user_id = :user_id"
            params["user_id"] = user_id
        if since:
            filters += " AND created_at >= :since"
            params["since"] = since
        if until:
            filters += " AND created_at <= :until"
            params["until"] = until
        if contains:
            filters += " AND content_tsv @@ websearch_to_tsquery('english', :contains)"
            params["contains"] = contains
        if message_id:
            filters += " AND discord_message_id = :message_id"
            params["message_id"] = message_id

        if (
            self.session.bind is not None
            and self.session.bind.dialect.name == "postgresql"
        ):
            self.session.exec(text("SET LOCAL statement_timeout = '3s'"))

        if metric in ("first", "latest"):
            order = "ASC" if metric == "first" else "DESC"
            sql = text(
                f"SELECT * FROM chat.messages WHERE {filters} "
                f"ORDER BY created_at {order} LIMIT 1"
            )
            result = self.session.exec(sql, params=params)
            rows = list(result)
            if not rows:
                return []
            row = rows[0]
            return [
                {
                    "discord_message_id": row.discord_message_id,
                    "username": row.username,
                    "user_id": row.user_id,
                    "content": row.content,
                    "created_at": row.created_at,
                    "is_bot": row.is_bot,
                }
            ]

        # metric == "count"
        if group_by == "author":
            sql = text(
                f"SELECT user_id, username, count(*) AS n FROM chat.messages "
                f"WHERE {filters} GROUP BY user_id, username "
                "ORDER BY n DESC LIMIT :limit"
            )
            result = self.session.exec(sql, params=params)
            return [
                {"user_id": row.user_id, "username": row.username, "count": row.n}
                for row in result
            ]
        if group_by == "day":
            group_fragment = _STATS_GROUP_BY["day"]
            sql = text(
                f"SELECT {group_fragment} AS d, count(*) AS n FROM chat.messages "
                f"WHERE {filters} GROUP BY d ORDER BY d LIMIT :limit"
            )
            result = self.session.exec(sql, params=params)
            return [{"day": row.d, "count": row.n} for row in result]

        # group_by is None -- a single total count
        sql = text(f"SELECT count(*) AS n FROM chat.messages WHERE {filters}")
        result = self.session.exec(sql, params=params)
        rows = list(result)
        return [{"count": rows[0].n if rows else 0}]

    def get_attachments(
        self, message_ids: list[int]
    ) -> dict[int, list[tuple[Attachment, Blob]]]:
        """Load attachments with their blobs for a set of message IDs."""
        if not message_ids:
            return {}
        stmt = (
            select(Attachment, Blob)
            .join(Blob, Attachment.blob_sha256 == Blob.sha256)
            .where(Attachment.message_id.in_(message_ids))
        )
        result: dict[int, list[tuple[Attachment, Blob]]] = {}
        for att, blob in self.session.exec(stmt).all():
            result.setdefault(att.message_id, []).append((att, blob))
        return result

    def get_blob(self, sha256: str) -> Blob | None:
        """Look up a blob by its content hash."""
        return self.session.get(Blob, sha256)

    def find_user_id_by_username(self, channel_id: str, username: str) -> str | None:
        """Look up a user_id by username within a channel. Returns None if not found."""
        stmt = (
            select(Message.user_id)
            .where(Message.channel_id == channel_id, Message.username == username)
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        return self.session.exec(stmt).first()

    def list_user_summaries(self, channel_id: str) -> list[UserChannelSummary]:
        """Return all user summaries for a channel, ordered by most recently updated."""
        stmt = (
            select(UserChannelSummary)
            .where(UserChannelSummary.channel_id == channel_id)
            .order_by(UserChannelSummary.updated_at.desc())
        )
        return list(self.session.exec(stmt).all())

    def get_user_summary(
        self, channel_id: str, username: str
    ) -> UserChannelSummary | None:
        """Return the rolling summary for a user in a channel, or None."""
        stmt = select(UserChannelSummary).where(
            UserChannelSummary.channel_id == channel_id,
            UserChannelSummary.username == username,
        )
        return self.session.exec(stmt).first()

    def get_user_summary_by_user_id(
        self, channel_id: str, user_id: str
    ) -> UserChannelSummary | None:
        """Return the rolling summary for a user_id in a channel, or None.

        Looks up by the stable Discord user_id rather than the mutable display
        name. This is the canonical key — the table's unique constraint is on
        (channel_id, user_id) — and it survives nickname changes and exotic
        display names (e.g. Unicode "fraktur" nicks that never equal their
        ASCII spelling).
        """
        stmt = select(UserChannelSummary).where(
            UserChannelSummary.channel_id == channel_id,
            UserChannelSummary.user_id == user_id,
        )
        return self.session.exec(stmt).first()

    def upsert_summary(
        self,
        channel_id: str,
        user_id: str,
        username: str,
        summary_text: str,
        last_message_id: int,
    ) -> None:
        """Insert or update a rolling summary for a user in a channel."""
        existing = self.session.exec(
            select(UserChannelSummary).where(
                UserChannelSummary.channel_id == channel_id,
                UserChannelSummary.user_id == user_id,
            )
        ).first()
        if existing:
            existing.summary = summary_text
            existing.username = username
            existing.last_message_id = last_message_id
            existing.updated_at = datetime.now(timezone.utc)
            self.session.add(existing)
        else:
            self.session.add(
                UserChannelSummary(
                    channel_id=channel_id,
                    user_id=user_id,
                    username=username,
                    summary=summary_text,
                    last_message_id=last_message_id,
                )
            )
        self.session.commit()

    def get_channel_summary(self, channel_id: str) -> ChannelSummary | None:
        """Return the rolling summary for a channel, or None."""
        stmt = select(ChannelSummary).where(ChannelSummary.channel_id == channel_id)
        return self.session.exec(stmt).first()

    def upsert_channel_summary(
        self,
        channel_id: str,
        summary_text: str,
        last_message_id: int,
        message_count: int,
    ) -> None:
        """Insert or update a rolling summary for a channel."""
        existing = self.session.exec(
            select(ChannelSummary).where(ChannelSummary.channel_id == channel_id)
        ).first()
        if existing:
            existing.summary = summary_text
            existing.last_message_id = last_message_id
            existing.message_count = message_count
            existing.updated_at = datetime.now(timezone.utc)
            self.session.add(existing)
        else:
            self.session.add(
                ChannelSummary(
                    channel_id=channel_id,
                    summary=summary_text,
                    last_message_id=last_message_id,
                    message_count=message_count,
                )
            )
        self.session.commit()

    def get_user_summaries_for_users(
        self, channel_id: str, user_ids: list[str]
    ) -> list[UserChannelSummary]:
        """Return user summaries for a specific set of users in a channel."""
        if not user_ids:
            return []
        stmt = select(UserChannelSummary).where(
            UserChannelSummary.channel_id == channel_id,
            UserChannelSummary.user_id.in_(user_ids),
        )
        return list(self.session.exec(stmt).all())

    def get_messages_with_thinking(self, limit: int = 200) -> list[Message]:
        """Return recent bot messages that have stored thinking, newest first."""
        stmt = (
            select(Message)
            .where(Message.is_bot == True, Message.thinking != None)  # noqa: E711,E712
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        return list(self.session.exec(stmt).all())

    def get_recent_bot_messages(self, limit: int = 200) -> list[Message]:
        """Return recent bot messages for button view re-registration on startup."""
        stmt = (
            select(Message)
            .where(Message.is_bot == True)  # noqa: E712
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        return list(self.session.exec(stmt).all())

    # -- Message lock operations ------------------------------------------------

    def acquire_lock(self, discord_message_id: str, channel_id: str) -> bool:
        """Try to claim a message for processing. Returns True if this caller won."""
        nested = self.session.begin_nested()
        try:
            self.session.add(
                MessageLock(
                    discord_message_id=discord_message_id,
                    channel_id=channel_id,
                )
            )
            self.session.flush()
            nested.commit()
            return True
        except IntegrityError:
            nested.rollback()
            return False

    def mark_completed(self, discord_message_id: str) -> None:
        """Mark a lock as completed after successful processing."""
        lock = self.session.get(MessageLock, discord_message_id)
        if lock:
            lock.completed = True
            self.session.add(lock)
            self.session.commit()

    def release_lock(self, discord_message_id: str) -> None:
        """Delete a lock on failure so it can be reclaimed immediately."""
        lock = self.session.get(MessageLock, discord_message_id)
        if lock:
            self.session.delete(lock)
            self.session.commit()

    def reclaim_expired(
        self, ttl_seconds: int = 30, limit: int = 5
    ) -> list[MessageLock]:
        """Reclaim locks that expired without completing.

        Uses FOR UPDATE SKIP LOCKED so multiple pods won't grab the same row.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
        sql = text(
            "SELECT * FROM chat.message_locks "
            "WHERE completed = false AND claimed_at < :cutoff "
            "ORDER BY claimed_at "
            "LIMIT :limit "
            "FOR UPDATE SKIP LOCKED"
        )
        rows = self.session.exec(sql, params={"cutoff": cutoff, "limit": limit})
        locks = [MessageLock.model_validate(row) for row in rows]

        # Re-claim by bumping claimed_at
        now = datetime.now(timezone.utc)
        for lock in locks:
            refreshed = self.session.get(MessageLock, lock.discord_message_id)
            if refreshed:
                refreshed.claimed_at = now
                self.session.add(refreshed)
        self.session.commit()
        return locks

    def cleanup_completed(self, max_age_seconds: int = 3600) -> int:
        """Delete completed locks older than max_age. Returns count deleted."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        result = self.session.exec(
            text(
                "DELETE FROM chat.message_locks "
                "WHERE completed = true AND claimed_at < :cutoff"
            ),
            params={"cutoff": cutoff},
        )
        self.session.commit()
        return result.rowcount
