"""Write-path database engine for grimoire_chat (ADR security/005 posture).

The public binary's default engine (``core.db``) connects as the read-only
``public_reader`` role to the read replica ``monolith-pg-ro``. Session and
transcript WRITES must NOT go through it. This module owns a SECOND engine bound
to ``PUBLIC_WRITER_DATABASE_URL``, which points at the Postgres PRIMARY
(``monolith-pg-rw``) as the dedicated ``public_writer`` role. That role holds DML
on the public-tier write schemas (chat_public AND now grimoire_chat) and nothing
else, so keeping the two engines separate is the database half of the read/write
split: a bug in grimoire_chat cannot turn the public read tier into a writer, and
the chat write role cannot read any private schema.

It is a verbatim copy of ``chat_public/db.py`` (same public_writer engine, same
``PUBLIC_WRITER_DATABASE_URL`` env var): both public chat surfaces share the one
write role, which now has grants on both schemas. It is self-contained (no import
of any private write path) so it stays out of the forbidden import closure
asserted by ``app/main_public_imports_test.py``.
"""

from __future__ import annotations

import os
from functools import lru_cache

from sqlmodel import Session, create_engine

# CNPG hands out postgresql://; SQLAlchemy needs the psycopg v3 driver suffix.
# Rewrite the scheme to postgresql+psycopg:// (same as core.db). The default is a
# local dev URL; production injects the real primary + public_writer credentials.
_raw_url = os.environ.get(
    "PUBLIC_WRITER_DATABASE_URL",
    "postgresql://public_writer:public_writer@localhost:5432/monolith",
)
PUBLIC_WRITER_DATABASE_URL = _raw_url.replace(
    "postgresql://", "postgresql+psycopg://", 1
)


@lru_cache(maxsize=1)
def get_chat_engine():
    return create_engine(PUBLIC_WRITER_DATABASE_URL)


def get_chat_session():
    """FastAPI dependency: a Session bound to the grimoire_chat write engine."""
    with Session(get_chat_engine()) as session:
        yield session
