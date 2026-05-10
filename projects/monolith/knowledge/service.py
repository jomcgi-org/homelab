"""Startup hook that registers the knowledge scheduled jobs."""

import asyncio
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from dulwich import porcelain

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlmodel import Session

from knowledge.gardener import _slugify
from knowledge.layout import EdgeRef, LayoutParams, NodePos, compute_layout
from knowledge.models import Note, NoteLink
from knowledge.reconciler import Reconciler
from knowledge.store import KnowledgeStore
from shared.embedding import EmbeddingClient

logger = logging.getLogger(__name__)

VAULT_ROOT_ENV = "VAULT_ROOT"
DEFAULT_VAULT_ROOT = "/vault"
# 5-minute reconcile cycle. _TTL_SECS is the lock-lease: a worker holding
# the row past this is treated as crashed and the lock can be reclaimed.
# We keep it generous (20m) so LLM-heavy handlers can finish without being
# preempted; the tradeoff is slower recovery if a pod actually dies mid-job.
_INTERVAL_SECS = 300
_TTL_SECS = 1200
_BACKUP_INTERVAL_SECS = 900  # 15 minutes
_BACKUP_TTL_SECS = 1200  # 20 minute lock-lease (git push can be slow)
_INGEST_INTERVAL_SECS = 300
_INGEST_TTL_SECS = 1200
_CLASSIFY_INTERVAL_SECS = 60  # 1-minute tick
# Scheduler reclaims jobs whose lock-lease exceeds ttl_secs. Must comfortably
# exceed gap_classifier._CLASSIFY_TIMEOUT_SECS (300s) — otherwise a long-
# running classifier subprocess would have its lock reclaimed mid-flight,
# risking a second replica racing Edit calls on the same stubs.
_CLASSIFY_TTL_SECS = 360  # 300s subprocess timeout + 60s headroom
_CLASSIFY_BATCH_SIZE = 10
_RESEARCH_INTERVAL_SECS = 86400  # once per day
_RESEARCH_TTL_SECS = (
    1200  # 20min lock-lease (Sonnet research runs can be slow with web tools)
)
_GIT_READY_SENTINEL = ".git-ready"
_SYNC_READY_SENTINEL = ".sync-ready"
_GIT_AUTHOR = b"vault-backup <vault-backup@monolith.local>"


def _vault_sync_ready() -> bool:
    """Return True if the obsidian sidecar has completed its initial sync."""
    vault_root = Path(os.environ.get(VAULT_ROOT_ENV, DEFAULT_VAULT_ROOT))
    return (vault_root / _SYNC_READY_SENTINEL).exists()


async def clone_vault() -> None:
    """Clone the vault repo to pre-seed the emptyDir volume.

    Skips if VAULT_GIT_REMOTE is not set or if the vault already has a .git dir.
    Always writes a .git-ready sentinel so the obsidian sidecar can start.
    """
    vault_root = Path(os.environ.get(VAULT_ROOT_ENV, DEFAULT_VAULT_ROOT))
    try:
        remote = os.environ.get("VAULT_GIT_REMOTE", "")
        if not remote:
            logger.info("VAULT_GIT_REMOTE not set, skipping clone")
            return

        if (vault_root / ".git").exists():
            logger.info("Vault at %s already initialised, skipping clone", vault_root)
            return

        token = os.environ.get("GITHUB_TOKEN", "")
        clone_kwargs: dict = {
            "source": remote,
            "target": str(vault_root),
            "depth": 1,
        }
        if token:
            clone_kwargs["username"] = "x-access-token"
            clone_kwargs["password"] = token

        try:
            porcelain.clone(**clone_kwargs)
            logger.info("Vault cloned from git to %s", vault_root)
        except Exception as exc:
            logger.warning("Vault clone failed, proceeding without pre-seed: %s", exc)
    finally:
        vault_root.mkdir(parents=True, exist_ok=True)
        (vault_root / _GIT_READY_SENTINEL).touch()


