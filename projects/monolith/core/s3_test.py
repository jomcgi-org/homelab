"""shared_client builds one client per endpoint and reuses it."""

from unittest.mock import patch

from core import s3


def test_shared_client_is_built_once_per_endpoint():
    s3.shared_client.cache_clear()
    with patch.object(s3, "path_style_client", side_effect=lambda e: object()) as build:
        first = s3.shared_client("https://a.example")
        assert s3.shared_client("https://a.example") is first
        assert s3.shared_client("https://b.example") is not first
    assert build.call_count == 2
    s3.shared_client.cache_clear()
