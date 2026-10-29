"""Route and schema regression coverage for the mounted chat API."""

import pytest
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts

from chat.router import router


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(router)
    return app


def test_mounted_chat_routes_and_openapi(app):
    routes = list(iter_route_contexts(app.routes))
    paths = app.openapi()["paths"]
    registered = {(route.path, method) for route in routes for method in route.methods}
    for path in ("/api/chat/explore", "/api/chat/cluster"):
        assert (path, "POST") in registered
        assert "post" in paths[path]
    for path in ("/api/chat/backfill", "/api/chat/backfill/status"):
        assert path not in {route.path for route in routes}
        assert path not in paths
    schemas = app.openapi()["components"]["schemas"]
    assert "ExploreRequest" in schemas
    assert "ClusterChatRequest" in schemas
    assert "BackfillRequest" not in schemas
    assert "BackfillStatus" not in schemas