def _has_changes(vault_root: Path) -> bool:
    """Check if the vault has any uncommitted or untracked changes."""
    status = porcelain.status(str(vault_root))
    has_staged = any(status.staged.get(k) for k in ("add", "delete", "modify"))
    return has_staged or bool(status.unstaged) or bool(status.untracked)


async def vault_backup_handler() -> datetime | None:
    """Commit and push vault changes to GitHub (best-effort).

    Called by the scheduler and during shutdown.
    """
    if not _vault_sync_ready():
        logger.info("knowledge.vault-backup: vault sync not ready, deferring")
        return None
    vault_root = Path(os.environ.get(VAULT_ROOT_ENV, DEFAULT_VAULT_ROOT))
    if not (vault_root / ".git").exists():
        logger.info("knowledge.vault-backup: no .git dir, skipping")
        return None

    if not _has_changes(vault_root):
        logger.info("knowledge.vault-backup: no changes to commit")
        return None

    try:
        porcelain.add(str(vault_root))
        porcelain.commit(
            str(vault_root),
            message=b"sync: vault backup",
            author=_GIT_AUTHOR,
            committer=_GIT_AUTHOR,
        )
        token = os.environ.get("GITHUB_TOKEN", "")
        push_kwargs: dict = {}
        if token:
            push_kwargs["username"] = "x-access-token"
            push_kwargs["password"] = token
        porcelain.push(str(vault_root), **push_kwargs)
        logger.info("knowledge.vault-backup: committed and pushed")
    except Exception as exc:
        logger.warning("knowledge.vault-backup: push failed: %s", exc)
    return None


async def garden_handler(session: Session) -> datetime | None:
    """Scheduler handler: run the knowledge vault gardener."""
    if not _vault_sync_ready():
        logger.info("knowledge.garden: vault sync not ready, deferring")
        return None
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        logger.warning("knowledge.garden: CLAUDE_CODE_OAUTH_TOKEN not set, skipping")
        return None

    from knowledge.gardener import Gardener

    vault_root = Path(os.environ.get(VAULT_ROOT_ENV, DEFAULT_VAULT_ROOT))
    try:
        max_files = int(os.environ.get("GARDENER_MAX_FILES_PER_RUN", "10"))
    except ValueError:
        logger.warning(
            "knowledge.garden: GARDENER_MAX_FILES_PER_RUN is not an integer, "
            "falling back to default",
        )
        max_files = 10
    gardener = Gardener(
        vault_root=vault_root,
        max_files_per_run=max_files,
        session=session,
    )
    stats = await gardener.run()
    extra = {
        "resolved": stats.resolved,
        "moved": stats.moved,
        "deduped": stats.deduped,
        "reconciled": stats.reconciled,
        "ingested": stats.ingested,
        "failed": stats.failed,
        "gaps_discovered": stats.gaps_discovered,
    }
    if stats.ingested == 0 and stats.failed > 0:
        logger.error("knowledge.garden complete (all failed)", extra=extra)
    else:
        logger.info("knowledge.garden complete", extra=extra)
    return None


