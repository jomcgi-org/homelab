from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "chart/migrations/20260922120000_agent_board.sql"
)


def test_board_migration_is_one_small_table_with_required_shape():
    sql = MIGRATION.read_text()
    assert sql.count("CREATE TABLE") == 1
    for column in (
        "principal TEXT NOT NULL",
        "topic TEXT NOT NULL",
        "body TEXT NOT NULL",
        "created_at TIMESTAMPTZ NOT NULL",
        "expires_at TIMESTAMPTZ NOT NULL",
        "acknowledged_by JSONB NOT NULL",
    ):
        assert column in sql
    assert MIGRATION.stat().st_size < 8 * 1024


def test_agents_writer_grants_are_narrow_and_ack_only_update():
    sql = MIGRATION.read_text()
    assert "GRANT SELECT ON knowledge.agent_board_messages TO agents_writer" in sql
    assert "GRANT INSERT (" in sql
    assert "GRANT UPDATE (acknowledged_by)" in sql
    assert "GRANT DELETE" not in sql
    assert "GRANT ALL" not in sql
