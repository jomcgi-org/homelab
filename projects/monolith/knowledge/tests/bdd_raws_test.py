"""BDD coverage for raw evidence ingestion."""

import time

import httpx

from shared.testing.markers import covers_route


@covers_route("/api/knowledge/raws", method="POST")
def test_create_raw_evidence(live_server_with_fake_embedding):
    content = f"BDD raw evidence {time.time_ns()}"

    response = httpx.post(
        f"{live_server_with_fake_embedding}/api/knowledge/raws",
        json={"content": content, "source": "bdd-test"},
    )

    assert response.status_code == 201
    assert response.json()["created"] is True
    assert response.json()["raw_id"]
