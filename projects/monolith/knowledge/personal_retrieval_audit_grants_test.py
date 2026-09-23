"""Real-Postgres contract for personal retrieval audit privileges and retention.

The pg fixture applies every migration. SET ROLE proves that agents_writer can
use default-filled columns through its narrow INSERT grant, cannot inspect or
delete audit rows, and cannot inspect the backing sequence. The insert also
executes the definer-rights retention trigger in the same transaction.

Hand-written bdd_test in the gazelle-excluded knowledge package, registered in
the central BUILD file.
"""

import pytest
from sqlmodel import Session, create_engine, text


_OLD_SUBJECT = "personal-audit-grants-test-old"
_NEW_SUBJECT = "personal-audit-grants-test-new"


def _assert_agents_writer_denied(engine, statement: str) -> None:
    with Session(engine) as session:
        session.execute(text("SET ROLE agents_writer"))
        with pytest.raises(Exception) as exc:
            session.execute(text(statement)).all()
        session.rollback()
        session.execute(text("RESET ROLE"))
        session.commit()
        assert "permission denied" in str(exc.value).lower()


def test_personal_retrieval_audit_insert_only_grant_and_retention_trigger(pg):
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            session.execute(
                text(
                    """
                    INSERT INTO knowledge.personal_retrieval_audit
                        (principal_subject, principal_actor,
                         principal_authority, personal_scope, entrypoint)
                    VALUES
                        (:subject, '[]', 'standing', :scope, 'http')
                    """
                ),
                {"subject": _OLD_SUBJECT, "scope": f"personal:{_OLD_SUBJECT}"},
            )
            session.execute(
                text(
                    """
                    UPDATE knowledge.personal_retrieval_audit
                    SET created_at = pg_catalog.now() - INTERVAL '91 days'
                    WHERE principal_subject = :subject
                    """
                ),
                {"subject": _OLD_SUBJECT},
            )
            session.commit()

        with Session(engine) as session:
            session.execute(text("SET ROLE agents_writer"))
            session.execute(
                text(
                    """
                    INSERT INTO knowledge.personal_retrieval_audit
                        (principal_subject, principal_actor,
                         principal_authority, personal_scope, entrypoint)
                    VALUES (:subject, '[]', 'standing', :scope, 'http')
                    """
                ),
                {"subject": _NEW_SUBJECT, "scope": f"personal:{_NEW_SUBJECT}"},
            )
            session.execute(text("RESET ROLE"))

            rows = session.execute(
                text(
                    """
                    SELECT principal_subject, id, created_at
                    FROM knowledge.personal_retrieval_audit
                    WHERE principal_subject IN (:old_subject, :new_subject)
                    """
                ),
                {"old_subject": _OLD_SUBJECT, "new_subject": _NEW_SUBJECT},
            ).all()
            assert [row[0] for row in rows] == [_NEW_SUBJECT]
            assert rows[0][1] is not None
            assert rows[0][2] is not None
            session.commit()

        _assert_agents_writer_denied(
            engine,
            "SELECT principal_subject FROM knowledge.personal_retrieval_audit",
        )
        _assert_agents_writer_denied(
            engine,
            "DELETE FROM knowledge.personal_retrieval_audit "
            f"WHERE principal_subject = '{_NEW_SUBJECT}'",
        )
        _assert_agents_writer_denied(
            engine,
            "SELECT last_value FROM knowledge.personal_retrieval_audit_id_seq",
        )
    finally:
        with Session(engine) as session:
            session.execute(
                text(
                    "DELETE FROM knowledge.personal_retrieval_audit "
                    "WHERE principal_subject IN (:old_subject, :new_subject)"
                ),
                {"old_subject": _OLD_SUBJECT, "new_subject": _NEW_SUBJECT},
            )
            session.commit()
        engine.dispose()
