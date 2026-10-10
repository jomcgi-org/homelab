"""Real campaign rows and case-insensitive wire canaries for route ACL tests.

Each placement has its own token and allowed harness roles in ``canaries``:
campaign name/dm_name belong to that campaign's members; character names,
player names, class names and sheet JSON belong to its DM and assigned player;
sheet-version JSON, derived JSON, comments and attribution emails have that
same restriction. Member ids, grant ids and invitation ids are DM-only
administrative data. Emails belong to their account holder and the DMs managing
that account's membership or invitation. Entity names, source_book/site/detail JSON and
NPC descriptions/race/occupation/disposition follow full grants; partial and
name-only grants expose identity but keep those remaining fields DM-only.
Grant revealed_details follow the grantee. Relationship type/properties follow
both endpoints. Private entity ids also serve as canaries in relationship and
mention responses. Corpus chunk bodies and book metadata are deliberately
unmarked: they are corpus-global, even when mentioning a private entity.
Session ids are visible in full entity spines, so their audience is campaign
members; session mutations are pinned by status and database snapshots.

No caller's claimed viewer is substituted for production authorization. Only
verified identity and the vector-distance primitive are test seams. Registration,
operator groups, membership, object scoping, and projections execute unchanged.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from auth.api import Authority, Principal, PrincipalKind
from core.db import get_session
from fastapi import FastAPI, HTTPException, Request
from knowledge.api import get_embedding_client
from sqlmodel import Session, SQLModel, create_engine, select

import grimoire
from grimoire.access import get_authenticated_identity
from grimoire.models import (
    AppUser,
    Book,
    Campaign,
    CampaignInvitation,
    CampaignMember,
    CharacterSheetVersion,
    ChunkEntityMention,
    Embedding,
    Entity,
    EntityNpc,
    GameSession,
    KnowledgeChunk,
    KnowledgeGrant,
    Note,
    PlayerCharacter,
    Relationship,
    SessionEvent,
)
from grimoire.play_embeddings import audience_columns, event_embedding_kind

ROLES = ("dm", "player_a", "player_b", "no_character", "outsider", "other_campaign")
MEMBERS = frozenset(("dm", "player_a", "player_b", "no_character"))
SHEET_BODY = {
    "schema_version": 1,
    "ancestry": "Human",
    "class_name": "Fighter",
    "level": 1,
    "ability_scores": dict.fromkeys(
        ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"),
        12,
    ),
}


@dataclass
class LeakHarness:
    session: Session
    rows: dict = field(default_factory=dict)
    emails: dict = field(default_factory=dict)
    canaries: dict[str, tuple[frozenset[str], str]] = field(default_factory=dict)

    def token(self, placement: str, allowed, *, identifier=False) -> str:
        """Uppercase wire-safe tokens, including UUID-shaped restricted ids."""
        number = len(self.canaries) + 1
        token = (
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"grimoire-leak:{placement}")).upper()
            if identifier
            else f"CANARY-{number:04d}-SECRET"
        )
        assert token not in self.canaries, placement
        self.canaries[token] = (frozenset(allowed), placement)
        return token

    def headers(self, viewer: str) -> dict:
        return {"X-Test-Auth-Email": self.emails[viewer]}

    def assert_no_leak(self, response, viewer: str) -> None:
        assert viewer in (*ROLES, "operator"), viewer
        body = response.text.casefold()
        for token, (allowed, placement) in self.canaries.items():
            if viewer not in allowed and token.casefold() in body:
                raise AssertionError(
                    f"{response.request.method} {response.request.url}: "
                    f"{viewer} leaked {token} ({placement})"
                )

    def snapshot(self) -> dict[str, list[str]]:
        """All model tables, including pending writes, not just row counts."""
        self.session.flush()
        return {
            key: sorted(
                json.dumps(dict(row), sort_keys=True, default=str)
                for row in self.session.connection().execute(table.select()).mappings()
            )
            for key, table in SQLModel.metadata.tables.items()
        }

    def prepare(self, state: str | None) -> None:
        if state == "bootstrap":
            members = self.session.exec(
                select(CampaignMember).where(
                    CampaignMember.campaign_id == self.rows["campaign"].id,
                    CampaignMember.role == "dm",
                )
            ).all()
            for member in members:
                self.session.delete(member)
        elif state == "submitted":
            self.rows["sheet"].status = "submitted"
            self.rows["sheet"].submitted_at = datetime.now(timezone.utc)
        elif state == "closed":
            self.rows["sheet"].status = "approved"
            self.rows["sheet"].submitted_at = datetime.now(timezone.utc)
            self.rows["sheet"].decided_at = datetime.now(timezone.utc)
            self.rows["sheet"].decided_by_email = self.emails["dm"]
        elif state == "submit":
            self.rows["sheet"].sheet = dict(SHEET_BODY)
        elif state == "play":
            self.rows["campaign_session"].status = "active"
        self.session.commit()

    def app(self) -> FastAPI:
        app = FastAPI()
        grimoire.register(app)
        grimoire.register_public(app)
        app.dependency_overrides[get_session] = lambda: self.session
        app.dependency_overrides[get_embedding_client] = lambda: FakeEmbedClient()

        def identity(request: Request) -> Principal:
            email = request.headers.get("X-Test-Auth-Email", "").strip().lower()
            if email not in self.emails.values():
                raise HTTPException(403, "test identity missing")
            return Principal(
                subject=f"test:{email}",
                actor=(),
                scope=(),
                groups=("operators",) if email == self.emails["operator"] else (),
                email=email,
                kind=PrincipalKind.HUMAN,
                authority=Authority.STANDING,
            )

        app.dependency_overrides[get_authenticated_identity] = identity
        return app


class FakeEmbedClient:
    async def embed(self, text: str) -> list[float]:
        return [0.0] * 1024


@contextmanager
def sqlite_harness(path):
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False}
    )
    schemas = [(table, table.schema) for table in SQLModel.metadata.tables.values()]
    try:
        for table, _ in schemas:
            table.schema = None
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield build_fixture(session)
    finally:
        engine.dispose()
        for table, schema in schemas:
            table.schema = schema


def build_fixture(session: Session) -> LeakHarness:
    h = LeakHarness(session)
    rows = h.rows
    objects = []

    def keep(key, row):
        rows[key] = row
        objects.append(row)
        return row

    def mark(key, allowed, identifier=False):
        return h.token(
            key,
            (*allowed, "operator") if "dm" in allowed else allowed,
            identifier=identifier,
        )

    for role in (*ROLES, "operator", "other_player", "invitee"):
        allowed = (
            ("other_campaign",)
            if role in ("other_campaign", "other_player")
            else ("dm", role)
        )
        if role == "outsider":
            allowed = ("dm", "other_campaign", "outsider")
        email = f"{mark(f'user.{role}.email', allowed)}@example.test".lower()
        h.emails[role] = email
        keep(
            f"user_{role}",
            AppUser(
                email=email,
                issuer="test-issuer" if role in ("invitee", "outsider") else None,
                subject=f"test-{role}" if role in ("invitee", "outsider") else None,
                display_name=mark(f"user.{role}.display_name", allowed),
            ),
        )

    for key, allowed, owner in (
        ("campaign", MEMBERS, "dm"),
        ("other", ("other_campaign",), "other_campaign"),
    ):
        keep(
            key,
            Campaign(
                id=mark(f"{key}.id", allowed, True),
                name=mark(f"{key}.name", allowed),
                dm_name=mark(f"{key}.dm_name", allowed),
                owner_app_user_id=rows[f"user_{owner}"].id,
            ),
        )
        keep(
            f"{key}_session",
            GameSession(
                id=mark(f"{key}.session.id", allowed, True),
                campaign_id=rows[key].id,
                status="ended",
            ),
        )

    for key, role, campaign_key in (
        ("character_a", "player_a", "campaign"),
        ("character", "player_b", "campaign"),
        ("other_character", "other_player", "other"),
    ):
        allowed = ("dm", role) if campaign_key == "campaign" else ("other_campaign",)
        keep(
            key,
            PlayerCharacter(
                id=mark(f"{key}.id", allowed, True),
                campaign_id=rows[campaign_key].id,
                character_name=mark(f"{key}.character_name", allowed),
                player_name=mark(f"{key}.player_name", allowed),
                class_name=mark(f"{key}.class_name", allowed),
                level=1,
                sheet={"secret": mark(f"{key}.sheet", allowed)},
            ),
        )
        for version, status in ((1, "approved"), (2, "draft")):
            sheet_key = (
                "sheet"
                if key == "character" and version == 2
                else f"{key}_sheet{version}"
            )
            keep(
                sheet_key,
                CharacterSheetVersion(
                    id=mark(f"{sheet_key}.id", allowed, True),
                    campaign_id=rows[campaign_key].id,
                    player_character_id=rows[key].id,
                    version=version,
                    status=status,
                    sheet={
                        **SHEET_BODY,
                        "ancestry": mark(f"{sheet_key}.sheet.ancestry", allowed),
                        "class_name": mark(f"{sheet_key}.sheet.class_name", allowed),
                    },
                    derived={"secret": mark(f"{sheet_key}.derived", allowed)},
                    created_by_email=f"{mark(f'{sheet_key}.created_by_email', allowed)}@example.test".lower(),
                    submitted_at=datetime.now(timezone.utc)
                    if status == "approved"
                    else None,
                    decided_at=datetime.now(timezone.utc)
                    if status == "approved"
                    else None,
                    decided_by_email=(
                        f"{mark(f'{sheet_key}.decided_by_email', allowed)}@example.test".lower()
                        if status == "approved"
                        else None
                    ),
                    decision_comment=mark(f"{sheet_key}.decision_comment", allowed)
                    if status == "approved"
                    else None,
                ),
            )

    for role, campaign_key, character_key, member_role in (
        ("dm", "campaign", None, "dm"),
        ("operator", "campaign", None, "dm"),
        ("player_a", "campaign", "character_a", "player"),
        ("player_b", "campaign", "character", "player"),
        ("no_character", "campaign", None, "player"),
        ("other_campaign", "other", None, "dm"),
        ("other_player", "other", "other_character", "player"),
    ):
        allowed = ("dm", role) if campaign_key == "campaign" else ("other_campaign",)
        member_key = "member" if role == "player_b" else f"member_{role}"
        keep(
            member_key,
            CampaignMember(
                id=mark(f"{member_key}.id", allowed, True),
                campaign_id=rows[campaign_key].id,
                app_user_id=rows[f"user_{role}"].id,
                role=member_role,
                player_character_id=rows[character_key].id if character_key else None,
            ),
        )

    for key, kind, readable, deleted, campaign_key, allowed in (
        ("note_private", "character", False, False, "campaign", ("player_a",)),
        ("note_shared", "character", True, False, "campaign", ("player_a", "dm")),
        (
            "note_party",
            "party",
            False,
            False,
            "campaign",
            ("dm", "player_a", "player_b"),
        ),
        ("note_deleted_character", "character", True, True, "campaign", ()),
        ("note_deleted_party", "party", False, True, "campaign", ()),
        ("note_foreign", "party", False, False, "other", ("other_campaign",)),
    ):
        author = (
            "member_player_a" if campaign_key == "campaign" else "member_other_campaign"
        )
        keep(
            key,
            Note(
                id=mark(f"{key}.id", allowed, True),
                campaign_id=rows[campaign_key].id,
                author_member_id=rows[author].id,
                player_character_id=rows["character_a"].id
                if campaign_key == "campaign"
                else None,
                kind=kind,
                dm_readable=readable,
                title=mark(f"{key}.title", allowed),
                markdown=mark(f"{key}.markdown", allowed),
                links={
                    "entity_ids": [],
                    "event_ids": [mark(f"{key}.event_id", allowed, True)],
                },
                created_in_session=rows[f"{campaign_key}_session"].id,
                created_at=datetime(
                    2026, 10, 2, 1, len(h.canaries) % 60, tzinfo=timezone.utc
                ),
                updated_at=datetime(
                    2026, 10, 2, 2, len(h.canaries) % 60, tzinfo=timezone.utc
                ),
                deleted_at=datetime.now(timezone.utc) if deleted else None,
            ),
        )

    for audience, pc_key, allowed in (
        ("dm", None, ("dm",)),
        ("pcs", "character_a", ("dm", "player_a")),
        ("pcs", "character", ("dm", "player_b")),
        ("table", None, MEMBERS),
    ):
        for retracted in (False, True):
            key = f"event_{pc_key or audience}{'_retracted' if retracted else ''}"
            keep(
                key,
                SessionEvent(
                    id=mark(f"{key}.id", allowed, True),
                    campaign_id=rows["campaign"].id,
                    session_id=rows["campaign_session"].id,
                    seq=1 + sum(isinstance(obj, SessionEvent) for obj in objects),
                    kind="narration",
                    author_member_id=rows["member_dm"].id,
                    audience=audience,
                    audience_pc_ids=[rows[pc_key].id] if pc_key else [],
                    body={
                        "secret": mark(f"{key}.body", ("dm",) if retracted else allowed)
                    },
                    retracted_at=datetime.now(timezone.utc) if retracted else None,
                ),
            )

    keep(
        "invitation",
        CampaignInvitation(
            id=mark("invitation.id", ("dm",), True),
            campaign_id=rows["campaign"].id,
            invitee_id=rows["user_invitee"].id,
            invited_by_id=rows["user_dm"].id,
        ),
    )
    keep(
        "other_invitation",
        CampaignInvitation(
            id=mark("other_invitation.id", ("other_campaign",), True),
            campaign_id=rows["other"].id,
            invitee_id=rows["user_outsider"].id,
            invited_by_id=rows["user_other_campaign"].id,
        ),
    )

    for key, allowed, full, campaign_key in (
        ("private", ("dm",), ("dm",), "campaign"),
        ("a_only", ("dm", "player_a"), ("dm", "player_a"), "campaign"),
        ("b_only", ("dm", "player_b"), ("dm", "player_b"), "campaign"),
        ("partial", ("dm", "player_a"), ("dm",), "campaign"),
        ("name_only", ("dm", "player_a"), ("dm",), "campaign"),
        ("foreign", ("other_campaign",), ("other_campaign",), "other"),
    ):
        entity = keep(
            key,
            Entity(
                id=mark(f"{key}.id", allowed, True),
                entity_type="npc",
                name=mark(f"{key}.name", allowed),
                is_global=False,
                source_type="homebrew",
                created_in_session=rows[f"{campaign_key}_session"].id,
                source_book=mark(f"{key}.source_book", full),
                site=mark(f"{key}.site", full),
                detail={"secret": mark(f"{key}.detail", full)},
            ),
        )
        keep(
            f"{key}_detail",
            EntityNpc(
                entity_id=entity.id,
                **{
                    field: mark(f"{key}.{field}", full)
                    for field in ("race", "occupation", "disposition", "description")
                },
            ),
        )
        if key not in ("private", "foreign"):
            character_key = "character" if key == "b_only" else "character_a"
            grant_key = "grant" if key == "b_only" else f"grant_{key}"
            scope = {"partial": "partial", "name_only": "name_only"}.get(key, "full")
            keep(
                grant_key,
                KnowledgeGrant(
                    id=mark(f"{grant_key}.id", ("dm",), True),
                    campaign_id=rows["campaign"].id,
                    entity_id=entity.id,
                    player_character_id=rows[character_key].id,
                    grant_scope=scope,
                    granted_in_session=rows["campaign_session"].id,
                    revealed_details={
                        "secret": mark(
                            f"{grant_key}.revealed_details",
                            ("dm",) if scope == "name_only" else allowed,
                        )
                    },
                ),
            )
        keep(
            f"mention_{key}",
            ChunkEntityMention(chunk_id=str(uuid.uuid4()), entity_id=entity.id),
        )
        keep(
            f"chunk_{key}",
            KnowledgeChunk(
                id=rows[f"mention_{key}"].chunk_id,
                book_id="corpus",
                chunk_ref=key,
                content="Corpus-global source text",
                section_path="Chapter 1",
                seq=len(objects),
            ),
        )
        keep(
            f"embedding_{key}",
            Embedding(
                embeddable_kind="entity",
                embeddable_id=entity.id,
                model="test",
                dim=1024,
                vector=[0.0] * 1024,
            ),
        )
    for key, entity_key, pc_key, scope, allowed in (
        ("reveal_b", "b_only", "character", "full", ("dm", "player_b")),
        ("reveal_partial", "partial", "character_a", "partial", ("dm", "player_a")),
        (
            "reveal_name_only",
            "name_only",
            "character_a",
            "name_only",
            ("dm", "player_a"),
        ),
    ):
        entity = rows[entity_key]
        item = {
            "entity_id": entity.id,
            "name": entity.name,
            "entity_type": entity.entity_type,
            "grant_scope": scope,
            "text": mark(f"{key}.text", ("dm",) if scope == "name_only" else allowed),
        }
        if scope == "partial":
            item["entity"] = {
                "revealed_details": rows["grant_partial"].revealed_details
            }
        elif scope == "full":
            item["entity"] = {"description": item["text"]}
        keep(
            key,
            SessionEvent(
                id=mark(f"{key}.id", allowed, True),
                campaign_id=rows["campaign"].id,
                session_id=rows["campaign_session"].id,
                seq=1 + sum(isinstance(obj, SessionEvent) for obj in objects),
                kind="reveal",
                author_member_id=rows["member_dm"].id,
                audience="pcs",
                audience_pc_ids=[rows[pc_key].id],
                body={"reveals": [item]},
            ),
        )
    keep(
        "event_foreign",
        SessionEvent(
            id=mark("event_foreign.id", ("other_campaign",), True),
            campaign_id=rows["other"].id,
            session_id=rows["other_session"].id,
            seq=1,
            kind="narration",
            author_member_id=rows["member_other_campaign"].id,
            audience="table",
            body={"text": mark("event_foreign.body", ("other_campaign",))},
        ),
    )
    # Seed stale vectors even for deleted notes and retracted events. The fake
    # nearest-neighbor seam ignores all filters to exercise live-source checks.
    for key, row in list(rows.items()):
        if isinstance(row, (Note, SessionEvent)):
            keep(
                f"embedding_{key}",
                Embedding(
                    embeddable_kind="note"
                    if isinstance(row, Note)
                    else event_embedding_kind(row),
                    embeddable_id=row.id,
                    model="harness-play",
                    dim=1024,
                    vector=[0.0] * 1024,
                    **audience_columns(row),
                ),
            )
    keep(
        "embedding_chunk",
        Embedding(
            embeddable_kind="chunk",
            embeddable_id=rows["chunk_private"].id,
            model="test",
            dim=1024,
            vector=[0.0] * 1024,
        ),
    )
    for key, allowed in (
        ("private", ("dm",)),
        ("b_only", ("dm",)),
        ("partial", ("dm", "player_a")),
        ("name_only", ("dm", "player_a")),
        ("foreign", ()),
    ):
        keep(
            f"relationship_{key}",
            Relationship(
                from_entity_id=rows["a_only"].id,
                to_entity_id=rows[key].id,
                rel_type=mark(f"relationship_{key}.rel_type", allowed),
                properties={"secret": mark(f"relationship_{key}.properties", allowed)},
            ),
        )
    keep("book", Book(id="corpus", display_name="Global corpus"))
    keep("ancestry", Entity(entity_type="race", name="Human", is_global=True))
    keep(
        "class",
        Entity(
            entity_type="class",
            name="Fighter",
            is_global=True,
            detail={"hit_die": "d10", "saves": "Strength, Constitution"},
        ),
    )
    session.add_all(objects)
    session.commit()
    return h


def fake_knn(session, query_vector, kinds, limit, model=None, where=None):
    """Replace only pgvector distance: all seeded private candidates compete."""
    return [(row, 0.0) for row in session.exec(select(Embedding)).all()][:limit]
