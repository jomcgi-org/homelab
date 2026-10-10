"""Inventory SQL and Python visibility agree with the independent oracle."""

from sqlalchemy import create_engine

from grimoire.testing.inventory_matrix import (
    assert_inventory_agreement,
    inventory_table,
)


def test_sqlite_inventory_agreement(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'inventory.db'}")
    try:
        table = inventory_table("inventory_matrix")
        table.create(engine)
        with engine.begin() as connection:
            assert_inventory_agreement(connection, table)
    finally:
        engine.dispose()
