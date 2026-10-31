"""Exercise historical-evidence projection on the migrated PostgreSQL schema."""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import event

from factory.execution.admission_diagnostics import _recorded_cessation
from factory.execution.models import ProbeObservation


def _seed(session, raw, identity="a" * 64):
    permit_id = 2**63 - 1
    session.add(
        ProbeObservation(
            permit_id=permit_id,
            identity_sha256=identity,
            evidence_json=raw,
            checked_at=datetime.now(UTC),
        )
    )
    session.flush()
    return permit_id


def test_recorded_cessation_projects_bounded_fields_without_writing(session):
    saved = {
        "session_id": "s-exact-historical-guest",
        "generation": 0,
        "invoke_started_at": 1791352607535,
        "updated_at": 1791356520888,
        "observed_at": "2026-10-07T07:02:32.297952+00:00",
        "response_sha256": "b" * 64,
        "terminal_state": "destroyed",
        "cessation_evidence": "terminal_dispatch",
        "last_invoke_at": None,
    }
    raw = json.dumps({**saved, "private": {"token": "not-for-diagnostics"}})
    permit_id = _seed(session, raw)
    statements = []

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(session.bind, "before_cursor_execute", capture)
    try:
        result = _recorded_cessation(session, permit_id)
        assert _recorded_cessation(session, permit_id - 1) is None
    finally:
        event.remove(session.bind, "before_cursor_execute", capture)
    assert statements and all(
        s.lstrip().upper().startswith("SELECT") for s in statements
    )
    assert result == {"identity_sha256": "a" * 64, **saved}
    assert "not-for-diagnostics" not in json.dumps(result)
    assert session.get(ProbeObservation, permit_id).evidence_json == raw


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not-json",
        "null",
        "[]",
        '{"session_id":"\\u0000"}',
        '{"generation":' + "9" * 5000 + "}",
        json.dumps(
            {
                "session_id": {"secret": "not-for-diagnostics"},
                "observed_at": ["not-for-diagnostics"],
                "response_sha256": "b" * 65,
                "terminal_state": "running",
                "cessation_evidence": "untrusted",
                "generation": True,
                "invoke_started_at": 1.5,
                "updated_at": -1,
                "last_invoke_at": 2**64,
            }
        ),
        json.dumps(
            {
                key: "x" * 257
                for key in (
                    "session_id",
                    "observed_at",
                    "response_sha256",
                    "terminal_state",
                    "cessation_evidence",
                    "generation",
                    "invoke_started_at",
                    "updated_at",
                    "last_invoke_at",
                )
            }
        ),
    ],
)
def test_recorded_cessation_rejects_malformed_postgres_fields(session, raw):
    permit_id = _seed(session, raw, identity="a" * 65)
    result = _recorded_cessation(session, permit_id)
    assert result and all(value is None for value in result.values())


@pytest.mark.parametrize("value", [0, 2**63 - 1])
def test_recorded_cessation_preserves_integer_bounds(session, value):
    permit_id = _seed(session, json.dumps({"generation": value}))
    assert _recorded_cessation(session, permit_id)["generation"] == value
