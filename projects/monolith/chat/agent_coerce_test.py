"""Unit tests for agent._coerce_username() -- all input paths."""

import logging

from chat.agent import _coerce_username


class TestCoerceUsernameNone:
    def test_none_returns_none(self):
        """None input returns None."""
        assert _coerce_username(None) is None


class TestCoerceUsernameString:
    def test_string_passthrough(self):
        """A plain string is returned unchanged."""
        assert _coerce_username("alice") == "alice"

    def test_empty_string_passthrough(self):
        """An empty string is returned unchanged."""
        assert _coerce_username("") == ""

    def test_string_with_spaces_passthrough(self):
        """A string with spaces is returned unchanged."""
        assert _coerce_username("alice smith") == "alice smith"


class TestCoerceUsernameDictWithUsername:
    def test_dict_with_username_key(self):
        """Dict with 'username' key returns the string value."""
        result = _coerce_username({"username": "alice", "id": 123})
        assert result == "alice"

    def test_dict_with_name_key(self):
        """Dict with 'name' key (but no 'username') returns the name value."""
        result = _coerce_username({"name": "bob"})
        assert result == "bob"

    def test_dict_with_display_name_key(self):
        """Dict with 'display_name' key (but no 'username'/'name') returns it."""
        result = _coerce_username({"display_name": "carol"})
        assert result == "carol"

    def test_dict_prefers_username_over_name(self):
        """'username' key takes priority over 'name' when both are present."""
        result = _coerce_username({"username": "preferred", "name": "fallback"})
        assert result == "preferred"

    def test_dict_prefers_name_over_display_name(self):
        """'name' key takes priority over 'display_name' when username is absent."""
        result = _coerce_username({"name": "second", "display_name": "third"})
        assert result == "second"

    def test_dict_value_must_be_str(self):
        """Dict key with a non-string value is skipped; falls through to None with warning."""
        # username key exists but value is an int, not str — should skip it
        result = _coerce_username({"username": 42})
        assert result is None

    def test_dict_name_non_str_falls_through_to_display_name(self):
        """If 'name' value is not a string, falls through to 'display_name'."""
        result = _coerce_username({"name": 99, "display_name": "Dave"})
        assert result == "Dave"


class TestCoerceUsernameDictMissingAllKeys:
    def test_empty_dict_returns_none_with_warning(self, caplog):
        """Empty dict logs a warning and returns None."""
        with caplog.at_level(logging.WARNING, logger="chat.agent"):
            result = _coerce_username({})
        assert result is None
        assert len(caplog.records) == 1
        assert "Could not extract username" in caplog.records[0].message

    def test_dict_with_unrelated_keys_returns_none_with_warning(self, caplog):
        """Dict with no username-like keys logs a warning and returns None."""
        with caplog.at_level(logging.WARNING, logger="chat.agent"):
            result = _coerce_username({"id": 1, "email": "x@example.com"})
        assert result is None
        assert any("Could not extract username" in r.message for r in caplog.records)


class TestCoerceUsernameNonStrDict:
    def test_int_coerced_to_string(self):
        """Integer is converted via str()."""
        assert _coerce_username(42) == "42"

    def test_float_coerced_to_string(self):
        """Float is converted via str()."""
        assert _coerce_username(3.14) == "3.14"

    def test_list_coerced_to_string(self):
        """List is converted via str()."""
        result = _coerce_username([1, 2])
        assert result == "[1, 2]"

    def test_bool_coerced_to_string(self):
        """Bool (non-str, non-dict) is converted via str()."""
        # bool is a subclass of int, not str, so falls through to str()
        assert _coerce_username(True) == "True"
