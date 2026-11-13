"""Direct SQL exercises sheet history guards installed by real migrations."""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError


TABLE = "grimoire.character_sheet_version"
IDENTITY_ERROR = "character sheet version identity is immutable"
TRANSITION_ERROR = "invalid character sheet transition"
SUBMIT_ERROR = "submit cannot alter character sheet content"
CONTENT_ERROR = "submitted character sheet content is immutable"
DECIDED_ERROR = "decided character sheet versions are immutable"
DELETE_ERROR = "submitted character sheet history cannot be deleted"


@pytest.fixture
def sheets(pg):
    # pg applies every chart migration, rather than SQLModel.create_all.
    engine = create_engine(pg.url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
                connection.exec_driver_sql("SET LOCAL statement_timeout = '15s'")
                ids = {
                    key: str(uuid4())
                    for key in (
                        "campaign",
                        "other_campaign",
                        "pc",
                        "other_pc",
                        "id",
                        "other_id",
                    )
                }
                for campaign in (ids["campaign"], ids["other_campaign"]):
                    connection.execute(
                        text(
                            "INSERT INTO grimoire.campaign (id, name) VALUES (:id, 'Sheet history')"
                        ),
                        {"id": campaign},
                    )
                for pc, campaign in (
                    (ids["pc"], ids["campaign"]),
                    (ids["other_pc"], ids["other_campaign"]),
                ):
                    connection.execute(
                        text(
                            "INSERT INTO grimoire.player_character (id, campaign_id, character_name) VALUES (:id, :campaign, 'PC')"
                        ),
                        {"id": pc, "campaign": campaign},
                    )
                yield connection, ids
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def _metadata(status):
    # Every target state has valid CHECK metadata, even forbidden transitions.
    return {
        "draft": "submitted_at = NULL, decided_at = NULL, decided_by_email = NULL, decision_comment = NULL",
        "submitted": "submitted_at = '2026-11-13T10:00:00Z', decided_at = NULL, decided_by_email = NULL, decision_comment = NULL",
        "approved": "submitted_at = '2026-11-13T10:00:00Z', decided_at = '2026-11-13T11:00:00Z', decided_by_email = 'dm@example.test', decision_comment = 'Approved'",
        "returned": "submitted_at = '2026-11-13T10:00:00Z', decided_at = '2026-11-13T11:00:00Z', decided_by_email = 'dm@example.test', decision_comment = 'Please revise'",
    }[status]


def _row(connection, row_id):
    return (
        connection.execute(
            text(f"SELECT * FROM {TABLE} WHERE id = :id"), {"id": row_id}
        )
        .mappings()
        .one_or_none()
    )


def _seed(sheets, status):
    connection, ids = sheets
    # INSERT fixtures use valid state metadata without depending on UPDATE.
    connection.execute(
        text(
            f"INSERT INTO {TABLE} (id, campaign_id, player_character_id, version, sheet, derived, created_by_email, status, submitted_at, decided_at, decided_by_email, decision_comment) VALUES (:id, :campaign, :pc, 1, '{{}}', '{{}}', 'player@example.test', :status, :submitted, :decided, :reviewer, :comment)"
        ),
        {
            **ids,
            "status": status,
            "submitted": "2026-11-13T10:00:00Z" if status != "draft" else None,
            "decided": "2026-11-13T11:00:00Z"
            if status in ("approved", "returned")
            else None,
            "reviewer": "dm@example.test"
            if status in ("approved", "returned")
            else None,
            "comment": "Please revise" if status == "returned" else None,
        },
    )
    return _row(connection, ids["id"])


def _refuse(sheets, sql, message):
    connection, ids = sheets
    before = dict(_row(connection, ids["id"]))
    # The nested context rolls back the failed statement before comparison.
    with pytest.raises(DBAPIError) as refused, connection.begin_nested():
        connection.execute(text(sql), ids)
    assert refused.value.orig.sqlstate == "P0001"
    assert refused.value.orig.diag.message_primary == message
    assert _row(connection, ids["id"]) == before
    # A failed identity UPDATE must not move the row to a different primary key.
    assert _row(connection, ids["other_id"]) is None


def test_draft_edits_and_deletion_persist(sheets):
    connection, ids = sheets
    before = _seed(sheets, "draft")
    connection.execute(
        text(
            f"UPDATE {TABLE} SET sheet = '{{\"name\": \"Mara\"}}', derived = '{{\"hp\": 12}}', updated_at = '2026-11-13T09:00:00Z' WHERE id = :id"
        ),
        ids,
    )
    after = _row(connection, ids["id"])
    assert after.sheet == {"name": "Mara"}
    assert after.derived == {"hp": 12}
    assert after.updated_at.isoformat() == "2026-11-13T09:00:00+00:00"
    assert after.status == "draft" and after.created_at == before.created_at
    connection.execute(text(f"DELETE FROM {TABLE} WHERE id = :id"), ids)
    assert _row(connection, ids["id"]) is None


@pytest.mark.parametrize("decision", ["approved", "returned"])
def test_submission_and_decision_persist(sheets, decision):
    connection, ids = sheets
    before = dict(_seed(sheets, "draft"))
    connection.execute(
        text(
            f"UPDATE {TABLE} SET status = 'submitted', {_metadata('submitted')} WHERE id = :id"
        ),
        ids,
    )
    submitted = dict(_row(connection, ids["id"]))
    expected = {
        **before,
        "status": "submitted",
        "submitted_at": submitted["submitted_at"],
    }
    assert submitted == expected
    assert submitted["submitted_at"].isoformat() == "2026-11-13T10:00:00+00:00"
    connection.execute(
        text(
            f"UPDATE {TABLE} SET status = '{decision}', {_metadata(decision)} WHERE id = :id"
        ),
        ids,
    )
    after = dict(_row(connection, ids["id"]))
    assert after == {
        **submitted,
        "status": decision,
        "decided_at": after["decided_at"],
        "decided_by_email": "dm@example.test",
        "decision_comment": "Approved" if decision == "approved" else "Please revise",
    }
    assert after["decided_at"].isoformat() == "2026-11-13T11:00:00+00:00"


@pytest.mark.parametrize("status", ["draft", "submitted"])
@pytest.mark.parametrize(
    "assignment",
    [
        "id = :other_id",
        "campaign_id = :other_campaign",
        "player_character_id = :other_pc",
        "version = 2",
        "contract_version = 2",
        "created_by_email = 'other@example.test'",
        "created_at = created_at + interval '1 second'",
    ],
)
def test_identity_is_immutable(sheets, status, assignment):
    _seed(sheets, status)
    # contract_version = 2 also violates a CHECK, but the BEFORE guard must
    # win and report the identity exception, not an IntegrityError.
    _refuse(sheets, f"UPDATE {TABLE} SET {assignment} WHERE id = :id", IDENTITY_ERROR)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("draft", "approved"),
        ("draft", "returned"),
        ("draft", "unknown"),
        ("submitted", "draft"),
        ("submitted", "submitted"),
        ("submitted", "unknown"),
    ],
)
def test_invalid_transitions(sheets, source, target):
    _seed(sheets, source)
    metadata = _metadata(target if target != "unknown" else source)
    _refuse(
        sheets,
        f"UPDATE {TABLE} SET status = '{target}', {metadata} WHERE id = :id",
        TRANSITION_ERROR,
    )


