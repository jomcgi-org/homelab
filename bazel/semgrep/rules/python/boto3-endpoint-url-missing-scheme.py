# Tests for boto3-endpoint-url-missing-scheme rule.
# boto3 raises "Invalid endpoint" at runtime when endpoint_url is a scheme-less
# host:port value like "localhost:9000".  Variables and env-var reads must be
# guarded with startswith(("http://", "https://")) before being passed.
import boto3
import os


def bad_env_var_no_scheme():
    endpoint = os.environ.get("S3_ENDPOINT_URL", "")
    # ruleid: boto3-endpoint-url-missing-scheme
    client = boto3.client("s3", endpoint_url=endpoint)
    return client


def bad_config_variable():
    endpoint = config.get("endpoint_url")
    # ruleid: boto3-endpoint-url-missing-scheme
    client = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1")
    return client


def bad_scheme_less_literal():
    # ruleid: boto3-endpoint-url-missing-scheme
    client = boto3.client("s3", endpoint_url="minio.internal:9000")
    return client


def bad_expression():
    host = os.environ["S3_HOST"]
    port = os.environ["S3_PORT"]
    # ruleid: boto3-endpoint-url-missing-scheme
    client = boto3.client("s3", endpoint_url=host + ":" + port)
    return client


def ok_https_literal():
    # ok: hardcoded https URL already has the scheme
    client = boto3.client("s3", endpoint_url="https://s3.amazonaws.com")
    return client


def ok_http_literal():
    # ok: hardcoded http URL already has the scheme
    client = boto3.client("s3", endpoint_url="http://minio.local:9000")
    return client


def ok_no_endpoint_url():
    # ok: no endpoint_url keyword, connects to the default AWS endpoint
    client = boto3.client("s3", region_name="us-east-1")
    return client


def ok_guarded_url():
    url = os.environ.get("S3_ENDPOINT_URL", "")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    # ok: scheme guard applied above; literal after guard has scheme
    client = boto3.client("s3", endpoint_url="http://" + url.lstrip("http://"))
    return client
