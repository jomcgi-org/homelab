# public_reader DB credential (ADR 004, Phase 5b)

The anonymous public service connects to `monolith-pg-ro` as the read-only
`public_reader` role. CNPG sets that role's password from a
`kubernetes.io/basic-auth` secret named `monolith-pg-public-reader` (referenced
by `passwordSecret` in `chart/templates/cnpg-cluster.yaml`).

## Why this secret is created out-of-band

CNPG's `managed.roles[].passwordSecret` requires a `kubernetes.io/basic-auth`
secret (keys `username` + `password`) labelled `cnpg.io/reload`. The 1Password
operator emits only `Opaque` secrets and the `OnePasswordItem` CRD cannot set the
secret type or labels, so this one secret cannot be synced declaratively.

The password's single source of truth is the 1Password item
`k8s-homelab/public-reader-db`. The public service itself reads that same item
through its own `OnePasswordItem` (Opaque is fine there), so both stay
consistent. This is the only manual step; everything else is GitOps.

The credential is low-value by design: read-only, public data only, scoped by
the `public_api` views (Phase 2 + 5a').

## (Re)create the secret

Requires a logged-in `op` CLI and a kubectl context on the cluster. Idempotent.

```sh
PW="$(op read 'op://k8s-homelab/public-reader-db/password')"
kubectl delete secret monolith-pg-public-reader -n monolith --ignore-not-found
kubectl create secret generic monolith-pg-public-reader -n monolith \
  --type=kubernetes.io/basic-auth \
  --from-literal=username=public_reader \
  --from-literal=password="$PW"
kubectl label secret monolith-pg-public-reader -n monolith cnpg.io/reload=true
```

If the 1Password item is ever lost, recreate it with a fresh generated password
(`op item create --category=login --vault=k8s-homelab --title=public-reader-db
--generate-password='letters,digits,32' username=public_reader`) and re-run the
block above so CNPG resets the role password to match.
