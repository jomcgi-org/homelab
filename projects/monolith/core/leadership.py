"""Postgres heartbeat leader election for the monolith's in-process singletons.

Only one replica should run the background singletons (Discord bot, AIS ingest,
scheduler loop) at a time, so the web tier can scale horizontally without N
duplicate bots/streams. A single lease row in ``scheduler.leader_lease`` is
renewed by the leader every ``RENEW_INTERVAL`` seconds; any replica may steal it
once ``heartbeat_at`` is older than ``LEASE_TTL`` (i.e. ~LEASE_TTL of missed
heartbeats). It is one atomic upsert per replica per interval, so the load on
Postgres is negligible.

Fail-safe: any error resolving leadership is treated as "not leader", so a
partitioned or DB-starved replica relinquishes its singletons rather than risk
two leaders running two Discord bots.
"""

from __future__ import annotations

import asyncio
import logging
import platform
from collections.abc import Awaitable, Callable

from sqlmodel import Session, text

from core.db import get_engine

logger = logging.getLogger("monolith.leadership")

LEASE_KEY = "singletons"
RENEW_INTERVAL = 2.0  # seconds between heartbeats
LEASE_TTL = 5.0  # seconds of missed heartbeats before the lease can be stolen

# platform.node() is the pod name under Kubernetes (matches scheduler._HOSTNAME).
HOLDER_ID = platform.node()

# Acquire, renew, or steal-if-stale in one atomic statement. The ON CONFLICT
# UPDATE only fires when we already hold the lease (renew) or the current holder
# has gone stale (steal); otherwise zero rows change and RETURNING is empty, so
# we are a follower. The row lock on the conflicting key serialises racing
# replicas, so exactly one wins a contested steal.
_ACQUIRE_SQL = """
    INSERT INTO scheduler.leader_lease (lease_key, holder, heartbeat_at)
    VALUES (:key, :holder, now())
    ON CONFLICT (lease_key) DO UPDATE
        SET holder = excluded.holder, heartbeat_at = now()
    WHERE scheduler.leader_lease.holder = :holder
       OR scheduler.leader_lease.heartbeat_at < now() - make_interval(secs => :ttl)
    RETURNING holder
"""

_RELEASE_SQL = (
    "DELETE FROM scheduler.leader_lease WHERE lease_key = :key AND holder = :holder"
)


def _acquire_or_renew(key: str = LEASE_KEY) -> bool:
    """Atomically acquire/renew/steal the lease. True if this replica holds it."""
    with Session(get_engine()) as session:
        row = session.execute(
            text(_ACQUIRE_SQL),
            {"key": key, "holder": HOLDER_ID, "ttl": LEASE_TTL},
        ).fetchone()
        session.commit()
    return row is not None and row[0] == HOLDER_ID


def _release(key: str = LEASE_KEY) -> None:
    """Drop our lease on graceful shutdown so failover is immediate, not TTL-bound."""
    with Session(get_engine()) as session:
        session.execute(text(_RELEASE_SQL), {"key": key, "holder": HOLDER_ID})
        session.commit()


class LeaderElector:
    """Drives leadership transitions, invoking callbacks on acquire/resign.

    ``lease_key`` scopes the election: differently-composed binaries (the
    confined monolith vs a standalone domain image) must not contend for the
    same lease, or whichever process wins silently benches the other's
    singletons. The confined monolith keeps the historical "singletons" key;
    domain profiles get a per-domain key (see framework.domain_profile).
    """

    def __init__(self, lease_key: str = LEASE_KEY) -> None:
        self._is_leader = False
        self._lease_key = lease_key

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    async def run(
        self,
        on_acquire: Callable[[], Awaitable[None]],
        on_resign: Callable[[], Awaitable[None]],
    ) -> None:
        """Loop forever, firing on_acquire/on_resign as leadership changes.

        Runs on every replica; only the one holding the lease invokes on_acquire.
        Cancel the task to stop (the lease is released if held).
        """
        logger.info("leader election started as %s", HOLDER_ID)
        try:
            while True:
                try:
                    leader = await asyncio.to_thread(_acquire_or_renew, self._lease_key)
                except Exception:
                    logger.exception("leader lease check failed; treating as follower")
                    leader = False

                if leader and not self._is_leader:
                    self._is_leader = True
                    logger.info("became leader (%s)", HOLDER_ID)
                    try:
                        await on_acquire()
                    except Exception:
                        # A singleton hook is application code, and a failure
                        # there must not kill this election task.  Resign the
                        # partially acquired state before retrying so the next
                        # attempt runs the complete startup sequence again.
                        self._is_leader = False
                        logger.exception(
                            "leader singleton startup failed; retrying election"
                        )
                        try:
                            await on_resign()
                        except Exception:
                            logger.exception(
                                "leader singleton cleanup after startup failure "
                                "failed"
                            )
                        try:
                            await asyncio.to_thread(_release, self._lease_key)
                        except Exception:
                            logger.exception(
                                "leader lease release after startup failure failed"
                            )
                elif not leader and self._is_leader:
                    self._is_leader = False
                    logger.warning(
                        "lost leadership (%s); stopping singletons", HOLDER_ID
                    )
                    await on_resign()

                await asyncio.sleep(RENEW_INTERVAL)
        except asyncio.CancelledError:
            if self._is_leader:
                try:
                    await asyncio.to_thread(_release, self._lease_key)
                    logger.info("released leader lease on shutdown")
                except Exception:
                    logger.exception("leader lease release failed")
            raise
