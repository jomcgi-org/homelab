"""artifact batch jobs (ADR 026 Phase 2).

Off-pod jobs for the artifact domain, run as Argo CronWorkflows via
``app/jobs_main.py`` (the in-process scheduler was retired). Today this is just
the goose-session TTL sweep (Task 2.5): abandoned threads' persisted session DBs
are evicted so storage does not grow unbounded.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlmodel import Session

logger = logging.getLogger("monolith.artifact.jobs")

# Evict persisted goose sessions whose threads have been idle this long. Sessions
# are kilobytes each, so this is generous; an evicted session just makes the next
# reply cold-rebuild (Model B). Artifacts are never evicted (they are the served
# output). Override via ARTIFACT_SESSION_TTL_DAYS.
SESSION_TTL_DAYS = int(os.environ.get("ARTIFACT_SESSION_TTL_DAYS", "30"))


def evict_stale_sessions_handler(_session: Session | None = None) -> int:
    """Delete goose session DBs older than the TTL. Returns the count deleted.

    The ``_session`` arg matches the batch-job handler contract (the CLI opens a
    DB session for it); this job only touches S3, so it is unused.
    """
    from artifact import s3

    deleted = s3.prune_sessions(SESSION_TTL_DAYS)
    logger.info(
        "artifact: evicted %d stale goose session(s) (ttl=%dd)",
        deleted,
        SESSION_TTL_DAYS,
    )
    return deleted
