"""Hermetic contract for the single missing-bucket upload retry."""

import os
import subprocess
import sys
from unittest.mock import Mock, call

import pytest
from botocore.exceptions import ClientError

from core.s3_upload import put_object


def test_import_does_not_load_storage_sdks():
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import core.s3_upload; import sys; "
                "assert not any(n.split('.')[0] in ('boto3', 'botocore') for n in sys.modules)"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def _error(code):
    return ClientError({"Error": {"Code": code}}, "PutObject")


def _upload(client, body=b"payload"):
    return put_object(
        client, bucket="bucket", key="key", body=body, content_type="image/png"
    )


def test_success_puts_once():
    client = Mock()
    assert _upload(client) is None
    assert client.mock_calls == [
        call.put_object(
            Bucket="bucket", Key="key", Body=b"payload", ContentType="image/png"
        )
    ]


@pytest.mark.parametrize("code", ["NoSuchBucket", "404"])
def test_missing_bucket_creates_once_and_retries_identical_request(code):
    client = Mock()
    client.put_object.side_effect = [_error(code), {}]
    body = bytes(bytearray(b"payload"))
    _upload(client, body)
    request = dict(Bucket="bucket", Key="key", Body=body, ContentType="image/png")
    assert client.mock_calls == [
        call.put_object(**request),
        call.create_bucket(Bucket="bucket"),
        call.put_object(**request),
    ]
    first, second = client.put_object.call_args_list
    assert first.kwargs == second.kwargs
    assert first.kwargs["Body"] is body
    assert second.kwargs["Body"] is body


@pytest.mark.parametrize(
    "error",
    [_error("AccessDenied"), _error("NotFound"), _error(404), RuntimeError("offline")],
)
def test_other_errors_escape_without_create_or_retry(error):
    client = Mock()
    client.put_object.side_effect = error
    with pytest.raises(type(error)) as raised:
        _upload(client)
    assert raised.value is error
    client.put_object.assert_called_once()
    client.create_bucket.assert_not_called()


def test_create_failure_escapes_without_second_put():
    client = Mock()
    client.put_object.side_effect = _error("NoSuchBucket")
    error = _error("AccessDenied")
    client.create_bucket.side_effect = error
    with pytest.raises(ClientError) as raised:
        _upload(client)
    assert raised.value is error
    assert [entry[0] for entry in client.mock_calls] == ["put_object", "create_bucket"]


@pytest.mark.parametrize("code", ["NoSuchBucket", "404", "AccessDenied"])
def test_second_put_failure_escapes_without_more_retries(code):
    client = Mock()
    error = _error(code)
    client.put_object.side_effect = [_error("NoSuchBucket"), error]
    with pytest.raises(ClientError) as raised:
        _upload(client)
    assert raised.value is error
    assert [entry[0] for entry in client.mock_calls] == [
        "put_object",
        "create_bucket",
        "put_object",
    ]
