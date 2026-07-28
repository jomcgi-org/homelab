import os
from functools import lru_cache

from sqlmodel import Session, create_engine

_raw_url = os.environ.get(
    "DATABASE_URL", "postgresql://app:app@localhost:5432/monolith"
)
# CNPG provides postgresql:// but SQLAlchemy needs the driver suffix
# for psycopg v3. Rewrite the scheme to postgresql+psycopg://.
DATABASE_URL = _raw_url.replace("postgresql://", "postgresql+psycopg://", 1)


@lru_cache(maxsize=1)
def get_engine():
    return create_engine(DATABASE_URL)


def get_session():
    with Session(get_engine()) as session:
        yield session
