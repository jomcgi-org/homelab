# EmberVM op-log DB credential (ADR embervm/007)

The EmberVM control plane connects to `monolith-pg-rw.monolith.svc` as the
`embervm` role and stores its op-log in the `embervm_oplog` database. CNPG sets
that role's password from a `kubernetes.io/basic-auth` secret named
`monolith-pg-embervm-oplog` (referenced by `passwordSecret` in
`chart/templates/cnpg-cluster.yaml`).

## Why this secret is created out-of-band

Same reason as [public-reader-secret.md](public-reader-secret.md): CNPG's
`managed.roles[].passwordSecret` requires a `kubernetes.io/basic-auth` secret
(keys `username` + `password`) labelled `cnpg.io/reload`, and the 1Password
operator emits only `Opaque` secrets with no way to set a type or labels.

The single source of truth is the 1Password item `k8s-homelab/embervm-oplog-db`.
EmberVM reads the same item through its own `OnePasswordItem` (Opaque is fine
there) to get the full DSN, so the role password and the DSN cannot drift.

## The password MUST be URL-safe

`Embervm.OpLog.Postgres.connect/1` parses the DSN with `URI.parse` and does not
percent-decode `userinfo`. A password containing `@`, `/`, `:`, `?` or `#` will
mis-parse into the wrong host or database, and the control plane fails to boot
with `{:connect_failed, ...}` rather than with anything that names the real
cause. Generate alphanumerics only:

```sh
LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40
```

## The 1Password item

Item `k8s-homelab/embervm-oplog-db` needs exactly **one** field, named
`password`, holding the URL-safe password generated above. It must land as a
Secret key named exactly `password`, which is what `opLog.postgres.secretKey`
expects.

The connection string is not stored anywhere. `chart/templates/deployment.yaml`
assembles it from `opLog.postgres.{user,host,port,database}` plus the password,
using the same `$(VAR)` env interpolation `RELEASE_NODE` uses. So the credential
exists in exactly two places (this item and the CNPG basic-auth secret derived
from it), the connection target stays reviewable in git, and there is no
assembled copy that can drift.

## (Re)create the CNPG secret

Requires a logged-in `op` CLI and a kubectl context on the cluster. Idempotent.

```sh
PW="$(op read 'op://k8s-homelab/embervm-oplog-db/password')"
kubectl delete secret monolith-pg-embervm-oplog -n monolith --ignore-not-found
kubectl create secret generic monolith-pg-embervm-oplog -n monolith \
  --type=kubernetes.io/basic-auth \
  --from-literal=username=embervm \
  --from-literal=password="$PW"
kubectl label secret monolith-pg-embervm-oplog -n monolith cnpg.io/reload=""
```

## Order of operations on first rollout

1. Create the 1Password item and run the block above. **Do this first.** Without
   the secret CNPG cannot set the role password, and without the role the
   `Database` CR cannot be created with `owner: embervm`.
2. Let ArgoCD sync `monolith`. Confirm the role and database exist:
   ```sh
   kubectl get database monolith-pg-embervm-oplog -n monolith
   kubectl exec -n monolith monolith-pg-1 -c postgres -- \
     psql -tAc "\l embervm_oplog"
   ```
3. Let ArgoCD sync `embervm`. The control plane creates its own 13 tables on
   first boot; look for `embervm op-log opened against Postgres` in its logs.
   Until step 2 completes the CP crash-loops on `{:connect_failed, ...}`, which
   is expected and self-healing.

## Verifying which backend is live

`Embervm.Application.op_log_mod/0` selects Postgres purely on
`EMBERVM_OPLOG_DSN` being set and non-empty, so the pod spec is the answer:

```sh
kubectl get deploy embervm-embervm -n embervm -o yaml | grep -A 3 EMBERVM_OPLOG
```

`EMBERVM_OPLOG_DSN` present means Postgres; `EMBERVM_OPLOG_PATH` present means
SQLite. The chart never renders both.

## Rollback

Set `opLog.postgres.enabled: false` in `projects/embervm/deploy/values.yaml`.
The PVC re-renders and the CP restarts on SQLite with an **empty** op-log, since
nothing is migrated in either direction. Running state rebuilds from node
adoption on the next dial-home. Leave the monolith side enabled; the database is
`databaseReclaimPolicy: retain` and costs nothing while idle.
