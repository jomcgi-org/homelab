-- Make chat.blobs.data nullable. Raw attachment bytes now live in SeaweedFS
-- object storage (s3://<CHAT_BLOB_S3_BUCKET>/blobs/<sha256>), written by
-- chat.store._blob_s3_put. New blobs store NULL here. Existing rows keep their
-- bytes until a manual out-of-band export to SeaweedFS, after which a follow-up
-- migration drops the column entirely.
ALTER TABLE chat.blobs ALTER COLUMN data DROP NOT NULL;
