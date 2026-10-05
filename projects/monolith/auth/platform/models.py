"""Private platform account storage, independent of application membership."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Column, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlmodel import Field, SQLModel

SCHEMA = "platform_auth"
UUID_TYPE = UUID(as_uuid=False).with_variant(String(36), "sqlite")


def now():
    return datetime.now(timezone.utc)


def primary_key():
    return Field(
        default_factory=lambda: str(uuid4()),
        sa_column=Column(UUID_TYPE, primary_key=True),
    )


def user_key():
    return Field(
        sa_column=Column(UUID_TYPE, ForeignKey(f"{SCHEMA}.user.id"), nullable=False)
    )


def timestamp():
    return Field(
        default_factory=now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class PlatformUser(SQLModel, table=True):
    __tablename__ = "user"
    __table_args__ = (UniqueConstraint("username"), {"schema": SCHEMA})
    id: str = primary_key()
    username: str
    email: str | None = None
    display_name: str
    active: bool = True
    created_at: datetime = timestamp()


class PlatformIdentity(SQLModel, table=True):
    __tablename__ = "identity"
    __table_args__ = (UniqueConstraint("issuer", "subject"), {"schema": SCHEMA})
    id: str = primary_key()
    user_id: str = user_key()
    issuer: str
    subject: str


class PlatformApplicationUser(SQLModel, table=True):
    __tablename__ = "application_user"
    __table_args__ = (
        UniqueConstraint("application", "application_user_id"),
        UniqueConstraint("application", "user_id"),
        {"schema": SCHEMA},
    )
    id: str = primary_key()
    user_id: str = user_key()
    application: str
    application_user_id: str


class PlatformInvitation(SQLModel, table=True):
    __tablename__ = "invitation"
    __table_args__ = (UniqueConstraint("token_digest"), {"schema": SCHEMA})
    id: str = primary_key()
    recipient_label: str
    issued_by: str
    status: str = "awaiting_delivery"
    token_digest: str | None = Field(default=None, repr=False)
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    created_at: datetime = timestamp()
    accepted_user_id: str | None = Field(
        default=None, sa_column=Column(UUID_TYPE, ForeignKey(f"{SCHEMA}.user.id"))
    )
    accepted_issuer: str | None = None
    accepted_subject: str | None = None
    identity_activated: bool = False


class PlatformGrant(SQLModel, table=True):
    __tablename__ = "grant"
    __table_args__ = (UniqueConstraint("user_id", "permission"), {"schema": SCHEMA})
    id: str = primary_key()
    user_id: str = user_key()
    permission: str
    issued_by: str
    created_at: datetime = timestamp()


class PlatformCommand(SQLModel, table=True):
    __tablename__ = "command"
    __table_args__ = (UniqueConstraint("actor", "request_id"), {"schema": SCHEMA})
    id: str = primary_key()
    actor: str
    request_id: str
    fingerprint: str
    result_json: str


class PlatformAudit(SQLModel, table=True):
    __tablename__ = "audit"
    __table_args__ = {"schema": SCHEMA}
    id: str = primary_key()
    issuer: str
    subject: str
    action: str
    target: str
    request_id: str
    reason: str
    created_at: datetime = timestamp()
