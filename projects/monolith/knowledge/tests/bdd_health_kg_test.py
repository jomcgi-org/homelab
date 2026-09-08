"""BDD coverage for knowledge extraction health in the private composite."""

import httpx
from uuid import uuid4

from sqlalchemy import text
from sqlmodel import Session, create_engine

from knowledge.extraction import KG_JOB_KIND
from knowledge.health import _kg_health_core
from shared.invocation_outcomes import UNKNOWN_INVOCATION
from shared.testing.markers import covers_route


@covers_route("/api/health")
def test_health_includes_kg_component(live_server_with_fake_embedding):
    response = httpx.get(f"{live_server_with_fake_embedding}/api/health")

    assert response.status_code in (200, 503)
    body = response.json()
    assert "kg" in body["components"]
    assert set(body["components"]["kg"]) == {
        "ok",
        "queued",
        "held",
        "oldest_queued_seconds",
        "failed_24h",
        "atoms_24h",
        "rejected_24h",
        "corrected_24h",
        "last_success_at",
        "jobs_today",
        "cap",
        "effective_cap",
        "burst",
        "swept_last_cycle",
        "open_disputes",
        "oldest_open_dispute_seconds",
        "repo_diff_last_sha",
        "repo_diff_last_run_at",
    }


def test_kg_held_query_scopes_and_deduplicates_real_postgres_rows(pg):
    """Execute the production SQL, including holds without a surviving job."""
    engine = create_engine(pg.url, isolation_level="REPEATABLE READ")
    prefix = "health-holds-" + uuid4().hex
    try:
        with Session(engine) as session:
            before = _kg_health_core(session, 400)
            for name, kind, status in (
                ("routine-only", KG_JOB_KIND, UNKNOWN_INVOCATION),
                ("overlap", KG_JOB_KIND, UNKNOWN_INVOCATION),
                ("permit-only", KG_JOB_KIND, "error"),
                ("settled", KG_JOB_KIND, "error"),
                ("ready", KG_JOB_KIND, None),
                ("project", "docfix", UNKNOWN_INVOCATION),
            ):
                session.execute(
                    text(
                        "INSERT INTO claude_agent.routine_jobs "
                        "(name, routine_kind, last_status, next_run_at) "
                        "VALUES (:name, :kind, :status, now())"
                    ),
                    {"name": prefix + name, "kind": kind, "status": status},
                )
            for index, (name, tier, state) in enumerate(
                (
                    ("overlap", "kg", "uncertain"),
                    ("overlap", "kg", "running"),
                    ("permit-only", "kg", "uncertain"),
                    ("missing-job", "kg", "uncertain"),
                    ("settled", "kg", "settled"),
                    ("project", "project", "uncertain"),
                    ("missing-project", "project", "uncertain"),
                )
            ):
                session.execute(
                    text(
                        "INSERT INTO agent_sessions.capacity_reservations "
                        "(local_session_id, pending_seq, tier, state, routine_job_name) "
                        "VALUES (:local_id, 1, :tier, :state, :job)"
                    ),
                    {
                        "local_id": f"{prefix}-{index}",
                        "tier": tier,
                        "state": state,
                        "job": prefix + name,
                    },
                )

            after = _kg_health_core(session, 400)

            assert after["held"] == before["held"] + 4
            assert after["queued"] == before["queued"] + 2
            session.rollback()
    finally:
        engine.dispose()
