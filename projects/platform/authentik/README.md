# authentik

This deploys the official authentik Helm chart with a separately managed
CloudNativePG database, as decided by ADR 032.

Before ArgoCD can make the server ready, create the 1Password item at
`vaults/k8s-homelab/items/authentik`. It must contain a field named
`AUTHENTIK_SECRET_KEY` with a generated, durable value, plus
`AUTHENTIK_BOOTSTRAP_PASSWORD` and `AUTHENTIK_BOOTSTRAP_EMAIL`. The item is
mirrored by the 1Password Operator as `authentik-secrets` in the `authentik`
namespace.

Create the item **before** the Application first syncs. The bootstrap
credentials are read only by the one-shot `0003_default_user` migration, at
akadmin creation. Add them later and they are inert for that database: akadmin
keeps an unusable password, and the out-of-box setup flow stays open, offering
an admin-password form to any anonymous visitor. Recovering an
already-migrated instance means setting the password by hand:

```bash
kubectl exec -n authentik deploy/authentik-server -- ak shell -c "
import os
from authentik.core.models import User
u = User.objects.get(username='akadmin')
u.set_password(os.environ['AUTHENTIK_BOOTSTRAP_PASSWORD'])
u.save()"
```

The `operator.1password.io/auto-restart` annotation on the `OnePasswordItem`
matters here: the operator runs `AUTO_RESTART=false` by default, and both pods
read the secret through `envFrom`, which is evaluated only at container start.
Without the annotation, rotating any of these fields updates the Secret and
changes nothing in the running process, silently.

The database password is generated and managed by CloudNativePG through the
`authentik-pg-app` Secret. The authentik chart reads that password by reference,
so it is never committed to this repository.

The Gateway API route publishes the Authentik login interface at
`https://auth.jomcgi.dev`. No OIDC provider is configured yet, so there is no
discovery document to fetch. authentik serves discovery per application at
`/application/o/<slug>/.well-known/openid-configuration`, which starts
answering once a provider exists. This deployment does not create an Ember
OIDC provider or modify Ember's authentication configuration.

Friend applications should use separate HTTPRoutes with Envoy OIDC policies.
Do not attach those policies to the Authentik route itself or the login flow
will recurse.

Two properties of the published surface are worth knowing before changing it.
Cloudflare Access covers `auth.jomcgi.dev/if/admin*`, which is the admin
console, not the admin API: `/api/v3` is authorized by authentik session or
token, so MFA on the account is the control that matters, not the edge path
rule. And the login flow and the setup flow share the
`/api/v3/flows/executor/` prefix, so the login endpoint cannot be gated by
path without breaking authentication for everyone.

authentik ships no CRDs. Declarative configuration is done with blueprints,
mounted through `blueprints.configMaps` or `blueprints.secrets` and applied by
the worker, which is the GitOps path for adding providers and applications.
