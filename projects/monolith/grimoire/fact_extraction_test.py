"""The campaign lens sees authorized input only and queues immutable identities."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from grimoire.fact_extraction import (
    EXTRACTION_VERSION,
    JOB_KIND,
    FactExtractionInvalid,
    build_fact_prompt,
    enqueue_session_facts,
    parse_facts,
)
from grimoire.models import SessionEvent
from grimoire.testing.leak_harness import sqlite_harness
from grimoire.testing.sql_capture import assert_no_knowledge_sql, capture_sql


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    with sqlite_harness(tmp_path / "fact-lens.db") as h:
        yield h


@pytest.mark.parametrize("viewer", ["character_a", "party"])
def test_rendered_prompt_has_no_dm_other_pc_ooc_or_retracted_canaries(harness, viewer):
    h = harness
    rows = []
    forbidden = []
    for index, (audience, pc, body, retracted, allowed) in enumerate(
        (
            ("table", None, {}, False, ("dm", "player_a", "player_b")),
            ("dm", None, {}, False, ("dm",)),
            ("pcs", "character", {}, False, ("dm", "player_b")),
            ("table", None, {"ooc": True}, False, ()),
            ("table", None, {"redacted": True}, False, ()),
            ("table", None, {}, True, ()),
        ),
        200,
    ):
        token = h.token(f"fact-lens.utterance.{index}", allowed)
        if index != 200:
            forbidden.append(token)
        rows.append(
            SessionEvent(
                campaign_id=h.rows["campaign"].id,
                session_id=h.rows["campaign_session"].id,
                seq=index,
                kind="utterance",
                audience=audience,
                audience_pc_ids=[h.rows[pc].id] if pc else [],
                body={"text": token, **body},
                retracted_at=datetime.now(UTC) if retracted else None,
            )
        )
    h.session.add_all(rows)
    h.session.commit()
    viewer_key = "party" if viewer == "party" else h.rows[viewer].id
    prompt = build_fact_prompt(
        h.session, h.rows["campaign"].id, h.rows["campaign_session"].id, viewer_key
    )
    response = httpx.Response(
        200, request=httpx.Request("GET", "https://test/prompt"), text=prompt
    )
    h.assert_no_leak(response, "player_a")
    if viewer == "party":
        h.assert_no_leak(response, "player_b")
    else:
        assert h.rows["note_private"].markdown in prompt
        assert h.rows["event_character_a"].id in prompt
    assert rows[0].body["text"] in prompt
    assert EXTRACTION_VERSION in prompt
    for token in forbidden:
        assert token not in prompt
    payload = json.loads(prompt.split("Viewer-visible payload:\n", 1)[1])
    assert rows[0].id in payload["evidence_event_ids"]
    assert all(row.id not in payload["evidence_event_ids"] for row in rows[3:])


@pytest.mark.parametrize(
    "wrapper", ["{}", "before ```json\n{}\n``` after", "before {} after"]
)
def test_parser_accepts_strict_facts_and_fenced_json(wrapper):
    value = {
        "facts": [
            {
                "statement": "A {brace} was learned",
                "evidence_event_ids": ["id"],
                "entity_id": None,
                "confidence": 0,
            }
        ]
    }
    assert parse_facts(wrapper.format(json.dumps(value)))[0].confidence == 0
    assert parse_facts('{"facts": []}') == []


@pytest.mark.parametrize(
    "output",
    [
        "not JSON",
        '{"oops": {"facts": []}',
        '{"facts": [], "extra": 1}',
        '{"facts": "none"}',
        "{}",
        '{"facts": [{"statement": "x"}]}',
        *[
            json.dumps(
                {
                    "facts": [
                        {
                            "statement": "x",
                            "evidence_event_ids": ["id"],
                            "entity_id": None,
                            "confidence": value,
                        }
                    ]
                }
            )
            for value in (True, "0.8", -0.1, 1.1, float("nan"), float("inf"))
        ],
    ],
)
def test_malformed_output_fails_cleanly(output):
    with pytest.raises(FactExtractionInvalid):
        parse_facts(output)


def test_parser_uses_last_fenced_result_not_earlier_example():
    assert (
        parse_facts(
            'Example ```json {"facts": "bad"}```\nResult ```json {"facts": []}```'
        )
        == []
    )


def test_session_end_enqueues_each_viewer_once_even_when_cap_off(harness, monkeypatch):
    h = harness
    monkeypatch.setenv("DRAINER_GRIMOIRE_KG_MAX_JOBS_PER_DAY", "0")
    url = f"/api/grimoire/campaigns/{h.rows['campaign'].id}/sessions/{h.rows['campaign_session'].id}"
    with capture_sql(h.session.get_bind()) as statements, TestClient(h.app()) as client:
        for _ in range(2):
            response = client.patch(
                url,
                json={"status": "ended"},
                headers={"X-Test-Auth-Email": h.emails["dm"]},
            )
            assert response.status_code == 200
    assert_no_knowledge_sql(statements)
    rows = h.session.execute(
        text("SELECT name,routine_kind,payload FROM routine_jobs")
    ).all()
    viewers = {h.rows["character_a"].id, h.rows["character"].id, "party"}
    assert len(rows) == len(viewers)
    for name, kind, payload in rows:
        payload = json.loads(payload)
        assert kind == JOB_KIND
        assert payload == {
            "campaign_id": h.rows["campaign"].id,
            "session_id": h.rows["campaign_session"].id,
            "viewer_key": payload["viewer_key"],
        }
        assert payload["viewer_key"] in viewers
        assert name == f"{JOB_KIND}:{payload['session_id']}:{payload['viewer_key']}"


def test_enqueue_belongs_to_session_end_transaction(harness):
    h = harness
    assert (
        enqueue_session_facts(
            h.session, h.rows["campaign"].id, h.rows["campaign_session"].id
        )
        == 3
    )
    h.session.rollback()
    assert (
        h.session.execute(text("SELECT count(*) FROM routine_jobs")).scalar_one() == 0
    )
