"""BDD coverage for attaching usage to raw session evidence."""

import time

import httpx
from sqlalchemy import create_engine, text

from shared.testing.markers import covers_route


@covers_route("/api/knowledge/raws/{raw_id}/usage", method="POST")
def test_attach_and_price_raw_usage(live_server_with_fake_embedding, pg):
    marker = time.time_ns()
    created = httpx.post(
        f"{live_server_with_fake_embedding}/api/knowledge/raws",
        json={
            "content": f"BDD raw usage {marker}",
            "source": "bdd-test",
        },
    )
    assert created.status_code == 201
    raw_id = created.json()["raw_id"]

    attached = httpx.post(
        f"{live_server_with_fake_embedding}/api/knowledge/raws/{raw_id}/usage",
        json={
            "usage": {"shape": "codex", "input_tokens": 1_000_000},
            "models": ["gpt-5.6-luna"],
            "model": "luna",
        },
    )
    assert attached.status_code == 200
    assert attached.json() == {"raw_id": raw_id, "updated": True}

    engine = create_engine(pg.url)
    try:
        with engine.connect() as connection:
            extra = connection.execute(
                text("SELECT extra FROM knowledge.raw_inputs WHERE raw_id = :raw_id"),
                {"raw_id": raw_id},
            ).scalar_one()
    finally:
        engine.dispose()
    assert extra["usage"]["input_tokens"] == 1_000_000
    assert extra["usage_cost_usd"] > 0
    assert extra["usage_cost_source"] == "list"

    missing = httpx.post(
        f"{live_server_with_fake_embedding}/api/knowledge/raws/unknown-{marker}/usage",
        json={"usage": {"shape": "claude"}, "models": []},
    )
    assert missing.status_code == 404
