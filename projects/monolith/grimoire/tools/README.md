# Upload an extracted book to R2

This is an operator procedure. Uploading writes objects and can overwrite an
existing book prefix. Use an approved fixture and prefix for validation. Loading
the book into Postgres is a separate operation requiring authorization.

Install `rclone` and `gzip`. The bucket must already exist. No port-forward or
bucket provisioning is needed. The shared chart contract is:

| Environment | Chart source |
| --- | --- |
| `SEAWEEDFS_S3_ENDPOINT` | `stars.s3Endpoint`, the complete HTTPS R2 URL |
| `AWS_REGION` | `stars.s3Region`, currently `auto` |
| `GRIMOIRE_S3_BUCKET` | `grimoire.s3Bucket` |
| `S3_ACCESS_KEY_ID` | `monolith-r2-s3` Secret key `access-key-id` |
| `S3_SECRET_ACCESS_KEY` | `monolith-r2-s3` Secret key `secret-access-key` |

The Secret is supplied by the 1Password Operator. In an authorized operator
shell, inject that existing identity from the approved secret manager into the
two `S3_*` variables. Do not copy values into scripts, command arguments, shell
history, logs or a persistent rclone config. Do not enable shell tracing or
rclone debug dumps. For manual injection, Bash can read the existing values
without echoing them; use a subshell so they expire when it exits:

```bash
(
  set +x
  read -r -s -p 'Existing R2 access key: ' S3_ACCESS_KEY_ID; echo
  read -r -s -p 'Existing R2 secret key: ' S3_SECRET_ACCESS_KEY; echo
  export S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY
  # Supply the current chart endpoint and bucket through the operator environment.
  : "${SEAWEEDFS_S3_ENDPOINT:?export stars.s3Endpoint as a complete HTTPS URL}"
  : "${AWS_REGION:?export stars.s3Region (auto)}"
  : "${GRIMOIRE_S3_BUCKET:?export grimoire.s3Bucket}"
  bash projects/monolith/grimoire/tools/upload-book.sh '<book-id>' '/path/to/book dir'
)
```

The script compresses `output.json` without changing it. Under
`s3://$GRIMOIRE_S3_BUCKET/books/<book-id>/` it writes `raw/output.json.gz`,
optional `raw/output.metadata.json`, images under `raw/img/<original filename>`
(`.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`), and optional `chunks/chunks.ndjson`.
Generate chunks separately with `grimoire/marker.py` if needed. Missing optional
files are skipped; upload failures stop the script. Only its own temporary
directory is removed. It neither loads the book nor deletes remote objects.

## Other manual S3 operations

CERRA and the Echo fixture use the same endpoint, region and existing identity.
In the credential-scoped operator subshell, configure an environment-only
remote for their documented commands:

```bash
: "${SEAWEEDFS_S3_ENDPOINT:?required}" "${AWS_REGION:?required}"
: "${S3_ACCESS_KEY_ID:?required}" "${S3_SECRET_ACCESS_KEY:?required}"
[[ "$SEAWEEDFS_S3_ENDPOINT" == https://* ]] || exit 1
export RCLONE_CONFIG_R2_TYPE=s3 RCLONE_CONFIG_R2_PROVIDER=Cloudflare
export RCLONE_CONFIG_R2_ENDPOINT="$SEAWEEDFS_S3_ENDPOINT"
export RCLONE_CONFIG_R2_REGION="$AWS_REGION"
export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$S3_ACCESS_KEY_ID"
export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$S3_SECRET_ACCESS_KEY"
export RCLONE_CONFIG_R2_FORCE_PATH_STYLE=true
```

Pass `--config /dev/null --s3-no-check-bucket` on every rclone command to avoid
reading a saved identity or attempting bucket creation. Resolve each destination
from its chart settings before writing. Do not run loaders, row replacements or
CDN purges as part of an access check.
