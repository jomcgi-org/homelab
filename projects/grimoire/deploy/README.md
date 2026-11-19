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

Record the extension version and schema on the quiescent scoped source
alongside the dump, using the same query and output format as restore.sh.

```sh
PGSERVICE=scoped-source psql -XAtq -v ON_ERROR_STOP=1 -c "SELECT extversion, nspname FROM pg_extension JOIN pg_namespace ON pg_namespace.oid = extnamespace WHERE extname = 'vector'" > vector.metadata
```

After staging grimoire-rehearsal-snapshot, include restore-job.yaml in a
one-shot GitOps overlay with a grimoire-restore-script configMapGenerator
containing restore.sh. Run the transfer from a pod in namespace grimoire with
serviceAccountName grimoire-restore. Mount the staged snapshot read-only.
Configure its rehearsal libpq service for grimoire-rw.grimoire.svc:5432,
database grimoire, user grimoire_restore, with TLS required. Supply the
password from grimoire-app only to the restore pod. Use grimoire-restore-s3
for the private destination. Stage source objects on a restricted volume
before transfer. The restore identity can reach Garage and the two database
clusters. Run restore.sh against a fresh rehearsal database.

```sh
PGSERVICE=grimoire-rehearsal PRIVATE_DUMP=/snapshot/grimoire.dump \
  VECTOR_METADATA=/snapshot/vector.metadata ./restore.sh
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
rclone hashsum SHA256 /snapshot/books > source.sha256
rclone copy /snapshot/books factory-private:grimoire-private
rclone check --download /snapshot/books factory-private:grimoire-private
rclone checksum SHA256 source.sha256 factory-private:grimoire-private --download
```

After validation, trigger a CNPG Backup and record its completed backup ID.
Once the backup completes and WAL archiving catches up, deploy recovery.yaml
as a one-shot GitOps change. It creates grimoire-recovered in the grimoire
namespace and uses grimoire-recovery-s3 to read the backup. Point the recovery
libpq service at grimoire-recovered-rw.grimoire.svc:5432 and use the owner
password from the backed-up database. Repeat the database comparisons against that cluster with hub access
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
