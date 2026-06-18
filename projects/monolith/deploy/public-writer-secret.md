# public_writer DB credential (ADR 005, Public Chat V3 Phase 1)

The public service writes session and transcript rows to the PRIMARY
`monolith-pg-rw` as the write-scoped `public_writer` role. CNPG sets that role's
password from a `kubernetes.io/basic-auth` secret named
`monolith-pg-public-writer` (referenced by `passwordSecret` in
`chart/templates/cnpg-cluster.yaml`).

`public_writer` is the generic public-tier write identity, parallel to the
read-only `public_reader`. Today its only grant is DML on the `chat_public`
schema (sessions + transcripts); the naming is generic so future public-tier
write paths can reuse the same role rather than minting a new one.

## Why this secret is created out-of-band

CNPG's `managed.roles[].passwordSecret` requires a `kubernetes.io/basic-auth`
secret (keys `username` + `password`) labelled `cnpg.io/reload`. The 1Password
operator emits only `Opaque` secrets and the `OnePasswordItem` CRD cannot set the
secret type or labels, so this one secret cannot be synced declaratively. CNPG
consumes this secret (it does not create it): it reads the password and applies
it to the role, then reloads when the `cnpg.io/reload` label changes.

The password's single source of truth is the 1Password item
`k8s-homelab/public-writer-db`. The public service itself reads that same item
through its own `OnePasswordItem` (`monolith-public-writer-db`, Opaque is fine
there), whose `uri` field holds the full `postgresql://` connection string to
`monolith-pg-rw` as `public_writer`, so both stay consistent. This is the only
manual step; everything else is GitOps.

The credential is low-value by design: DML on the single `chat_public` schema of
anonymous chat data, with no access to any other schema.

## (Re)create the secret

Requires a logged-in `op` CLI and a kubectl context on the cluster. Idempotent.

```sh
PW="$(op read 'op://k8s-homelab/public-writer-db/password')"
kubectl delete secret monolith-pg-public-writer -n monolith --ignore-not-found
kubectl create secret generic monolith-pg-public-writer -n monolith \
  --type=kubernetes.io/basic-auth \
  --from-literal=username=public_writer \
  --from-literal=password="$PW"
kubectl label secret monolith-pg-public-writer -n monolith cnpg.io/reload=true
```

If the 1Password item is ever lost, recreate it with a fresh generated password
(`op item create --category=login --vault=k8s-homelab --title=public-writer-db
--generate-password='letters,digits,32' username=public_writer`) and re-run the
block above so CNPG resets the role password to match. Update the item's `uri`
field to the matching `postgresql://public_writer:<password>@monolith-pg-rw:5432/monolith`
connection string so the app-facing `monolith-public-writer-db` secret stays in
sync.
