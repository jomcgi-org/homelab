"""Projection invariants over unfiltered, seeded event streams."""

import json
import random
from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from grimoire.audience import can_see
from grimoire.journal import journal
from grimoire.models import SessionEvent


def uid(label):
    return str(uuid5(NAMESPACE_URL, label))


def member(viewer):
    return SimpleNamespace(
        id=uid(f"member-{viewer}"),
        role="dm" if viewer == "dm" else "player",
        player_character_id=None if viewer in ("dm", None) else viewer,
    )


A, B = uid("pc-a"), uid("pc-b")


def event(seq, **changes):
    values = {
        "id": uid(f"event-{seq}"),
        "campaign_id": uid("campaign"),
        "session_id": uid("session"),
        "seq": seq,
        "kind": "reveal",
        "audience": "pcs",
        "audience_pc_ids": [A],
        "author_member_id": member("dm").id,
        "body": {},
        "retracted_at": None,
    }
    values.update(changes)
    return SessionEvent(**values)


def project(events, viewer=A, grants=None, entities=None, view="mine"):
    return asdict(
        journal(
            viewer,
            member(viewer),
            events,
            current_grants=grants or set(),
            visible_entities=entities or {},
            view=view,
        )
    )


def reveal(entity_id, **changes):
    return dict(
        entity_id=entity_id,
        name="Known person",
        entity_type="npc",
        grant_scope="full",
        entity={"details": "previous-secret"},
        **changes,
    )


def test_retraction_silent_regrant_and_dm_row_retract():
    entity_id = uid("learned")
    first = event(1, body=reveal(entity_id))
    revoked = event(
        2,
        body={
            "entity_id": entity_id,
            "name": "Known person",
            "entity_type": "npc",
            "grant_scope": "full",
            "retracted": True,
            "silent": False,
            "entity": {"details": "must-never-survive"},
        },
    )
    result = project([first, revoked])
    assert result["learned"][0]["retracted"] is True
    assert "entity" not in result["learned"][0]
    assert result["people_and_places"] == []
    assert "previous-secret" not in json.dumps(result)
    assert "must-never-survive" not in json.dumps(result)
    silent = event(2, body={"retracted": True, "silent": True})
    assert project([first, silent])["learned"] == []
    assert project([first, silent])["people_and_places"] == []
    regrant = event(3, body=reveal(entity_id))
    assert (
        project([first, revoked, regrant], grants={(A, entity_id)})["learned"][0]["seq"]
        == 3
    )
    first.retracted_at = datetime.now(timezone.utc)
    for viewer in ("dm", A):
        assert project([first], viewer, {(A, entity_id)})["learned"] == []


def test_latest_per_pc_bulk_and_historical_downgrade():
    entity_id = uid("shared")
    first = event(1, body={"reveals": [reveal(entity_id)]})
    second = event(2, audience_pc_ids=[B], body=reveal(entity_id))
    latest = event(
        3,
        body={
            "entity_id": entity_id,
            "name": "Latest name",
            "entity_type": "npc",
            "grant_scope": "partial",
            "entity": {"revealed_details": {"fact": "latest"}},
        },
    )
    grants = {(A, entity_id), (B, entity_id)}
    result = project([latest, second, first], "dm", grants)
    assert [item["seq"] for item in result["learned"]] == [2, 3]
    assert [item["player_character_id"] for item in result["learned"]] == [B, A]
    assert project([first], grants=grants)["learned"][0]["grant_scope"] == "full"
    assert "player_character_id" not in project([first], grants=grants)["learned"][0]


def test_only_visible_later_reply_closes_private_thread():
    action = event(
        3,
        kind="action",
        author_member_id=member(A).id,
        body={"text": "private question"},
    )
    early = event(2, kind="narration", body={"reply_to": action.id})
    hidden = event(
        4,
        kind="narration",
        audience="dm",
        audience_pc_ids=[],
        body={"reply_to": action.id},
    )
    dead = event(
        5,
        kind="narration",
        body={"reply_to": action.id},
        retracted_at=datetime.now(timezone.utc),
    )
    assert len(project([action, early, hidden, dead])["open_threads"]) == 1
    reply = event(6, kind="narration", body={"reply_to": action.id})
    assert project([action, reply])["open_threads"] == []
    assert project([action], view="party")["open_threads"] == []


