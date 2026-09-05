# agents_writer DB credential (#5656)

The `monolith-agents` tier connects to `monolith-pg-rw` as `agents_writer`. CNPG
sets that role's password from a `kubernetes.io/basic-auth` secret named
`monolith-pg-agents-writer` (referenced by `passwordSecret` in
`chart/templates/cnpg-cluster.yaml`).

## Why this secret is created out-of-band

CNPG's `managed.roles[].passwordSecret` requires a `kubernetes.io/basic-auth`
secret (keys `username` + `password`) labelled `cnpg.io/reload`. The 1Password
operator emits only `Opaque` secrets and the `OnePasswordItem` CRD cannot set the
secret type or labels, so this one secret cannot be synced declaratively. Same
constraint as `public-reader-secret.md`.

The password's single source of truth is the 1Password item
`k8s-homelab/agents-writer-db`. The agents tier reads the `uri` field of that
same item through its own `OnePasswordItem` (Opaque is fine there), so both stay
consistent.

## This credential is NOT low-value

Unlike `public_reader`, this role can write: raw inputs, disputes, extraction
jobs and Discord outbox rows. What bounds it is the GRANT set in
`chart/migrations/20260904180000_agents_writer_role.sql`, which is read across
`knowledge` plus INSERT on exactly four tables, and no access at all to `home`,
`scheduler`, `trips`, `todo`, `hikes`, `ships` or `stars`.

It is the database leg of the tier's isolation. The other two are the pruned
source glob (the binary cannot import the private domains) and the ServiceAccount
carrying no cluster RBAC. Widening any one of them should be a deliberate act.

## (Re)create the secret

Requires a logged-in `op` CLI and a kubectl context on the cluster. Idempotent.

```sh
PW="$(op read 'op://k8s-homelab/agents-writer-db/password')"
kubectl delete secret monolith-pg-agents-writer -n monolith --ignore-not-found
kubectl create secret generic monolith-pg-agents-writer -n monolith \
  --type=kubernetes.io/basic-auth \
  --from-literal=username=agents_writer \
  --from-literal=password="$PW"
kubectl label secret monolith-pg-agents-writer -n monolith cnpg.io/reload=true
```

If the password is ever rotated, update the 1Password item's `password` AND `uri`
fields together (the uri embeds the password) and re-run the block above so CNPG
resets the role password to match.
