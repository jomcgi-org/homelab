"""Replay the real roadmap upgrade over privately collected feedback."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlmodel import Session, create_engine, text

from grimoire import feedback
from shared.testing.plugin import _find_migrations_dir

_ROADMAP = "20261113090000_grimoire_feedback_roadmap.sql"
_AUTHOR = "private-upgrade-author@example.invalid"
_PAGE = "/private-upgrade-page"
_CAMPAIGN_NAME = "Private upgrade campaign"
_COLUMNS = (
    "id, text, author_email, page, campaign_id, status, issue_number, resolved_at"
)


def _assert_no_metadata(items, private_values):
    serialized = " ".join(item.model_dump_json() for item in items)
    for value in private_values:
        assert value not in serialized
    for item in items:
        assert {
            "author",
            "author_email",
            "page",
            "campaign_id",
            "campaign_name",
        }.isdisjoint(item.model_dump())


def test_roadmap_upgrade_preserves_private_feedback(pg, monkeypatch):
    def unexpected_api(*args, **kwargs):
        raise AssertionError("unexpected GitHub/API call")

    reads = []

    def issue_states(repo, labels, **kwargs):
        reads.append((repo, labels))
        return {987654: "open"}

    monkeypatch.setattr(feedback, "list_issue_states", issue_states)
    monkeypatch.setattr(feedback, "create_issue", unexpected_api)
    # Fail any unstubbed HTTP access, including new GitHub integration paths.
    monkeypatch.setattr("httpx.Client.send", unexpected_api)
    monkeypatch.setattr("httpx.AsyncClient.send", unexpected_api)

    database = f"feedback_upgrade_{uuid4().hex}"
    admin = create_engine(pg.url, isolation_level="AUTOCOMMIT")
    engine = create_engine(admin.url.set(database=database))
    created = False
    try:
        with admin.connect() as conn:
            original_database = conn.exec_driver_sql(
                "SELECT current_database()"
            ).scalar_one()
            original_relation = conn.exec_driver_sql(
                "SELECT 'grimoire.feedback'::regclass::oid"
            ).scalar_one()
            assert database != original_database
            conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
            created = True

        migrations = sorted(_find_migrations_dir().glob("*.sql"))
        assert any(path.name == _ROADMAP for path in migrations)
        campaign_id = str(uuid4())
        ids = {status: str(uuid4()) for status in ("new", "dismissed", "filed")}
        private_texts = {status: f"Private upgrade {status} secret" for status in ids}
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE EXTENSION vector")
            for path in migrations:
                if path.name >= _ROADMAP:
                    break
                conn.execute(text(path.read_text()))
            columns = set(
                conn.exec_driver_sql(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'grimoire' AND table_name = 'feedback'"
                ).scalars()
            )
            assert {"shared", "title"}.isdisjoint(columns)
            conn.execute(
                text("INSERT INTO grimoire.campaign (id, name) VALUES (:id, :name)"),
                {"id": campaign_id, "name": _CAMPAIGN_NAME},
            )
            for status, row_id in ids.items():
                conn.execute(
                    text(
                        f"INSERT INTO grimoire.feedback ({_COLUMNS}) VALUES "
                        "(:id, :text, :author, :page, :campaign, :status, :issue, :resolved)"
                    ),
                    {
                        "id": row_id,
                        "text": private_texts[status],
                        "author": _AUTHOR,
                        "page": _PAGE,
                        "campaign": campaign_id,
                        "status": status,
                        "issue": 987654 if status == "filed" else None,
                        "resolved": None
                        if status == "new"
                        else datetime(2026, 11, 1, tzinfo=UTC),
                    },
                )
            before = conn.exec_driver_sql(
                f"SELECT {_COLUMNS} FROM grimoire.feedback ORDER BY id"
            ).all()
            for path in migrations:
                if path.name >= _ROADMAP:
                    conn.execute(text(path.read_text()))
            assert (
                conn.exec_driver_sql(
                    f"SELECT {_COLUMNS} FROM grimoire.feedback ORDER BY id"
                ).all()
                == before
            )
            assert (
                conn.exec_driver_sql("SELECT shared FROM grimoire.feedback ORDER BY id")
                .scalars()
                .all()
                == [False] * 3
            ), "legacy feedback must remain private after upgrade"
            assert (
                conn.exec_driver_sql(
                    "SELECT title FROM grimoire.feedback WHERE status = 'filed'"
                ).scalar_one()
                is None
            )

        private_metadata = [_AUTHOR, _PAGE, campaign_id, _CAMPAIGN_NAME]
        with Session(engine) as other, Session(engine) as author:
            public_items = feedback.read_roadmap(None, "other@example.invalid", other)
            assert [item.id for item in public_items] == [ids["filed"]]
            filed = public_items[0]
            assert filed.text == "Suggestion"
            assert filed.state == "accepted"
            assert filed.issue_url == (
                f"https://github.com/{feedback.GITHUB_REPO}/issues/987654"
            )
            assert filed.mine is False
            _assert_no_metadata(
                public_items, [*private_metadata, *private_texts.values()]
            )
            assert len(reads) == 1

            own_items = feedback.read_roadmap(None, _AUTHOR, author)
            assert {item.id: item.text for item in own_items} == {
                ids[status]: value for status, value in private_texts.items()
            }
            assert all(item.mine for item in own_items)

            receipt = feedback.submit_feedback(
                feedback.FeedbackRequest(text="A new shared suggestion", page=_PAGE),
                None,
                _AUTHOR,
                author,
            )
            assert (
                author.execute(
                    text("SELECT shared FROM grimoire.feedback WHERE id = :id"),
                    {"id": receipt.id},
                ).scalar_one()
                is True
            )
            public_items = feedback.read_roadmap(None, "other@example.invalid", other)
            assert {item.id for item in public_items} == {ids["filed"], receipt.id}
            new_item = next(item for item in public_items if item.id == receipt.id)
            assert new_item.text == "A new shared suggestion"
            assert new_item.mine is False
            _assert_no_metadata(
                public_items, [*private_metadata, *private_texts.values()]
            )
    finally:
        # Context managers close every Session/connection before disposing pools.
        engine.dispose()
        try:
            if created:
                with admin.connect() as conn:
                    conn.exec_driver_sql(f'DROP DATABASE "{database}"')
                    assert (
                        conn.execute(
                            text(
                                "SELECT count(*) FROM pg_database WHERE datname = :name"
                            ),
                            {"name": database},
                        ).scalar_one()
                        == 0
                    )
                    assert (
                        conn.exec_driver_sql("SELECT current_database()").scalar_one()
                        == original_database
                    )
                    assert (
                        conn.exec_driver_sql(
                            "SELECT 'grimoire.feedback'::regclass::oid"
                        ).scalar_one()
                        == original_relation
                    )
                    conn.exec_driver_sql(
                        "SELECT shared, title FROM grimoire.feedback LIMIT 1"
                    ).all()
        finally:
            admin.dispose()
