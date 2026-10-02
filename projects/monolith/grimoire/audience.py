"""One audience contract for play rows, independent of HTTP and ORM models.

Membership and campaign scoping remain the caller's responsibility. These
helpers accept only an already-authorized member and their matching viewer.
Future play tables should use the exported column types with non-null audience
and PC-id columns and a nullable author-member column.
"""

from dataclasses import dataclass, field
from typing import Literal, Protocol

from sqlalchemy import JSON, Boolean, String, and_, false, literal, or_, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement

Viewer = str | None
AudienceKind = Literal["table", "dm", "pcs"]

AUDIENCE_TYPE = String()
AUDIENCE_PC_IDS_TYPE = JSONB().with_variant(JSON(), "sqlite")
AUTHOR_MEMBER_ID_TYPE = PG_UUID(as_uuid=False).with_variant(String(36), "sqlite")
_KINDS = ("table", "dm", "pcs")


class Member(Protocol):
    id: str | None
    role: str
    player_character_id: str | None


class AudienceRow(Protocol):
    audience: str
    audience_pc_ids: list[str]
    author_member_id: str | None


@dataclass(frozen=True)
class Audience:
    """Validated immutable audience, including optional author provenance."""

    kind: AudienceKind
    pc_ids: frozenset[str] = field(default_factory=frozenset)
    author_member_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "pc_ids", frozenset(self.pc_ids))
        if self.kind not in _KINDS:
            raise ValueError("Unknown audience")
        if any(not isinstance(pc_id, str) or not pc_id for pc_id in self.pc_ids):
            raise ValueError("PC ids must be non-empty strings")
        if self.kind == "pcs" and not self.pc_ids:
            raise ValueError("pcs audience requires at least one PC id")
        if self.kind != "pcs" and self.pc_ids:
            raise ValueError("Only pcs audience may carry PC ids")

    def to_columns(self) -> dict:
        """Return the three storage columns, sorting and deduplicating PC ids."""
        return {
            "audience": self.kind,
            "audience_pc_ids": sorted(self.pc_ids),
            "author_member_id": self.author_member_id,
        }

    @classmethod
    def from_columns(
        cls,
        audience: AudienceKind,
        audience_pc_ids: list[str],
        author_member_id: str | None = None,
    ) -> "Audience":
        return cls(audience, frozenset(audience_pc_ids), author_member_id)


def _validate_viewer(viewer: Viewer, member: Member | None) -> Member:
    if member is None:
        raise ValueError("Audience requires a campaign member")
    if member.role not in ("dm", "player") or member.id is None:
        raise ValueError("Audience requires a persisted campaign member")
    expected = "dm" if member.role == "dm" else member.player_character_id
    if viewer != expected or (viewer == "dm" and member.role != "dm"):
        raise ValueError("Viewer does not match the campaign member")
    return member


def note_predicate(table, viewer: Viewer, member: Member | None):
    """Notes have an opt-in DM audience. Compose this with campaign scoping."""
    member = _validate_viewer(viewer, member)
    columns = table.c if hasattr(table, "c") else table
    authored = columns.author_member_id == member.id
    character = and_(
        columns.kind == "character",
        columns.dm_readable.is_(True) if viewer == "dm" else authored,
    )
    party = columns.kind == "party" if viewer is not None else false()
    return and_(columns.deleted_at.is_(None), or_(character, party))


def can_see_note(viewer: Viewer, member: Member | None, row) -> bool:
    """Python twin of note_predicate, including NULL authors and unknown kinds."""
    member = _validate_viewer(viewer, member)
    if row.deleted_at is not None:
        return False
    if row.kind == "character":
        return (
            bool(row.dm_readable)
            if viewer == "dm"
            else row.author_member_id == member.id
        )
    if row.kind == "party":
        return viewer is not None
    return False


class _HasPC(FunctionElement):
    """Exact JSON array membership, never serialized-text substring matching."""

    type = Boolean()
    inherit_cache = True


@compiles(_HasPC, "postgresql")
def _has_pc_postgresql(element, compiler, **kw):
    ids, pc_id = element.clauses
    return (
        "EXISTS (SELECT 1 FROM jsonb_array_elements_text("
        f"{compiler.process(ids, **kw)}) AS audience_pc(value) "
        f"WHERE audience_pc.value = {compiler.process(pc_id, **kw)})"
    )


@compiles(_HasPC, "sqlite")
def _has_pc_sqlite(element, compiler, **kw):
    ids, pc_id = element.clauses
    return (
        f"EXISTS (SELECT 1 FROM json_each({compiler.process(ids, **kw)}) "
        f"AS audience_pc WHERE audience_pc.value = {compiler.process(pc_id, **kw)})"
    )


def audience_predicate(table, viewer: Viewer, member: Member | None):
    """Build the audience clause for an ORM model, Core table, or table alias.

    The table is explicit because no play table exists yet: the same contract
    must apply to every future table carrying the three audience columns.
    DMs see all rows. Players with characters see table, their pcs, or their
    authored rows, but unknown audience kinds fail closed. A characterless
    member sees table ONLY, even when they authored a restricted row.
    Non-members and inconsistent viewer/member pairs raise instead of guessing.
    This clause is not a campaign filter and must be composed with one.
    """
    member = _validate_viewer(viewer, member)
    if viewer == "dm":
        return true()
    columns = table.c if hasattr(table, "c") else table
    public = columns.audience == "table"
    if viewer is None:
        return public
    return and_(
        columns.audience.in_(_KINDS),
        or_(
            public,
            and_(
                columns.audience == "pcs",
                _HasPC(columns.audience_pc_ids, literal(viewer, type_=String())),
            ),
            columns.author_member_id == member.id,
        ),
    )


def can_see(viewer: Viewer, member: Member | None, row: AudienceRow) -> bool:
    """Python equivalent of audience_predicate, including its raise conditions."""
    member = _validate_viewer(viewer, member)
    if viewer == "dm":
        return True
    if row.audience not in _KINDS:
        return False
    if row.audience == "table":
        return True
    if viewer is None:
        return False
    return (
        row.audience == "pcs" and viewer in row.audience_pc_ids
    ) or row.author_member_id == member.id
