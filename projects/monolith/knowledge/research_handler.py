"""Scheduled-job handler for knowledge.research-gaps.

Every tick: pulls up to RESEARCH_BATCH_SIZE external+classified gaps,
runs the Sonnet research agent (single subprocess that triages and
optionally researches), routes per disposition.

  - ``research`` + non-empty post-filter claims: write the research raw,
    state -> "committed".
  - ``research`` + empty post-filter claims (everything failed citation
    check): quarantine the draft, bump ``research_attempts``. Below
    threshold the stub is left in place so the next tick retries; at
    threshold the gap is parked and the stub is deleted.
  - ``personal``: gap_class flips to ``internal``, state stays
    ``classified``. Sonnet caught a mis-classification. The stub is
    deleted -- if the term reappears in another note, the gardener
    will queue a fresh stub for re-classification.
  - ``discard``: gap_class flips to ``parked``, state -> ``parked``.
    Sonnet decided this isn't worth researching at all. Stub deleted.
  - Infra failures (claude subprocess timeout / non-zero exit / parse
    error) revert state without burning a research attempt.

Stubs MUST be deleted on every terminal disposition: the reconciler
projects ``_researching/<slug>.md`` frontmatter into the Gap row on
every cycle, so a stub left at ``status: classified`` reverts the DB
back to ``classified`` and the gap is re-picked indefinitely.

Triage is a refinement of the upstream ``gap_classifier``, not a
replacement -- the classifier's "external" candidates flow into here,
and Sonnet may downgrade them based on full vault context.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy import update
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from knowledge.gap_stubs import RESEARCHING_DIR
from knowledge.models import Gap
from knowledge.research_agent import AGENT_MODEL, ResearchResult, run_research
from knowledge.research_writer import quarantine, write_research_raw

logger = logging.getLogger(__name__)

RESEARCH_BATCH_SIZE = 10
RESEARCH_CONCURRENCY = 10
RESEARCH_PARK_THRESHOLD = 3


def _delete_stub(vault_root: Path, slug: str) -> None:
    # Used by the personal/quarantine paths where we want the gardener
    # to be free to recreate the stub if the term reappears. The reconciler
    # projects stub frontmatter into the Gap row on every cycle, so leaving
    # a stub at status=classified after the handler flipped the DB row to a
    # terminal state would cause the next reconciler tick to revert state
    # -> classified and the gap would be re-picked next research tick.
    # Removing the stub stops the projection.
    stub = vault_root / RESEARCHING_DIR / f"{slug}.md"
    try:
        stub.unlink()
    except FileNotFoundError:
        pass


def _lock_and_fetch_gap(engine: Engine, gap_id: int) -> Gap | None:
    """Atomically lock a classified gap to 'researching' and return a snapshot.

    Returns None if the row is no longer at state='classified' (another
    worker already claimed it). The returned ``Gap`` is detached from
    the closed session, so callers must only read attributes that were
    loaded by ``.get()`` and must not assign to its fields.

    Opens its own session per the canonical PR #2297 pattern so that
    ``asyncio.to_thread`` does not share a Session across the loop/
    thread boundary (``no-session-in-to-thread`` rule).
    """
    with Session(engine) as session:
        result = session.execute(
            update(Gap)
            .where(Gap.id == gap_id, Gap.state == "classified")
            .values(state="researching")
        )
        session.commit()
        if result.rowcount == 0:
            return None
        return session.get(Gap, gap_id)


def _finalize_gap_state(
    engine: Engine,
    gap_id: int,
    *,
    state: str,
    gap_class: str | None = None,
    research_attempts: int | None = None,
) -> None:
    """Update a Gap row to a terminal state in a fresh session.

    Bundles state + optional gap_class / research_attempts into one
    UPDATE so the per-disposition path in ``_process_one`` makes one
    DB round-trip per terminal transition rather than two.

    Fresh session per call (canonical PR #2297 pattern) so the caller's
    loop-thread session never crosses into the worker thread that runs
    the COMMIT round-trip.
    """
    values: dict[str, object] = {"state": state}
    if gap_class is not None:
        values["gap_class"] = gap_class
    if research_attempts is not None:
        values["research_attempts"] = research_attempts
    with Session(engine) as session:
        session.execute(update(Gap).where(Gap.id == gap_id).values(**values))
        session.commit()


def _sweep_and_select_candidates(engine: Engine) -> tuple[int, list[Gap]]:
    """Recover stuck 'researching' rows and fetch the next batch; sync DB I/O.

    Takes the engine (not a session) and opens its own session per the
    canonical PR #2297 pattern -- Sessions are not thread-safe and the
    ``no-session-in-to-thread`` rule forbids passing the caller's
    session across the loop/thread boundary.
    """
    with Session(engine) as session:
        stuck = session.execute(
            Gap.__table__.update()
            .where(Gap.state == "researching")
            .values(state="classified")
        )
        session.commit()
        return stuck.rowcount, list(
            session.execute(
                select(Gap)
                .where(
                    Gap.deleted_at.is_(None),
                    Gap.gap_class == "external",
                    Gap.state == "classified",
                )
                .order_by(Gap.id)
                .limit(RESEARCH_BATCH_SIZE)
            )
            .scalars()
            .all()
        )


def _mark_stub_discardable(vault_root: Path, slug: str) -> None:
    """Mark the stub as ``triaged: discardable`` so the gardener can
    rewrite source wikilinks on its next tick.

    Without this, every Sonnet ``discard`` is a treadmill: the gap row
    ends up parked, gets cleaned up, and then the gardener re-discovers
    the same wikilink in source notes and creates a new stub. Marking
    the stub plugs ``research_handler`` into the existing Phase A
    discardable-rewrite path in ``gaps.discover_gaps`` (gated by
    ``KNOWLEDGE_GAPS_REWRITE_DISCARDABLE``; default off = dry-run that
    just logs ``rewrites_dryrun=N``).

    Also overwrites ``status`` and ``gap_class`` to ``parked`` so the
    next reconciler tick projects matching values onto the Gap row
    instead of reverting it to the classifier's earlier classified+external
    state. ``parked`` is in the migration's CHECK list and in the
    reconciler's ``_VALID_GAP_STATES`` (also extended in this commit).

    Idempotent: missing stub is a no-op; an already-marked stub is left
    alone to avoid mtime churn.
    """
    stub = vault_root / RESEARCHING_DIR / f"{slug}.md"
    try:
        text = stub.read_text()
    except FileNotFoundError:
        return
    if not text.startswith("---\n"):
        return
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        return
    meta = yaml.safe_load(parts[1])
    if not isinstance(meta, dict):
        return
    if (
        meta.get("triaged") == "discardable"
        and meta.get("status") == "parked"
        and meta.get("gap_class") == "parked"
    ):
        return  # already marked — skip the write to keep mtime stable.
    meta["triaged"] = "discardable"
    meta["status"] = "parked"
    meta["gap_class"] = "parked"
    fm_str = yaml.dump(meta, default_flow_style=False, sort_keys=False)
    stub.write_text(f"---\n{fm_str}---\n{parts[2]}")


async def research_gaps_handler(*, session: Session, vault_root: Path) -> None:
    """Run one tick of the external research pipeline.

    Fans out up to ``RESEARCH_CONCURRENCY`` gaps in parallel via
    ``asyncio.gather``. Each task opens its own SQLAlchemy session from
    the bound engine -- sync ``Session`` objects are not safe to share
    across concurrently-awaiting tasks (the underlying psycopg
    connection can be corrupted by interleaved execute() calls).

    The passed-in ``session`` handles the recovery sweep + candidate
    SELECT only; the parallel tasks never touch it.
    """
    engine = session.get_bind()

    # Recovery sweep: if the previous tick crashed mid-flight (between
    # the 'researching' lock and any terminal state assignment), the
    # row would be stuck forever -- the SELECT below filters
    # state='classified', so nothing would ever pick it back up. Sweep
    # stuck rows back to 'classified' before each tick. Safe under the
    # single-worker scheduler model.
    stuck_count, candidates = await asyncio.to_thread(
        _sweep_and_select_candidates, engine
    )
    if stuck_count:
        logger.warning(
            "knowledge.research-gaps: recovered %d stuck 'researching' rows to "
            "'classified'",
            stuck_count,
        )

    if not candidates:
        logger.info("knowledge.research-gaps: no candidates")
        return

    # Capture plain values up front so the parallel tasks don't depend
    # on the original session staying open or on ORM lazy-loads.
    gap_descriptors = [(gap.id, gap.term, gap.gap_class) for gap in candidates]

    semaphore = asyncio.Semaphore(RESEARCH_CONCURRENCY)
    results = await asyncio.gather(
        *[
            _claim_and_process(
                engine=engine,
                gap_id=gap_id,
                term=term,
                gap_class=gap_class,
                vault_root=vault_root,
                semaphore=semaphore,
            )
            for gap_id, term, gap_class in gap_descriptors
        ],
        return_exceptions=True,
    )
    # Per-gap try/except inside _process_one already reverts state on
    # failure. Anything that escapes here is a bug; log loudly with the
    # full traceback so we see it instead of silently dropping a sibling
    # task's exception. (Can't use `exc_info=outcome` -- semgrep rule
    # `logger-exc-info-non-boolean` flags plain-variable exc_info because
    # the linter can't statically verify the value is an exception.)
    for (gap_id, term, _), outcome in zip(gap_descriptors, results):
        if isinstance(outcome, BaseException):
            tb = "".join(
                traceback.format_exception(
                    type(outcome), outcome, outcome.__traceback__
                )
            )
            logger.error(
                "knowledge.research-gaps: unexpected exception for gap %s "
                "(id=%d); other tasks unaffected:\n%s",
                term,
                gap_id,
                tb,
            )


async def _claim_and_process(
    *,
    engine: Engine,
    gap_id: int,
    term: str,
    gap_class: str | None,
    vault_root: Path,
    semaphore: asyncio.Semaphore,
) -> None:
    """Lock one gap row and dispatch it to ``_process_one``.

    Each invocation runs on its own SQLAlchemy session (one connection
    from the engine pool), bracketed by the semaphore so we never burst
    above ``RESEARCH_CONCURRENCY`` open subprocesses.
    """
    async with semaphore:
        # Defense-in-depth privacy guard: even though the SELECT
        # filtered, re-assert before each Sonnet call. Cheap, prevents
        # future misroutes.
        if gap_class != "external":
            logger.warning(
                "knowledge.research-gaps: skipping non-external gap %s", term
            )
            return

        gap = await asyncio.to_thread(_lock_and_fetch_gap, engine, gap_id)
        if gap is None:
            logger.info("knowledge.research-gaps: race lost for %s", term)
            return

        await _process_one(engine=engine, gap=gap, vault_root=vault_root)


async def _process_one(*, engine: Engine, gap: Gap, vault_root: Path) -> None:
    # ``gap`` is the detached snapshot returned by ``_lock_and_fetch_gap``.
    # We only READ from it here -- all state mutations go through
    # ``_finalize_gap_state`` against a fresh session in a worker thread,
    # so we don't share a Session across the loop/thread boundary
    # (``no-session-in-to-thread`` rule).
    #
    # gap.note_id is the slug used for both _inbox/research/<slug>.md
    # and _failed_research/<slug>-<N>.md. Schema permits NULL, but a
    # classified external gap reaching this point must have one (the
    # reconciler always links a stub note before classification).
    # Assert to fail loudly rather than write files literally named
    # 'None.md'.
    assert gap.note_id is not None, f"gap {gap.id} ({gap.term}) has no note_id"

    researched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        result: ResearchResult = await run_research(
            term=gap.term, vault_root=vault_root
        )
    except Exception:
        logger.exception(
            "knowledge.research-gaps: research failure on %s; reverting state",
            gap.term,
        )
        await asyncio.to_thread(_finalize_gap_state, engine, gap.id, state="classified")
        return

    if result.disposition == "personal":
        await asyncio.to_thread(
            _finalize_gap_state,
            engine,
            gap.id,
            state="classified",
            gap_class="internal",
        )
        _delete_stub(vault_root, gap.note_id)
        logger.info(
            "knowledge.research-gaps: %s -> personal (gap_class=internal); reason=%s",
            gap.term,
            result.reason,
        )
        return

    if result.disposition == "discard":
        await asyncio.to_thread(
            _finalize_gap_state,
            engine,
            gap.id,
            state="parked",
            gap_class="parked",
        )
        _mark_stub_discardable(vault_root, gap.note_id)
        logger.info(
            "knowledge.research-gaps: %s -> discard (gap_class=parked); reason=%s",
            gap.term,
            result.reason,
        )
        return

    # disposition == "research" beyond this point.
    assert result.note is not None, "research disposition implies note is set"

    if not result.note.claims:
        # All emitted claims failed the mechanical citation filter.
        # Quarantine the draft, bump attempts, park if over threshold.
        attempt = gap.research_attempts + 1
        try:
            quarantine(
                vault_root=vault_root,
                slug=gap.note_id,
                attempt=attempt,
                summary=result.note.summary,
                pre_filter_claims=list(result.raw_claims),
                sources=list(result.sources),
                agent_model=AGENT_MODEL,
                researched_at=researched_at,
            )
        except Exception:
            logger.exception(
                "knowledge.research-gaps: quarantine write failed for %s",
                gap.term,
            )
            await asyncio.to_thread(
                _finalize_gap_state, engine, gap.id, state="classified"
            )
            return

        new_state = "parked" if attempt >= RESEARCH_PARK_THRESHOLD else "classified"
        await asyncio.to_thread(
            _finalize_gap_state,
            engine,
            gap.id,
            state=new_state,
            research_attempts=attempt,
        )
        if new_state == "parked":
            _delete_stub(vault_root, gap.note_id)
        logger.info(
            "knowledge.research-gaps: rejected %s (attempt=%d, state=%s)",
            gap.term,
            attempt,
            new_state,
        )
        return

    try:
        write_research_raw(
            vault_root=vault_root,
            slug=gap.note_id,
            title=gap.term,
            summary=result.note.summary,
            supported_claims=list(result.note.claims),
            sources=list(result.sources),
            agent_model=AGENT_MODEL,
            researched_at=researched_at,
        )
    except Exception:
        logger.exception(
            "knowledge.research-gaps: raw write failed for %s; reverting state",
            gap.term,
        )
        await asyncio.to_thread(_finalize_gap_state, engine, gap.id, state="classified")
        return

    await asyncio.to_thread(_finalize_gap_state, engine, gap.id, state="committed")
    logger.info(
        "knowledge.research-gaps: committed %s (claims=%d)",
        gap.term,
        len(result.note.claims),
    )


__all__ = [
    "RESEARCH_BATCH_SIZE",
    "RESEARCH_PARK_THRESHOLD",
    "research_gaps_handler",
]
