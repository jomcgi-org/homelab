"""Unit tests for shared.testing.markers BDD coverage decorators."""

import pytest

from shared.testing.markers import covers_page, covers_public, covers_route


class TestCoversRoute:
    def test_returns_mark_decorator(self):
        result = covers_route("/api/v1/items")
        assert isinstance(result, pytest.MarkDecorator)

    def test_marker_name(self):
        result = covers_route("/api/v1/items")
        assert result.mark.name == "covers_route"

    def test_path_kwarg(self):
        result = covers_route("/api/v1/items")
        assert result.mark.kwargs["path"] == "/api/v1/items"

    def test_default_method_is_get(self):
        result = covers_route("/api/v1/items")
        assert result.mark.kwargs["method"] == "GET"

    def test_explicit_method_post(self):
        result = covers_route("/api/v1/items", method="POST")
        assert result.mark.kwargs["method"] == "POST"

    def test_explicit_method_put(self):
        result = covers_route("/api/v1/items", method="PUT")
        assert result.mark.kwargs["method"] == "PUT"

    def test_explicit_method_delete(self):
        result = covers_route("/api/v1/items", method="DELETE")
        assert result.mark.kwargs["method"] == "DELETE"

    def test_explicit_method_patch(self):
        result = covers_route("/api/v1/items", method="PATCH")
        assert result.mark.kwargs["method"] == "PATCH"

    def test_path_with_parameters(self):
        result = covers_route("/api/v1/items/{item_id}")
        assert result.mark.kwargs["path"] == "/api/v1/items/{item_id}"

    def test_no_positional_args(self):
        result = covers_route("/api/v1/items")
        assert result.mark.args == ()

    def test_different_paths_produce_distinct_decorators(self):
        a = covers_route("/api/v1/foo")
        b = covers_route("/api/v1/bar")
        assert a.mark.kwargs["path"] != b.mark.kwargs["path"]


class TestCoversPage:
    def test_returns_mark_decorator(self):
        result = covers_page("/dashboard")
        assert isinstance(result, pytest.MarkDecorator)

    def test_marker_name(self):
        result = covers_page("/dashboard")
        assert result.mark.name == "covers_page"

    def test_path_kwarg(self):
        result = covers_page("/dashboard")
        assert result.mark.kwargs["path"] == "/dashboard"

    def test_nested_path(self):
        result = covers_page("/settings/profile")
        assert result.mark.kwargs["path"] == "/settings/profile"

    def test_no_positional_args(self):
        result = covers_page("/dashboard")
        assert result.mark.args == ()

    def test_different_paths_produce_distinct_decorators(self):
        a = covers_page("/foo")
        b = covers_page("/bar")
        assert a.mark.kwargs["path"] != b.mark.kwargs["path"]


class TestCoversPublic:
    def test_returns_mark_decorator(self):
        result = covers_public("knowledge.search_notes")
        assert isinstance(result, pytest.MarkDecorator)

    def test_marker_name(self):
        result = covers_public("knowledge.search_notes")
        assert result.mark.name == "covers_public"

    def test_name_kwarg(self):
        result = covers_public("knowledge.search_notes")
        assert result.mark.kwargs["name"] == "knowledge.search_notes"

    def test_simple_qualified_name(self):
        result = covers_public("knowledge.search_notes")
        assert result.mark.kwargs["name"] == "knowledge.search_notes"

    def test_deeply_nested_qualified_name(self):
        result = covers_public("app.module.submodule.function")
        assert result.mark.kwargs["name"] == "app.module.submodule.function"

    def test_no_positional_args(self):
        result = covers_public("knowledge.search_notes")
        assert result.mark.args == ()

    def test_different_names_produce_distinct_decorators(self):
        a = covers_public("module.func_a")
        b = covers_public("module.func_b")
        assert a.mark.kwargs["name"] != b.mark.kwargs["name"]
