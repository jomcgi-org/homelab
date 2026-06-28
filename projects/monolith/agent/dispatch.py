"""AgentWorkflow dispatch over the Firecracker snapshot/restore controller
(ADR 022, Phase 5; the substrate-keyed interface of ADR 019, the consumer of
ADR 021).

This is the thin ``submit`` / ``status`` seam the consumers call:

  submit(task, thread_id=None) -> thread_id
      no thread_id : create a new AgentThread (PENDING) the controller boots
                     (from the repo's warm base when one is built) and runs.
      thread_id    : resume an existing IDLE thread (a wake request).
  status(thread_id) -> the thread's current registry row.

Plus the wake triggers (ADR 022): a Discord reply (wake_for_discord_thread), a CI
event or manual nudge (wake, by thread id). The controller (fc-agentd) does the
actual boot/restore by reconciling the rows this module writes; nothing here
touches Firecracker directly, keeping the control plane in Postgres.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text
from sqlmodel import Session

from agent import threads
from app.db import get_engine

# Single Firecracker node today (node-4, AMD). Snapshots are node/arch-bound, so
# new threads are pinned here; revisit when a second same-ISA node exists.
DEFAULT_NODE = "node-4"
DEFAULT_ARCH = "amd64"


def _new_thread_id() -> str:
    return f"t-{uuid4().hex[:12]}"


def _resolve_base_ref(session: Session, repo: str, arch: str) -> str | None:
    """The built warm-base ref for repo+arch, if one exists (instant start)."""
    row = session.execute(
        text(
            """
            SELECT base_ref FROM claude_agent.agent_base_snapshots
             WHERE repo = :repo AND arch = :arch AND built_sha IS NOT NULL
            """
        ),
        {"repo": repo, "arch": arch},
    ).fetchone()
    return row.base_ref if row else None


def submit(
    task: str,
    thread_id: str | None = None,
    *,
    recipe: str = "agent",
    repo: str = "",
    branch: str = "main",
    discord_thread: str = "",
    tier: str = "",
    arch: str = DEFAULT_ARCH,
    node: str = DEFAULT_NODE,
    ttl_secs: int = 86400,
) -> dict:
    """Create a new agent thread, or resume an existing one by id.

    Returns a dict with the ``thread_id`` and the ``action`` taken
    ("create" or "resume"). The work assignment (``recipe``, the goose recipe
    name to run, defaulting to "agent"; and ``task``, the task description) is
    stored on the new thread's row so the controller can hand it to the guest
    microVM over vsock.

    ``tier`` selects the model substrate the controller injects into the guest
    (ADR 024): "artifact" reaches Gemini via OpenRouter (key swapped at egress),
    the default/empty tier reaches in-cluster Qwen. The tier also bounds which
    secret placeholders the guest holds, so it is the credential trust boundary.
    """
    if thread_id:
        result = threads.request_resume(thread_id)
        return {"thread_id": thread_id, "action": "resume", **result}

    new_id = _new_thread_id()
    with Session(get_engine()) as session:
        base_ref = _resolve_base_ref(session, repo, arch) if repo else None
        session.execute(
            text(
                """
                INSERT INTO claude_agent.agent_threads
                    (thread_id, state, repo, branch, node, arch,
                     base_snapshot_ref, discord_thread, ttl_secs,
                     recipe, task, tier)
                VALUES (:id, 'PENDING', :repo, :branch, :node, :arch,
                        :base_ref, :discord_thread, :ttl,
                        :recipe, :task, :tier)
                """
            ),
            {
                "id": new_id,
                "repo": repo,
                "branch": branch,
                "node": node,
                "arch": arch,
                "base_ref": base_ref,
                "discord_thread": discord_thread,
                "ttl": ttl_secs,
                "recipe": recipe,
                "task": task,
                "tier": tier,
            },
        )
        session.commit()
    return {"thread_id": new_id, "action": "create", "base_snapshot_ref": base_ref}


def status(thread_id: str) -> dict | None:
    """Return the thread's current registry row (serialized), or None."""
    row = threads.get_thread(thread_id)
    return threads.serialize(row) if row else None


def wake(thread_id: str) -> dict:
    """Wake an IDLE thread by id (a manual nudge or a CI event for a known id)."""
    return threads.request_resume(thread_id)


def wake_for_discord_thread(discord_thread: str) -> dict:
    """Wake the IDLE agent thread fronting a Discord thread (a reply arrived).

    Returns ok=False if no IDLE thread fronts that Discord thread.
    """
    with Session(get_engine()) as session:
        row = session.execute(
            text(
                """
                UPDATE claude_agent.agent_threads
                   SET wake_requested_at = now(), last_active_at = now()
                 WHERE discord_thread = :dt AND state = 'IDLE'
                RETURNING thread_id
                """
            ),
            {"dt": discord_thread},
        ).fetchone()
        session.commit()
    if row is None:
        return {
            "ok": False,
            "discord_thread": discord_thread,
            "reason": "no idle thread for that discord thread",
        }
    return {
        "ok": True,
        "discord_thread": discord_thread,
        "thread_id": row.thread_id,
        "wake_requested": True,
    }