@pytest.mark.parametrize("field", ["sheet", "derived"])
def test_submission_cannot_change_content(sheets, field):
    _seed(sheets, "draft")
    _refuse(
        sheets,
        f"UPDATE {TABLE} SET status = 'submitted', {_metadata('submitted')}, {field} = '{{\"changed\": true}}' WHERE id = :id",
        SUBMIT_ERROR,
    )


@pytest.mark.parametrize("decision", ["approved", "returned"])
@pytest.mark.parametrize(
    "assignment",
    [
        "sheet = '{\"changed\": true}'",
        "derived = '{\"changed\": true}'",
        "submitted_at = submitted_at + interval '1 second'",
    ],
)
def test_decision_cannot_change_submitted_content(sheets, decision, assignment):
    _seed(sheets, "submitted")
    # Set decision fields separately so submitted_at has only one assignment.
    metadata = _metadata(decision).split(", ", 1)[1]
    _refuse(
        sheets,
        f"UPDATE {TABLE} SET status = '{decision}', {metadata}, {assignment} WHERE id = :id",
        CONTENT_ERROR,
    )


@pytest.mark.parametrize("status", ["approved", "returned"])
@pytest.mark.parametrize(
    "assignment",
    [
        "sheet = '{\"changed\": true}'",
        "derived = '{\"changed\": true}'",
        "submitted_at = submitted_at + interval '1 second'",
        "decision_comment = 'Rewritten'",
        "decided_by_email = 'other@example.test'",
        "decided_at = decided_at + interval '1 second'",
        "updated_at = updated_at + interval '1 second'",
        "id = :other_id",
        "status = status",
        "sheet = sheet",
        "status = 'submitted', decided_at = NULL, decided_by_email = NULL, decision_comment = NULL",
    ],
)
def test_decided_rows_reject_every_update(sheets, status, assignment):
    _seed(sheets, status)
    _refuse(sheets, f"UPDATE {TABLE} SET {assignment} WHERE id = :id", DECIDED_ERROR)


@pytest.mark.parametrize("status", ["submitted", "approved", "returned"])
def test_submitted_history_cannot_be_deleted(sheets, status):
    _seed(sheets, status)
    _refuse(sheets, f"DELETE FROM {TABLE} WHERE id = :id", DELETE_ERROR)
