# SPIRE DB credential (ADR embervm/041)

The SPIRE server connects to `monolith-pg-rw.monolith.svc` as the `spire` role
and stores its data in the `spire` database. CNPG sets that role's password
from a `kubernetes.io/basic-auth` Secret named `monolith-pg-spire`, referenced
by `passwordSecret` in `chart/templates/cnpg-cluster.yaml`.

## Why this secret is created out-of-band

Same reason as [public-reader-secret.md](public-reader-secret.md): CNPG's
`managed.roles[].passwordSecret` requires a `kubernetes.io/basic-auth` Secret
with `username` and `password` keys, labelled `cnpg.io/reload`. The 1Password
operator emits only `Opaque` Secrets and cannot set the required type or label.

The single source of truth is the 1Password item `k8s-homelab/spire-db`. The
SPIRE chart reads the same item through a `OnePasswordItem` named `spire-db` in
the `spire` namespace, so the server credential and role password cannot drift.

## The password MUST be URL-safe

SPIRE builds a libpq connection string. As with EmberVM, a password containing
URL delimiters can be parsed as part of the connection target. Generate
alphanumeric characters only:

```sh
LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40
```

## The 1Password item

Item `k8s-homelab/spire-db` needs exactly one field named `password`, holding
the URL-safe password generated above. It must land as Secret key `password`,
which the SPIRE server consumes from the `spire-db` Secret in the `spire`
namespace.

## (Re)create the CNPG secret

Requires a logged-in `op` CLI and a kubectl context on the cluster. Idempotent.

```sh
PW="$(op read 'op://k8s-homelab/spire-db/password')"
kubectl delete secret monolith-pg-spire -n monolith --ignore-not-found
kubectl create secret generic monolith-pg-spire -n monolith \
  --type=kubernetes.io/basic-auth \
  --from-literal=username=spire \
  --from-literal=password="$PW"
kubectl label secret monolith-pg-spire -n monolith cnpg.io/reload=""
```

## Order of operations on first rollout

1. Create the 1Password item and run the block above. Do this first. Without
   the Secret, CNPG cannot set the role password, and without the role the
   `Database` CR cannot be created with `owner: spire`.
2. Enable `postgres.spire.enabled`, then let ArgoCD sync `monolith`. Confirm the
   role and database exist:
   ```sh
   kubectl get database monolith-pg-spire -n monolith
   kubectl exec -n monolith monolith-pg-1 -c postgres -- \
     psql -tAc "\l spire"
   ```
3. Let ArgoCD sync `spire`. The SPIRE server runs its own schema migrations on
   first boot.

## Rollback

Disable the SPIRE Application. Leave the monolith side enabled because the
database has `databaseReclaimPolicy: retain` and costs nothing while idle.
