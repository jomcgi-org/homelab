"""Per-session goose sessions.db blob store (ADR 026 Phase 2).

The durable store for a thread's goose SQLite session, so a reply can resume the
prior conversation (Model A) instead of cold-rebuilding from the full transcript
(Model B). This is the single seam between the goosecracker runner and durable
storage.

Backed by the SeaweedFS S3 object store via ``artifact.s3`` (the same seam the
artifact HTML uses): the session lands at
``s3://artifacts/<session_id>/sessions.db``, sharing the id namespace and bucket
lifecycle with the published artifact (so the artifact session-prune job also
evicts stale sessions). The monolith mediates every S3 access (the agent guest
holds no credential): the runner ships the bytes to the guest inline in the
AgentRequest and stores the returned bytes here, so the guest never touches S3.

The blob is kilobytes (goose exits between turns, so sessions.db is consistent at
export). boto3 does blocking network I/O, so an async caller must hand these to
``asyncio.to_thread``.
"""

from __future__ import annotations

import logging

from artifact import s3

logger = logging.getLogger(__name__)


def load(session_id: str) -> bytes | None:
    """Return the stored sessions.db blob for ``session_id``, or None.

    None means "no prior session" (a first/cold run, or a run that never persisted
    one), which the runner reads as "cold run, do not resume".
    """
    return s3.get_session(session_id)


def save(session_id: str, data: bytes) -> None:
    """Persist the sessions.db blob for ``session_id`` to S3.

    Overwrites any prior session for the id so the next reply resumes from the
    latest state (``artifact.s3.put_session`` creates the bucket on first use).
    """
    s3.put_session(session_id, data)
