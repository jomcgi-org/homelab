"""Ships partition maintenance and retention.

Stub: Task 6 implements rolling daily-partition creation and drop-partition
retention for ships.positions. For now this is a no-op so the scheduled job
registers and the app starts cleanly.
"""

import logging
from datetime import datetime

from sqlmodel import Session

logger = logging.getLogger("ships")


async def partition_maintenance_handler(session: Session) -> datetime | None:
    """No-op placeholder. Task 6 implements create-ahead + drop-old partitions."""
    return None
