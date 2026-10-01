"""Guard the repo-doc drop migration and prevent reintroduction (#3905)."""

import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "chart/migrations"
MIGRATION = MIGRATIONS / "20260929120000_drop_repo_docs.sql"


def test_repo_docs_drop_replaces_the_view_before_dropping_both_tables():
    assert MIGRATION.is_file(), f"Missing drop migration: {MIGRATION.name}"
    sql = MIGRATION.read_text().lower()
    for table in ("repo_doc_chunks", "repo_docs"):
        assert re.search(
            rf"\bdrop\s+table\s+(?:if\s+exists\s+)?knowledge\.{table}\s*;", sql
        ), f"Missing DROP for knowledge.{table}"
    view = sql.index("create or replace view public_api.knowledge_chunks")
    first_drop = re.search(r"\bdrop\s+table\b", sql)
    assert view < first_drop.start(), "Replace the view before dropping tables"
    assert "repo_doc" not in sql[view : first_drop.start()]


def test_later_migrations_do_not_reference_repo_docs():
    migrations = list(MIGRATIONS.glob("*.sql"))
    assert MIGRATION in migrations, f"Drop migration not scanned: {MIGRATION.name}"
    offenders = []
    for migration in sorted(migrations):
        timestamp = re.match(r"\d{14}(?=_)", migration.name)
        if timestamp and timestamp.group() > MIGRATION.name[:14]:
            if "repo_doc" in migration.read_text().lower():
                offenders.append(migration.name)
    assert not offenders, f"Later migrations reference repo_doc: {', '.join(offenders)}"
