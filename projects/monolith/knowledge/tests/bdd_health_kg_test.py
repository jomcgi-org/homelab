"""BDD coverage for knowledge extraction health in the private composite."""

import httpx

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
