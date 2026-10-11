"""Real migrated embedding constraints preserve corpus and scope play rows."""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError


def test_play_embedding_migration_constraints_and_campaign_cascade(pg):
    engine = create_engine(pg.url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                campaign_id = str(uuid4())
                connection.execute(
                    text(
                        "INSERT INTO grimoire.campaign (id, name) VALUES (:id, 'Embedding test')"
                    ),
                    {"id": campaign_id},
                )
                base = {
                    "source_id": str(uuid4()),
                    "vector": "[" + ",".join(["0.1"] * 1024) + "]",
                }
                corpus_ids = []
                for kind in ("entity", "chunk"):
                    corpus_ids.append(
                        connection.scalar(
                            text(
                                "INSERT INTO grimoire.embedding (embeddable_kind, embeddable_id, model, dim, vector) "
                                "VALUES (:kind, :source_id, 'play-migration-test', 1024, CAST(:vector AS vector)) RETURNING id"
                            ),
                            {**base, "kind": kind},
                        )
                    )
                assert (
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM grimoire.embedding WHERE id IN (:a, :b) AND campaign_id IS NULL AND audience IS NULL AND audience_pc_ids IS NULL"
                        ),
                        {"a": corpus_ids[0], "b": corpus_ids[1]},
                    )
                    == 2
                )
                for kind, audience, readable in (
                    ("note", "character", False),
                    ("note", "party", True),
                    ("event", "table", None),
                    ("transcript", "dm", None),
                    ("event", "pcs", None),
                ):
                    connection.execute(
                        text(
                            "INSERT INTO grimoire.embedding (embeddable_kind, embeddable_id, model, dim, vector, campaign_id, audience, audience_pc_ids, dm_readable) "
                            "VALUES (:kind, :source_id, :model, 1024, CAST(:vector AS vector), :campaign, :audience, '[]'::jsonb, :readable)"
                        ),
                        {
                            **base,
                            "kind": kind,
                            "model": str(uuid4()),
                            "campaign": campaign_id,
                            "audience": audience,
                            "readable": readable,
                        },
                    )
                for kind in ("note", "event", "transcript", "fact"):
                    with pytest.raises(
                        IntegrityError, match="embedding_play_audience_chk"
                    ):
                        with connection.begin_nested():
                            connection.execute(
                                text(
                                    "INSERT INTO grimoire.embedding (embeddable_kind, embeddable_id, model, dim, vector) "
                                    "VALUES (:kind, :source_id, :model, 1024, CAST(:vector AS vector))"
                                ),
                                {**base, "kind": kind, "model": str(uuid4())},
                            )
                for kind, audience, campaign, pc_ids, readable in (
                    ("note", "character", campaign_id, "[]", None),
                    ("note", "table", campaign_id, "[]", False),
                    ("event", "party", campaign_id, "[]", None),
                    ("event", "table", None, "[]", None),
                    ("transcript", None, campaign_id, "[]", None),
                    ("event", "table", campaign_id, None, None),
                    ("chunk", "table", campaign_id, "[]", None),
                ):
                    with pytest.raises(
                        IntegrityError, match="embedding_play_audience_chk"
                    ):
                        with connection.begin_nested():
                            connection.execute(
                                text(
                                    "INSERT INTO grimoire.embedding (embeddable_kind, embeddable_id, model, dim, vector, campaign_id, audience, audience_pc_ids, dm_readable) "
                                    "VALUES (:kind, :source_id, :model, 1024, CAST(:vector AS vector), :campaign, :audience, CAST(:pcs AS jsonb), :readable)"
                                ),
                                {
                                    **base,
                                    "kind": kind,
                                    "model": str(uuid4()),
                                    "campaign": campaign,
                                    "audience": audience,
                                    "pcs": pc_ids,
                                    "readable": readable,
                                },
                            )
                assert (
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_indexes WHERE schemaname = 'grimoire' AND indexname = 'embedding_campaign_kind_idx'"
                        )
                    )
                    == 1
                )
                connection.execute(
                    text("DELETE FROM grimoire.campaign WHERE id = :id"),
                    {"id": campaign_id},
                )
                assert (
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM grimoire.embedding WHERE campaign_id = :id"
                        ),
                        {"id": campaign_id},
                    )
                    == 0
                )
                assert (
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM grimoire.embedding WHERE id IN (:a, :b)"
                        ),
                        {"a": corpus_ids[0], "b": corpus_ids[1]},
                    )
                    == 2
                )
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
