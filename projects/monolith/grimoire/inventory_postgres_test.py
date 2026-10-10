"""Real migrations enforce inventory ownership, audit immutability and FKs."""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from grimoire.testing.inventory_matrix import (
    assert_inventory_agreement,
    inventory_table,
)


def test_postgres_inventory_agreement(pg):
    engine = create_engine(pg.url)
    table = inventory_table(f"inventory_matrix_{uuid4().hex}")
    try:
        table.create(engine)
        with engine.begin() as connection:
            assert_inventory_agreement(connection, table)
    finally:
        table.drop(engine, checkfirst=True)
        engine.dispose()


def test_real_inventory_migration_constraints_append_only_and_fk_lifecycle(pg):
    # The pg fixture applies every real chart migration, including inventory.
    engine = create_engine(pg.url)
    ids = {
        key: str(uuid4())
        for key in (
            "campaign",
            "user",
            "member",
            "pc",
            "session",
            "event",
            "item",
            "audit",
            "entity",
        )
    }
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                # Report a blocked statement instead of exhausting the CI test
                # timeout, so FK lifecycle regressions identify their SQL.
                connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
                connection.exec_driver_sql("SET LOCAL statement_timeout = '15s'")
                for sql in (
                    "INSERT INTO grimoire.campaign (id, name) VALUES (:campaign, 'Inventory')",
                    "INSERT INTO grimoire.app_user (id, email) VALUES (:user, :email)",
                    "INSERT INTO grimoire.player_character (id, campaign_id, character_name) VALUES (:pc, :campaign, 'PC')",
                    "INSERT INTO grimoire.campaign_member (id, campaign_id, app_user_id, role, player_character_id) VALUES (:member, :campaign, :user, 'player', :pc)",
                    "INSERT INTO grimoire.game_session (id, campaign_id) VALUES (:session, :campaign)",
                    "INSERT INTO grimoire.session_event (id, campaign_id, session_id, seq, kind, audience) VALUES (:event, :campaign, :session, 1, 'system', 'table')",
                    "INSERT INTO grimoire.entity (id, entity_type, name) VALUES (:entity, 'item', 'Rope')",
                    "INSERT INTO grimoire.inventory_item (id, campaign_id, owner_kind, name, quantity, entity_id) VALUES (:item, :campaign, 'party', 'Rope', 3, :entity)",
                    "INSERT INTO grimoire.inventory_change (id, campaign_id, item_id, who_member_id, action, delta, quantity_after, session_id, event_id) VALUES (:audit, :campaign, :item, :member, 'create', 3, 3, :session, :event)",
                ):
                    connection.execute(
                        text(sql),
                        {**ids, "email": f"inventory-{ids['user']}@example.test"},
                    )
                row = connection.execute(
                    text("SELECT * FROM grimoire.inventory_item WHERE id = :item"), ids
                ).one()
                assert (
                    row.notes == ""
                    and row.hidden_from_party is False
                    and row.deleted_at is None
                )
                assert row.created_at is not None and row.updated_at is not None
                audit = connection.execute(
                    text("SELECT * FROM grimoire.inventory_change WHERE id = :audit"),
                    ids,
                ).one()
                assert (
                    audit.reason == ""
                    and audit.changes == {}
                    and audit.created_at is not None
                )
                for owner, pc, quantity, name in (
                    ("party", ids["pc"], 1, "Rope"),
                    ("character", None, 1, "Rope"),
                    ("unknown", None, 1, "Rope"),
                    ("party", None, -1, "Rope"),
                    ("party", None, 1000001, "Rope"),
                    ("party", None, 1, ""),
                    ("party", None, 1, "x" * 201),
                ):
                    with pytest.raises(IntegrityError), connection.begin_nested():
                        connection.execute(
                            text(
                                "INSERT INTO grimoire.inventory_item (campaign_id, owner_kind, player_character_id, name, quantity) VALUES (:campaign, :owner, :bad_pc, :name, :quantity)"
                            ),
                            {
                                **ids,
                                "owner": owner,
                                "bad_pc": pc,
                                "name": name,
                                "quantity": quantity,
                            },
                        )
                for quantity in (0, 1000000):
                    connection.execute(
                        text(
                            "INSERT INTO grimoire.inventory_item (campaign_id, owner_kind, name, quantity) VALUES (:campaign, 'party', 'Boundary', :quantity)"
                        ),
                        {**ids, "quantity": quantity},
                    )
                for changes, reason in (("[]", ""), ('"bad"', ""), ("{}", "x" * 501)):
                    with pytest.raises(IntegrityError), connection.begin_nested():
                        connection.execute(
                            text(
                                "INSERT INTO grimoire.inventory_change (campaign_id, item_id, action, delta, quantity_after, changes, reason) VALUES (:campaign, :item, 'update', 0, 3, CAST(:changes AS jsonb), :reason)"
                            ),
                            {**ids, "changes": changes, "reason": reason},
                        )
                for assignment in (
                    "reason = 'edited'",
                    "delta = 999",
                    "who_member_id = NULL",
                    "session_id = NULL",
                    "event_id = NULL",
                    "reason = reason",
                ):
                    with (
                        pytest.raises(
                            DBAPIError, match="inventory_change is append-only"
                        ),
                        connection.begin_nested(),
                    ):
                        connection.execute(
                            text(
                                f"UPDATE grimoire.inventory_change SET {assignment} WHERE id = :audit"
                            ),
                            ids,
                        )
                with (
                    pytest.raises(
                        DBAPIError, match="inventory_change is append-only"
                    ),
                    connection.begin_nested(),
                ):
                    connection.execute(
                        text(
                            "DELETE FROM grimoire.inventory_change WHERE id = :audit"
                        ),
                        ids,
                    )
                with (
                    pytest.raises(
                        DBAPIError, match="inventory_item is soft-deleted"
                    ),
                    connection.begin_nested(),
                ):
                    connection.execute(
                        text(
                            "DELETE FROM grimoire.inventory_item WHERE id = :item"
                        ),
                        ids,
                    )
                assert (
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM information_schema.table_privileges WHERE table_schema = 'grimoire' AND table_name IN ('inventory_item', 'inventory_change') AND grantee = 'public_reader'"
                        )
                    )
                    == 0
                )
                for sql in (
                    "DELETE FROM grimoire.campaign_member WHERE id = :member",
                    "DELETE FROM grimoire.session_event WHERE id = :event",
                    "DELETE FROM grimoire.game_session WHERE id = :session",
                    "DELETE FROM grimoire.entity WHERE id = :entity",
                ):
                    connection.execute(text(sql), ids)
                audit = connection.execute(
                    text("SELECT * FROM grimoire.inventory_change WHERE id = :audit"),
                    ids,
                ).one()
                assert (
                    audit.who_member_id is None
                    and audit.session_id is None
                    and audit.event_id is None
                )
                assert (
                    audit.delta == 3
                    and audit.quantity_after == 3
                    and audit.changes == {}
                )
                assert (
                    connection.scalar(
                        text(
                            "SELECT entity_id FROM grimoire.inventory_item WHERE id = :item"
                        ),
                        ids,
                    )
                    is None
                )
                # A PC deletion cascades its inventory and audit, too.
                character_item = connection.scalar(
                    text(
                        "INSERT INTO grimoire.inventory_item (campaign_id, owner_kind, player_character_id, name, quantity) VALUES (:campaign, 'character', :pc, 'PC item', 1) RETURNING id"
                    ),
                    ids,
                )
                connection.execute(
                    text(
                        "INSERT INTO grimoire.inventory_change (campaign_id, item_id, action, delta, quantity_after) VALUES (:campaign, :pc_item, 'create', 1, 1)"
                    ),
                    {**ids, "pc_item": character_item},
                )
                connection.execute(
                    text("DELETE FROM grimoire.player_character WHERE id = :pc"), ids
                )
                assert (
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM grimoire.inventory_item WHERE id = :pc_item"
                        ),
                        {"pc_item": character_item},
                    )
                    == 0
                )
                assert (
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM grimoire.inventory_change WHERE item_id = :pc_item"
                        ),
                        {"pc_item": character_item},
                    )
                    == 0
                )
                connection.execute(
                    text("DELETE FROM grimoire.campaign WHERE id = :campaign"), ids
                )
                assert (
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM grimoire.inventory_item WHERE campaign_id = :campaign"
                        ),
                        ids,
                    )
                    == 0
                )
                assert (
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM grimoire.inventory_change WHERE campaign_id = :campaign"
                        ),
                        ids,
                    )
                    == 0
                )
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