def _run_layout_pass(engine: Engine) -> tuple[int, int, int]:
    """Compute layout positions for the current graph and persist them.

    Opens its own SQLAlchemy session bound to the caller-supplied engine
    so the work can be dispatched onto a worker thread via
    ``asyncio.to_thread`` without sharing the loop-thread session.
    Engines are thread-safe (the connection pool lives on them); sessions
    are not, which is why the caller passes ``session.get_bind()``
    instead of the session itself. Mirrors ``KnowledgeStore.get_graph``'s
    edge filter (only edges where both endpoints map to known note_ids)
    so positions and degrees stay coherent with what the API ships.
    Caller is responsible for catching exceptions and translating them
    to structured log events.
    """
    params = LayoutParams.from_env()

    with Session(engine) as session:
        note_rows = session.execute(
            select(Note.id, Note.note_id, Note.layout_x, Note.layout_y)
        ).all()
        fk_to_note_id: dict[int, str] = {r.id: r.note_id for r in note_rows}
        nodes = [
            NodePos(id=r.note_id, prior_x=r.layout_x, prior_y=r.layout_y)
            for r in note_rows
        ]

        edge_rows = session.execute(
            select(NoteLink.src_note_fk, NoteLink.target_id)
        ).all()
        note_id_set = set(fk_to_note_id.values())
        # Mirror ``KnowledgeStore.get_graph``'s slug-normalize-then-resolve
        # logic. Body wikilinks store ``target_id`` as raw user-typed text
        # (``"Steve Krug"``); gap stubs and ``_processed`` notes carry
        # ``note_id`` in slug form. Without this normalization FA2 treats
        # gap edges as missing and the gap nodes get no position from
        # ``compute_layout`` (the API then drops them as orphans).
        slug_to_note_id = {_slugify(nid): nid for nid in note_id_set}
        edges: list[EdgeRef] = []
        for r in edge_rows:
            if r.src_note_fk not in fk_to_note_id:
                continue
            canonical_target = slug_to_note_id.get(_slugify(r.target_id))
            if canonical_target is None:
                continue
            edges.append(
                EdgeRef(source=fk_to_note_id[r.src_note_fk], target=canonical_target)
            )

        positions = compute_layout(nodes, edges, params)

        if positions:
            for note_id, (x, y) in positions.items():
                session.execute(
                    update(Note)
                    .where(Note.note_id == note_id)
                    .values(layout_x=x, layout_y=y)
                )
            session.commit()

        return len(nodes), len(edges), len(positions)


async def reconcile_handler(session: Session) -> datetime | None:
    """Scheduler handler: run the knowledge vault reconciler."""
    if not _vault_sync_ready():
        logger.info("knowledge.reconcile: vault sync not ready, deferring")
        return None
    vault_root = Path(os.environ.get(VAULT_ROOT_ENV, DEFAULT_VAULT_ROOT))
    reconciler = Reconciler(
        store=KnowledgeStore(session=session),
        embed_client=EmbeddingClient(),
        vault_root=vault_root,
    )
    stats = await reconciler.run()
    logger.info(
        "knowledge.reconcile complete",
        extra={
            "upserted": stats.upserted,
            "deleted": stats.deleted,
            "unchanged": stats.unchanged,
            "failed": stats.failed,
            "skipped_locked": stats.skipped_locked,
        },
    )

    # Persist reconciler upserts before the layout step so the layout
    # pass — which opens its own session in a worker thread — sees the
    # upserts via the committed snapshot.
    session.commit()

    # Dispatch the layout pass to a worker thread. ``compute_layout`` is
    # CPU-bound (FA2 + the hard-collide pass); running it on the asyncio
    # loop blocked uvicorn for >20s on the live graph and tripped the
    # ``/healthz`` liveness probe, which surfaced as 5xx on the notes
    # page. ``_run_layout_pass`` opens its own SQLAlchemy session bound
    # to the caller's engine — engines are thread-safe, sessions are not.
    start = time.perf_counter()
    try:
        node_count, edge_count, positioned = await asyncio.to_thread(
            _run_layout_pass, session.get_bind()
        )
    except Exception:  # noqa: BLE001 — layout failure must not affect reconcile result
        logger.exception("knowledge.layout: pass failed")
    else:
        logger.info(
            "knowledge.layout: pass succeeded",
            extra={
                "node_count": node_count,
                "edge_count": edge_count,
                "positioned": positioned,
                "duration_ms": int((time.perf_counter() - start) * 1000),
            },
        )

    return None


