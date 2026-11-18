# authentik

This deploys the official authentik Helm chart with a separately managed
CloudNativePG database, as decided by ADR 032.

The server and worker run a private overlay of `ghcr.io/goauthentik/server`
from `ghcr.io/jomcgi/authentik-fork`, which swaps the enterprise license CA
for a lab-local one. The pull secret is the existing
`vaults/k8s-homelab/items/ghcr-read-permissions` item, mirrored as
`ghcr-imagepull-secret` in this namespace. After the overlay is live, paste
the license JWT (local file
`~/repos/authentik-fork/secrets/authentik.license`) in the admin UI under
Enterprise, Licenses, Install.

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
`https://auth.jomcgi.dev`. Blueprints define separate OAuth2 providers and
applications for `dev`, `kargo`, `preview`, `moving` and `grimoire`. The MCP
blueprint updates the existing `mcp-friends` provider's mappings and application
gate, and creates the `mcp-agents` client-credentials provider and application.
The browser lanes use their own policy bindings; the agent lane is bound to
`kg-agents`.

Grimoire's provider and invitation-required enrollment flow are included by
default. `grimoireLinkEnrollment` gates the separate campaign-link flow;
`platformManagement` is enabled by default and adds protected profile claims.
The optional platform enrollment flow was retired in #7075. Its former shared
Grimoire profile mapping remains management-owned with the same object identity.

`grimoireDiscordSource` (off in the base values, on in the hub overlay since
2026-11-12, #7461) splices a Discord OAuth source,
a verified-email enrollment gate and a Grimoire-only sign-in flow into the
Grimoire blueprint, and points the Grimoire provider at that flow. Every other
application keeps the shared default login. It reads `client-id` and
`client-secret` from the 1Password item at `grimoireDiscordSource.itemPath`,
`vaults/k8s-homelab/items/discord-bosun`: the shared Bosun Discord
application, whose redirect is
`https://auth.jomcgi.dev/source/oauth/callback/discord/`. Discord identities
match by account id only, never by email, and create external users under the
`grimoire` path with no group.

With the flag off, the Grimoire blueprint contains only a splice-marker
comment: no Discord source, OnePasswordItem or environment reference is
rendered. The hub overlay gives the server and worker required references to
the `grimoire-discord-oauth` Secret, so a pod waits for the Secret and the
change rolls the pods; a newly created Secret alone would not restart them.
Joe registered the application, provisioned the credentials and approved
enablement on 2026-11-12. Any Discord account with a verified email can now
enrol, and campaign creation is open to every account while platform
enforcement is off. Disposable-account checks of the live flow, campaign-creation
restrictions and the invitation/platform-auth decisions remain in #7461.

The source contract was checked against Authentik's pinned
[`version/2026.5.6`](https://github.com/goauthentik/authentik/tree/version/2026.5.6):
`authentik/sources/oauth/views/callback.py` supplies `info` to property mappings
and `oauth_userinfo` to the flow policy context; `core/sources/flow_manager.py`
carries that context into enrollment. `sources/oauth/types/discord.py` requests
`email` and `identify`. `stages/identification/stage.py` validates the embedded
password against the selected stage's backends. These source checks and real-Helm
render tests do not establish a working Discord sign-in; disposable-account
validation of the live flow is still outstanding.

authentik serves discovery per application at
`/application/o/<slug>/.well-known/openid-configuration`. Ember's browser SSO
remains intentionally unconfigured under ADR 032.

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

### Roll back factory explorer sign-in

1. Revert the addition of `factory-blueprints/factory-explore.yaml` and sync
   the factory Authentik release. Confirm that `factory-explore.yaml` is gone
   from the `authentik-mcp-blueprints` ConfigMap and the worker's mounted
   blueprints. This prevents the worker from recreating objects during cleanup.
2. Start the [factory Authentik port-forward](../../factory-cluster/README.md#authentik-first-run)
   and keep it running during cleanup. Sign in as an administrator at
   `http://localhost:9000/if/admin/`, then
   open **Applications → Applications** and delete **Factory explorer** (slug
   `factory-explore`). Under **Applications → Providers**, delete the OAuth2
   provider with client ID `factory-explore` if it is still present. Refresh
   both lists and confirm both entries are gone.
3. In a browser signed in to factory Authentik, open this authorization request:

   ```text
   https://factory-auth.jomcgi.dev/application/o/authorize/?client_id=factory-explore&response_type=code&scope=openid&redirect_uri=https%3A%2F%2Ffactory-explore.jomcgi.dev%2Foidc%2Fcallback&state=rollback-check&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM&code_challenge_method=S256
   ```

   Confirm Authentik rejects the unknown client and issues no callback with an
   authorization code. A login prompt or network failure is inconclusive.
   Previously issued ID tokens can remain valid until their 10-minute expiry.

### Factory social sign-in rollback

Revert the factory social configuration and wait for the worker to reconcile
the reverted ConfigMap. Using a local administrator account through the admin
port-forward, edit **Flows and Stages → Stages → default-authentication-identification**.
Remove GitHub, Google and Discord from Sources and save. Under **Directory →
Federation and Social login**, disable each of the three sources. Keeping the
disabled source records preserves existing account connections. Check that
the local sign-in page offers password login and the three source redirects
are unavailable before closing the administrator session.

### Factory social sign-in validation

In the pinned release, the source flow manager routes a new identity to
enrollment and an existing connection to authentication. Enrollment creates the account and saves its provider connection without issuing a session.
Direct signup returns to the same provider to begin authentication. The validate stage challenges
an enrolled device and requires TOTP setup when there is no device.
[Source flow routing](https://github.com/goauthentik/authentik/blob/version/2026.5.6/authentik/core/sources/flow_manager.py#L151-L170)
and [MFA stage behavior](https://github.com/goauthentik/authentik/blob/version/2026.5.6/authentik/stages/authenticator_validate/stage.py#L259-L291).

The factory_resources_test target checks the rendered stage order, executes
the GitHub mapping and duplicate-email gate, and checks the application grants.
A new external user receives no group. The edge application requires
`authentik Admins`. The kubectl application requires that group or the viewers
group before the client can obtain a token.

The server contacts GitHub, Google and Discord for tokens and profiles.
The worker needs accounts.google.com and token.actions.githubusercontent.com
for discovery, plus www.googleapis.com and token.actions.githubusercontent.com
for signing keys. The namespace policy scopes both lists to TCP 443.
[Source serializer](https://github.com/goauthentik/authentik/blob/version/2026.5.6/authentik/sources/oauth/api/source.py#L60-L127).

Required PR checks run the flow executor check when the factory Authentik
blueprints, values or runtime fixture change. To run it locally with Docker
Compose and yq, use the following command from the repository root.

```bash
projects/platform/authentik/runtime/check.sh
```
