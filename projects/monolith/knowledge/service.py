"""Startup hook that registers the knowledge scheduled jobs."""

import asyncio
import logging
import time
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlmodel import Session

from core.db import get_engine
from knowledge.gardener import _slugify
from knowledge.models import Note, NoteLink
from knowledge.visibility import public_notes_filter

logger = logging.getLogger(__name__)


def on_startup(session: Session) -> None:
    """Register knowledge jobs with the scheduler.

    All vault-coupled jobs (reconcile, vault-backup, classify-gaps,
    research-gaps, detect-drift) were removed with the Obsidian
    decommission (ADR 006); the gap loop is now fully fileless and the
    surviving jobs operate purely on Postgres / S3.
    ``purge_unregistered_jobs`` drops the orphaned ScheduledJob rows for
    the deregistered handlers on the next startup.
    """
    from scheduler.api import register_job
