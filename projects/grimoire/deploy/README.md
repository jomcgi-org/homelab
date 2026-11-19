# Private restore rehearsal

Use the factory grimoire namespace. The database owner credential is in the
CNPG-generated grimoire-app Secret. Reserve that login for restore operations.
Provide grimoire-api-login and grimoire-worker-login as basic-auth Secrets
with usernames grimoire_api and grimoire_worker and separate generated
passwords. Runtime pods use their corresponding service account, database
login and S3 Secret. Bind inference egress to the selected private backend
and port when that endpoint is available.

Stage a consistent custom-format dump from the restricted, scoped rehearsal
database described in ARCHITECTURE.md. Include the referenced identity rows,
sequences and job checkpoints. Retain the source row counts, membership
pairs, vector index definitions and a representative nearest-neighbor result
from the same snapshot. Keep these artifacts in restricted storage.

Run the following from the authorized restore environment, with libpq service
entries for the empty target and later the recovered database. Use a fresh
database for each attempt. Restore the schema, data and post-data constraints
in one transaction. A failed foreign key or index build aborts the restore.

```sh
PGSERVICE=grimoire-rehearsal pg_restore --exit-on-error --single-transaction \
  --no-owner --no-acl --dbname=service=grimoire-rehearsal "$PRIVATE_DUMP"
```

Apply reviewed table and sequence grants to the runtime logins after import.
Compare the saved counts, identity pairs, sequence heads and vector query
results against the target. Check that the vector indexes are valid in
pg_index and that their definitions match the recorded source definitions.

Use rclone remotes restricted to the private source snapshot and the private
Garage bucket. Run from a protected directory with umask 077. Keep the
source checksum file with the dump. Verify the full key set as well as
downloaded bytes, including objects without an S3 content hash.

```sh
rclone hashsum SHA256 --download private-source:books > source.sha256
rclone copy private-source:books factory-private:grimoire-private
rclone check --download private-source:books factory-private:grimoire-private
rclone checksum SHA256 source.sha256 factory-private:grimoire-private --download
```

After validation, trigger a CNPG Backup and record its completed backup ID.
In a separate recovery namespace create a fresh CNPG Cluster with the same
Postgres and vector images, bootstrap.recovery.source pointing to an
externalClusters entry for grimoire-v1, and the source barmanObjectStore
settings with a read-only backup credential. Permit recovery traffic to
Garage. Repeat the database comparisons against that cluster with hub access
closed. Repeat the checksum test against an independently restored private
object copy. Record backup ID, counts, comparison outcomes and runtime
access probes internally. Exercise allowed DB, S3 and inference calls and
denied bucket, backup and unrelated endpoint calls as each runtime identity
before accepting the rehearsal.

Run the inventory on the quiescent scoped source, the restored rehearsal
database and the backup recovery database. Keep the outputs private and
require both comparisons to succeed.

```sh
for service in scoped-source grimoire-rehearsal grimoire-recovered; do
  PGSERVICE="$service" psql -XqAt -v ON_ERROR_STOP=1 -f inventory.sql > "$service.inventory" || exit 1
done
diff -q scoped-source.inventory grimoire-rehearsal.inventory &&
  diff -q scoped-source.inventory grimoire-recovered.inventory
```