def test_narration_only_resolves_explicit_visible_uuid_references():
    known, hidden = uid("known"), uid("hidden")
    narration = event(
        1,
        kind="narration",
        audience="table",
        audience_pc_ids=[],
        body={
            "entity_ids": [known.upper(), hidden, "bad", 42, {}, UUID(known).hex],
            "text": "unrelated name",
        },
    )
    entities = {
        known: {
            "id": known,
            "name": "Projected",
            "entity_type": "npc",
            "details": "never-in-identity",
        }
    }
    assert project([narration], entities=entities)["people_and_places"] == [
        {"id": known, "name": "Projected", "entity_type": "npc"}
    ]


@pytest.mark.parametrize("view", ["mine", "party"])
def test_seeded_unfiltered_streams_never_emit_hidden_or_retracted_canaries(view):
    rng = random.Random(6617)
    kinds = [
        "narration",
        "action",
        "roll",
        "reveal",
        "handout",
        "turn",
        "system",
        "utterance",
    ]
    for iteration in range(120):
        rows, tokens, grants, entities = [], {}, set(), {}
        for seq in range(1, 41):
            kind = rng.choice(kinds)
            audience, pcs = rng.choice(
                [
                    ("table", []),
                    ("dm", []),
                    ("pcs", [A]),
                    ("pcs", [B]),
                    ("pcs", [A, B]),
                ]
            )
            label = f"{iteration}-{seq}"
            entity_id, name, text = (
                uid(f"entity-{label}"),
                f"NAME-{label}-END",
                f"TEXT-{label}-END",
            )
            body = {
                "text": text,
                "entity_ids": [entity_id, "bad", None],
                "reply_to": uid(f"row-{iteration}-{rng.randint(1, 40)}"),
            }
            if kind == "reveal":
                author = member("dm").id
                bodies = [
                    {
                        "entity_id": entity_id,
                        "name": name,
                        "entity_type": "npc",
                        "grant_scope": "full",
                        "entity": {"detail": text},
                    }
                ]
                if rng.randrange(4) == 0:
                    bodies[0].update(retracted=True, silent=False)
                elif rng.randrange(4) == 0:
                    bodies = [{"retracted": True, "silent": True}]
                body = {"reveals": bodies} if rng.choice([False, True]) else bodies[0]
                if not bodies[0].get("retracted"):
                    grants.update((pc, entity_id) for pc in pcs)
            else:
                author = rng.choice([member("dm").id, member(A).id, member(B).id, None])
            row = event(
                seq,
                id=uid(f"row-{label}"),
                kind=kind,
                audience=audience,
                audience_pc_ids=pcs,
                author_member_id=author,
                body=body,
                retracted_at=datetime.now(timezone.utc)
                if rng.randrange(5) == 0
                else None,
            )
            rows.append(row)
            tokens[row.id] = (entity_id, name, text)
            entities[entity_id] = {"id": entity_id, "name": name, "entity_type": "npc"}
        for viewer in ("dm", A, B, None):
            serialized = json.dumps(project(rows, viewer, grants, entities, view))
            for row in rows:
                if (
                    not can_see(viewer, member(viewer), row)
                    or row.retracted_at is not None
                    or (view == "party" and row.audience != "table")
                ):
                    for token in tokens[row.id]:
                        assert token not in serialized, (iteration, viewer, view, token)
            if viewer != "dm":
                for other in (A, B):
                    if other != viewer:
                        assert other not in serialized
                        assert member(other).id not in serialized
                assert member("dm").id not in serialized
            shuffled = list(rows)
            rng.shuffle(shuffled)
            assert project(rows, viewer, grants, entities, view) == project(
                shuffled, viewer, grants, entities, view
            )
