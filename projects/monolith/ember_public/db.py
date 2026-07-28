"""Write-path database engine for the demo-postgres savings accrual.

Mirrors chat_public/db.py: on the public tier, accrual writes to
demo_pg_savings must go through the public_writer role on the primary, not
the default core.db engine (which is public_reader on the read replica and
cannot write). PUBLIC_WRITER_DATABASE_URL is set only on the public profile;
the private tier falls back to core.db.get_engine(), unchanged from before
this module existed.
"""

from __future__ import annotations

import os
from functools import lru_cache

from sqlmodel import create_engine

from core.db import get_engine as get_default_engine


@lru_cache(maxsize=1)
def get_savings_engine():
    """The engine accrual writes should use: public_writer when configured
    (public tier), else the default app engine (private tier)."""
    raw_url = os.environ.get("PUBLIC_WRITER_DATABASE_URL", "")
    if not raw_url:
        return get_default_engine()
    url = raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url)