async def classify_gaps_handler(session: Session) -> datetime | None:
    """Scheduler handler: classify a batch of gap stubs via Claude subprocess.

    Globs _researching/*.md for stubs with no gap_class set, takes up to
    _CLASSIFY_BATCH_SIZE of them, and calls classify_stubs. Claude edits
    the stub frontmatter in place; the reconciler projects the edits into
    the Gap table on its next tick.

    Returns None (matches the repo's scheduler contract).
    """
    if not _vault_sync_ready():
        logger.info("knowledge.classify-gaps: vault sync not ready, deferring")
        return None
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        logger.warning(
            "knowledge.classify-gaps: CLAUDE_CODE_OAUTH_TOKEN not set, skipping"
        )
        return None

    vault_root = Path(os.environ.get(VAULT_ROOT_ENV, DEFAULT_VAULT_ROOT))
    researching_dir = vault_root / "_researching"
    if not researching_dir.is_dir():
        logger.info("knowledge.classify-gaps: no _researching/ directory yet, skipping")
        return None

    from knowledge.gap_classifier import classify_stubs
    from knowledge.gap_stubs import parse_stub_frontmatter

    pending: list[Path] = []
    for stub in sorted(researching_dir.glob("*.md")):
        try:
            meta = parse_stub_frontmatter(stub)
        except Exception:
            logger.warning(
                "knowledge.classify-gaps: failed to parse %s, skipping",
                stub,
                exc_info=True,
            )
            continue
        if meta.get("gap_class") is None:
            pending.append(stub)
        if len(pending) >= _CLASSIFY_BATCH_SIZE:
            break

    if not pending:
        logger.info("knowledge.classify-gaps: no pending stubs")
        return None

    stats = await classify_stubs(pending)
    logger.info(
        "knowledge.classify-gaps complete",
        extra={
            "stubs_processed": stats.stubs_processed,
            "duration_ms": stats.duration_ms,
        },
    )
    return None


async def research_gaps_handler(session: Session) -> datetime | None:
    """Scheduler handler: drain the external research pipeline by one batch."""
    if not _vault_sync_ready():
        logger.info("knowledge.research-gaps: vault sync not ready, deferring")
        return None
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        logger.warning(
            "knowledge.research-gaps: CLAUDE_CODE_OAUTH_TOKEN not set, skipping"
        )
        return None

    vault_root = Path(os.environ.get(VAULT_ROOT_ENV, DEFAULT_VAULT_ROOT))

    from knowledge.research_handler import research_gaps_handler as _impl

    await _impl(session=session, vault_root=vault_root)
    return None


def on_startup(session: Session) -> None:
    """Register knowledge jobs with the scheduler."""
    from shared.scheduler import register_job

    # The scheduler claims one job per tick (LIMIT 1) and polls every 30s,
    # so the two jobs always run in separate ticks — there is no hard
    # ordering guarantee between them within a single cycle, and Postgres
    # gives no tiebreaker for identical next_run_at values. Registration
    # order is documentary rather than load-bearing. The eventual
    # consistency is fine: any file the gardener writes to _processed/ is
    # picked up by the reconciler on its next tick (~30s later).
    register_job(
        session,
        name="knowledge.garden",
        interval_secs=_INTERVAL_SECS,
        handler=garden_handler,
        ttl_secs=_TTL_SECS,
    )
    register_job(
        session,
        name="knowledge.reconcile",
        interval_secs=_INTERVAL_SECS,
        handler=reconcile_handler,
        ttl_secs=_TTL_SECS,
    )
    register_job(
        session,
        name="knowledge.vault-backup",
        interval_secs=_BACKUP_INTERVAL_SECS,
        handler=lambda _: vault_backup_handler(),
        ttl_secs=_BACKUP_TTL_SECS,
    )

    from knowledge.ingest_queue import ingest_handler

    register_job(
        session,
        name="knowledge.ingest",
        interval_secs=_INGEST_INTERVAL_SECS,
        handler=ingest_handler,
        ttl_secs=_INGEST_TTL_SECS,
    )
    register_job(
        session,
        name="knowledge.classify-gaps",
        interval_secs=_CLASSIFY_INTERVAL_SECS,
        handler=classify_gaps_handler,
        ttl_secs=_CLASSIFY_TTL_SECS,
    )
    register_job(
        session,
        name="knowledge.research-gaps",
        interval_secs=_RESEARCH_INTERVAL_SECS,
        handler=research_gaps_handler,
        ttl_secs=_RESEARCH_TTL_SECS,
    )
