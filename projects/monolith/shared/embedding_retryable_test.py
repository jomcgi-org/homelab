"""Unit tests for is_retryable() in shared.inference.

is_retryable() classifies exceptions as transient (worth retrying) or
permanent (fail fast). This covers all six boundary conditions:
ConnectError → True, ConnectTimeout → True, ReadTimeout → True,
HTTPStatusError(5xx) → True, HTTPStatusError(4xx) → False, ValueError → False.
"""

from unittest.mock import MagicMock

import httpx

from shared.inference import is_retryable


class TestIsRetryableEmbedding:
    def test_connect_error_is_retryable(self):
        """ConnectError is a transient network failure and should be retried."""
        exc = httpx.ConnectError("Connection refused")
        assert is_retryable(exc) is True

    def test_connect_timeout_is_retryable(self):
        """ConnectTimeout means the server is unreachable; worth retrying."""
        exc = httpx.ConnectTimeout("Connection timed out")
        assert is_retryable(exc) is True

    def test_read_timeout_is_retryable(self):
        """ReadTimeout (slow inference) is transient and should be retried."""
        exc = httpx.ReadTimeout("Read timed out")
        assert is_retryable(exc) is True

    def test_http_503_is_retryable(self):
        """HTTPStatusError with 503 Service Unavailable is retryable."""
        resp = MagicMock()
        resp.status_code = 503
        exc = httpx.HTTPStatusError(
            "Service Unavailable", request=MagicMock(), response=resp
        )
        assert is_retryable(exc) is True

    def test_http_500_is_retryable(self):
        """HTTPStatusError with 500 Internal Server Error is retryable."""
        resp = MagicMock()
        resp.status_code = 500
        exc = httpx.HTTPStatusError(
            "Internal Server Error", request=MagicMock(), response=resp
        )
        assert is_retryable(exc) is True

    def test_http_400_is_not_retryable(self):
        """HTTPStatusError with 400 Bad Request is a client error; do not retry."""
        resp = MagicMock()
        resp.status_code = 400
        exc = httpx.HTTPStatusError("Bad Request", request=MagicMock(), response=resp)
        assert is_retryable(exc) is False

    def test_http_404_is_not_retryable(self):
        """HTTPStatusError with 404 Not Found is a client error; do not retry."""
        resp = MagicMock()
        resp.status_code = 404
        exc = httpx.HTTPStatusError("Not Found", request=MagicMock(), response=resp)
        assert is_retryable(exc) is False

    def test_value_error_is_not_retryable(self):
        """ValueError (e.g. malformed response shape) is not retryable."""
        exc = ValueError("unexpected embedding response shape")
        assert is_retryable(exc) is False

    def test_generic_exception_is_not_retryable(self):
        """An arbitrary Python exception is not classified as retryable."""
        exc = RuntimeError("something went very wrong")
        assert is_retryable(exc) is False

    def test_http_422_is_not_retryable(self):
        """HTTPStatusError with 422 Unprocessable Entity is a client error; do not retry."""
        resp = MagicMock()
        resp.status_code = 422
        exc = httpx.HTTPStatusError(
            "Unprocessable Entity", request=MagicMock(), response=resp
        )
        assert is_retryable(exc) is False

    def test_boundary_499_is_not_retryable(self):
        """HTTPStatusError with 499 (4xx boundary) is not retryable."""
        resp = MagicMock()
        resp.status_code = 499
        exc = httpx.HTTPStatusError(
            "Client Closed Request", request=MagicMock(), response=resp
        )
        assert is_retryable(exc) is False

    def test_write_error_is_retryable(self):
        """WriteError (socket write failure mid-request) is retryable."""
        exc = httpx.WriteError("write failed")
        assert is_retryable(exc) is True

    def test_remote_protocol_error_is_retryable(self):
        """RemoteProtocolError (server closed conn early) is retryable."""
        exc = httpx.RemoteProtocolError("server disconnected")
        assert is_retryable(exc) is True

    def test_pool_timeout_is_retryable(self):
        """PoolTimeout (no free conn in pool) is retryable."""
        exc = httpx.PoolTimeout("no conns")
        assert is_retryable(exc) is True

    def test_network_error_is_retryable(self):
        """NetworkError (generic network failure) is retryable."""
        exc = httpx.NetworkError("boom")
        assert is_retryable(exc) is True
