import os
from functools import lru_cache
from urllib.parse import urlsplit

from sqlmodel import Session, create_engine

_raw_url = os.environ.get(
    "DATABASE_URL", "postgresql://app:app@localhost:5432/monolith"
)
# CNPG provides postgresql:// but SQLAlchemy needs the driver suffix
# for psycopg v3. Rewrite the scheme to postgresql+psycopg://.
DATABASE_URL = _raw_url.replace("postgresql://", "postgresql+psycopg://", 1)


def require_database_identity(expected_service: str, expected_username: str) -> None:
    """Fail closed when the injected database URI crosses its declared boundary.

    CNPG credentials are synced as a full URI, so the chart cannot assemble or
    inspect the hostname without handling the password. The public entrypoint
    calls this with non-secret expectations before it builds the app. Matching
    the first DNS label accepts short and fully qualified Kubernetes service
    names while still rejecting the primary ``-rw`` endpoint.
    """
    parsed = urlsplit(_raw_url)
    hostname = parsed.hostname or ""
    service = hostname.split(".", 1)[0]
    if parsed.username != expected_username or service != expected_service:
        raise RuntimeError(
            "DATABASE_URL must use the declared database service and role"
        )


@lru_cache(maxsize=1)
def get_engine():
    return create_engine(DATABASE_URL)


def get_session():
    with Session(get_engine()) as session:
        yield session
