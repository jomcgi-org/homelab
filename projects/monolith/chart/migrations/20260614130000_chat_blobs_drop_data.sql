-- Drop chat.blobs.data permanently. Raw attachment bytes now live in SeaweedFS
-- object storage (s3://<CHAT_BLOB_S3_BUCKET>/blobs/<sha256>), written by
-- chat.store._blob_s3_put. All 363 pre-existing rows were exported to SeaweedFS
-- and verified out-of-band, so the column is no longer read or written and can
-- be removed. The row now holds only metadata (sha256, content_type,
-- description). Note: dropping the column reclaims it logically, but a manual
-- VACUUM FULL chat.blobs is still required to return the TOAST space to the
-- volume.
ALTER TABLE chat.blobs DROP COLUMN data;
