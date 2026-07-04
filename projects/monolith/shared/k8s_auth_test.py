"""Tests for shared.k8s_auth: the fc-invoke caller-token helper."""

from __future__ import annotations

from shared.k8s_auth import auth_headers, service_account_token


def test_missing_token_file_yields_none_and_empty_headers(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert service_account_token(str(missing)) is None
    assert auth_headers(str(missing)) == {}


def test_present_token_is_read_and_stripped(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("  abc.def.ghi\n")
    assert service_account_token(str(token_file)) == "abc.def.ghi"
    assert auth_headers(str(token_file)) == {"Authorization": "Bearer abc.def.ghi"}


def test_empty_token_file_is_treated_as_absent(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("   \n")
    assert service_account_token(str(token_file)) is None
    assert auth_headers(str(token_file)) == {}
