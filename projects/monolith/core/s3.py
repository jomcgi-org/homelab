"""Path-style S3 client for the R2 endpoint the chart injects.

Shared by every domain that reads or writes object storage through the
``SEAWEEDFS_S3_ENDPOINT`` environment (the name is historical). Callers own
their unset-endpoint policy; this module only builds the client. boto3 is
imported lazily so importing this module never pulls boto3 into the public
app's import closure (app/main_public_imports_test.py).
"""

import os


def path_style_client(endpoint: str):
    """Build a path-style S3 client, retaining legacy/test credential defaults."""
    import boto3
    from botocore.config import Config

    # The chart supplies HTTPS for R2. HTTP prefixing is retained only for
    # scheme-less legacy/test endpoints; explicit schemes pass through.
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "http://" + endpoint
    # Scheme guaranteed above; the inline nosemgrep clears the pre-commit
    # boto3-endpoint-url-missing-scheme hook.
    return boto3.client(  # nosemgrep: boto3-endpoint-url-missing-scheme
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID", "duckdb"),
        aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY", "duckdb"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )
