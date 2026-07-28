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
from knowledge.layout import EdgeRef, LayoutParams, NodePos, compute_layout
from knowledge.models import Note, NoteLink
from knowledge.visibility import public_notes_filter

logger = logging.getLogger(__name__)


# Layout refresh cadence. _TTL_SECS is the lock-lease: a worker holding the
# row past this is treated as crashed and the lock can be reclaimed. We keep
# it generous (20m) so the CPU-bound layout pass can finish without being
# preempted; the tradeoff is slower recovery if a pod actually dies mid-job.
_INTERVAL_SECS = 300
_TTL_SECS = 1200
_INGEST_INTERVAL_SECS = 300
_INGEST_TTL_SECS = 1200
# Fileless gap discovery: a quick Postgres scan of note_links for unresolved
# wikilinks. 5-minute cadence matches the other knowledge jobs; the scan is
# cheap so a 10-minute lock-lease is ample headroom.
_DISCOVER_INTERVAL_SECS = 300
_DISCOVER_TTL_SECS = 600


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
            select(Note.id, Note.note_id, Note.layout_x, Note.layout_y).where(
                Note.deleted_at.is_(None)
            )
        ).all()
        fk_to_note_id: dict[int, str] = {r.id: r.note_id for r in note_rows}
        nodes = [
            NodePos(id=r.note_id, prior_x=r.layout_x, prior_y=r.layout_y)
            for r in note_rows
        ]

        # Source-side deletes are excluded via the fk_to_note_id filter
        # below; an inner join here would still pull edges whose source
        # was deleted, but the loop drops anything not in fk_to_note_id.
        # Filtering at SQL is purely an optimisation, not a correctness
        # requirement.
        edge_rows = session.execute(
            select(NoteLink.src_note_fk, NoteLink.target_id)
        ).all()
        note_id_set = set(fk_to_note_id.values())
        # Mirror ``KnowledgeStore.get_graph``'s slug-normalize-then-resolve
        # logic. Body wikilinks store ``target_id`` as raw user-typed text
        # (``"Steve Krug"``); resolved notes carry ``note_id`` in slug form.
        # Without this normalization FA2 treats those edges as missing and
        # the nodes get no position from ``compute_layout`` (the API then
        # drops them as orphans).
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


def _run_public_layout_pass(engine: Engine) -> tuple[int, int, int]:
    """Compute layout positions for the public-visibility subgraph only.

    Mirrors :func:`_run_layout_pass` but restricts to ``visibility = 'public'``
    notes and persists positions to ``layout_x_public`` / ``layout_y_public``.
    The full-graph layout (``layout_x`` / ``layout_y``) is unchanged — the
    two passes are independent so the private notes page continues to render
    from its existing positions regardless of which finishes first.

    Edge filter mirrors :func:`get_public_graph`: both endpoints must
    resolve to a public note (source via the SQL filter on ``Note``, target
    via the slug→canonical resolution against the public-only id set).
    """
    params = LayoutParams.from_env()

    with Session(engine) as session:
        note_rows = session.execute(
            select(
                Note.id,
                Note.note_id,
                Note.layout_x_public,
                Note.layout_y_public,
            )
            .where(public_notes_filter())
            .where(Note.deleted_at.is_(None))
        ).all()
        fk_to_note_id: dict[int, str] = {r.id: r.note_id for r in note_rows}
        nodes = [
            NodePos(id=r.note_id, prior_x=r.layout_x_public, prior_y=r.layout_y_public)
            for r in note_rows
        ]

        if not nodes:
            return 0, 0, 0

        edge_rows = session.execute(
            select(NoteLink.src_note_fk, NoteLink.target_id)
            .join(Note, NoteLink.src_note_fk == Note.id)
            .where(public_notes_filter())
            .where(Note.deleted_at.is_(None))
        ).all()
        note_id_set = set(fk_to_note_id.values())
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
                    .values(layout_x_public=x, layout_y_public=y)
                )
            session.commit()

        return len(nodes), len(edges), len(positions)


async def layout_handler(session: Session) -> datetime | None:
    """Scheduler handler: recompute graph layout positions (full + public).

    Formerly hosted inside the vault reconciler; the reconciler is gone
    (the vault is decommissioned, ADR 006) but the layout pass is pure
    Postgres and still drives the knowledge-graph node positions.

    ``compute_layout`` is CPU-bound (FA2 + the hard-collide pass); running
    it on the asyncio loop blocked uvicorn for >20s on the live graph and
    tripped the ``/healthz`` liveness probe. Both passes are dispatched to a
    worker thread via ``asyncio.to_thread``. ``_run_layout_pass`` /
    ``_run_public_layout_pass`` open their own SQLAlchemy session bound to
    the caller's engine (engines are thread-safe, sessions are not), so we
    pass ``session.get_bind()`` (the engine) and never the session itself.
    """
    engine = session.get_bind()

    start = time.perf_counter()
    try:
        node_count, edge_count, positioned = await asyncio.to_thread(
            _run_layout_pass, engine
        )
    except Exception:  # noqa: BLE001: layout failure must not crash the scheduler
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

    # Public-only layout pass: independent of the full-graph pass so the
    # private notes page never loses its layout if the public pass fails,
    # and vice versa. Runs sequentially on a worker thread for the same
    # event-loop-blocking reason as the full pass.
    public_start = time.perf_counter()
    try:
        (
            public_node_count,
            public_edge_count,
            public_positioned,
        ) = await asyncio.to_thread(_run_public_layout_pass, engine)
    except Exception:  # noqa: BLE001 — same isolation rule as the full-graph pass
        logger.exception("knowledge.layout: public pass failed")
    else:
        logger.info(
            "knowledge.layout: public pass succeeded",
            extra={
                "node_count": public_node_count,
                "edge_count": public_edge_count,
                "positioned": public_positioned,
                "duration_ms": int((time.perf_counter() - public_start) * 1000),
            },
        )

    return None


def _discover_gaps_sync_core() -> int:
    """Run fileless gap discovery in its own session (for ``to_thread``).

    Opens a fresh ``Session(get_engine())`` because SQLAlchemy sessions are
    not thread-safe: the scheduler's loop-thread session must never be passed
    into a worker thread (semgrep ``no-session-in-to-thread``). Returns the
    number of Gap rows inserted/resurrected this cycle.
    """
    from knowledge.gaps import discover_gaps

    with Session(get_engine()) as session:
        return discover_gaps(session)


async def discover_gaps_handler(session: Session) -> datetime | None:
    """Scheduler handler: scan note_links for unresolved wikilinks (fileless).

    Delegates all DB work to ``_discover_gaps_sync_core`` on a worker thread
    so the loop-thread session is never shared. The scheduler's ``session``
    argument is intentionally unused: the sync core owns its own session.

    Returns None so the scheduler advances by the default interval.
    """
    del session  # sync core opens its own session; never shared into to_thread
    inserted = await asyncio.to_thread(_discover_gaps_sync_core)
    logger.info("knowledge.discover-gaps: inserted %d gap(s)", inserted)
    return None


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

    register_job(
        session,
        name="knowledge.layout",
        interval_secs=_INTERVAL_SECS,
        handler=layout_handler,
        ttl_secs=_TTL_SECS,
        # FA2 layout loads the whole graph and is memory-heavy: serialize it
        # against other heavy jobs so the shared pod is not OOMKilled.
        heavy=True,
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
        name="knowledge.discover-gaps",
        interval_secs=_DISCOVER_INTERVAL_SECS,
        handler=discover_gaps_handler,
        ttl_secs=_DISCOVER_TTL_SECS,
    )
