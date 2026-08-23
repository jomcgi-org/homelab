# ADR 006: Crossing (the `moving` App) on `friends.jomcgi.dev` as a Second Authentik Authorization Lane

**Author:** Joe McGinley
**Status:** Accepted
**Created:** 2026-08-15
**Relates to:** Security 004: Public Read-Only Service Isolation

---

## Problem

Crossing, a family move-planner app (module and URL name `moving`), needs to be reachable by two people, Joe and Anna, and nobody else. Two existing hostnames were on the table and both fail the requirement for a different reason.

`private.jomcgi.dev` is gated by the Cloudflare Access lane: `cf-ingress.security-policy` (`projects/platform/cf-ingress-library/templates/_security-policy.tpl`) validates a Cloudflare Access JWT and projects `email` into `X-Auth-Email`. It carries no `authorization` block at all, so it authorizes anyone holding a valid `jomcgi` team session, full stop. There is no group concept on that lane. Narrowing one path on it to two people means either a path-scoped exclusion configured by hand in the Cloudflare dashboard, which moves authorization out of Git for that one path while everything else on the host stays Git-defined, or migrating the whole tier to authentik groups. Neither is a one-route change.

`friends.jomcgi.dev` already carries one route, `/preview/`, gated by an authentik OIDC + JWT SecurityPolicy scoped to the `homelab-admin` group (`projects/mcp/context-forge-gateway/chart/templates/httproute-preview.yaml`). That lane has the group concept the private tier lacks, but nobody in `homelab-admin` other than Joe should see a family move planner, so the existing route and its policy are the wrong shape for this audience too.

## Decision

Serve `/moving` and `/moving/*` from `friends.jomcgi.dev`, as a second HTTPRoute on that hostname with its own SecurityPolicy, gated to a new authentik group `family` (Joe and Anna) and to no other group. The app runs inside the existing monolith pod, the same SvelteKit and FastAPI processes that already serve `private.jomcgi.dev`, rather than a separate deployment.

This puts `/moving` on the correct side of an existing convention rather than inventing one. `httproute-preview.yaml` records that the two identity lanes are split by **audience, not by layer**: operator surfaces authenticate against Cloudflare Access, user surfaces against authentik, and user surfaces cost no Zero Trust seats. Crossing is a user surface with a non-operator user in it, so authentik is where it already belonged. Both lanes normalise onto the same downstream `X-Auth-Email` header, so no backend and no route match has to know which one authenticated the caller.

### Why a second route, not a rule on the existing one

A SecurityPolicy targets a whole HTTPRoute (`targetRefs` names an HTTPRoute, not a path match within one), so two authorization models on one hostname require two routes. `httproute-preview.yaml`'s own header comment states this as the reason `/.well-known/` and `/servers/` stay off that route entirely; `httproute-private.yaml` draws the identical line to keep the GitHub webhook off the Cloudflare Access policy. `/moving` follows the same pattern a third time: `/preview/` keeps its `homelab-admin` policy untouched, `/moving` gets its own policy scoped to `family`, and the two authorization models coexist on one hostname through disjoint path prefixes rather than through one policy trying to express both.

The route is rendered by the monolith chart into the `monolith` namespace, even though `/preview/` on the same hostname is rendered by the `mcp` namespace's chart. This works because the shared Gateway listener sets `allowedRoutes.namespaces.from: All`, and it is not a new pattern: `httproute-private.yaml`'s authentik-gated variant already models itself on the preview lane's mechanism (oidc plus jwt composing, `defaultAction: Deny`, claim-based not header-based authorization) while living in a different chart and namespace.

### Why the host, not the platform UIs, is the boundary that matters

The property "every route on this host sits behind an authentik token, scoped to the right group" is a property of a *host*, not of any one route's policy. On `friends.jomcgi.dev` that property holds by construction: the only other route on the hostname is `/preview/`, gated to `homelab-admin`, and nothing else has a route there at all, so a misconfigured `family` binding can at worst leak `/moving` itself.

On `private.jomcgi.dev` the same property does not hold, and cannot cheaply be made to hold. Five charts independently render HTTPRoutes on that hostname, each calling `cf-ingress.security-policy` on its own object: monolith, argocd (`/app/argocd`), signoz (`/app/signoz`), longhorn (`/app/longhorn`), kargo (`/app/kargo`). Two more routes on the same host deliberately carry no SecurityPolicy at all: the GitHub and Semgrep webhooks, gated only by HMAC verification in the handler, reachable because Cloudflare Access carries an IP-Bypass policy for their egress ranges. A `family` group added anywhere on that host is one policy-binding mistake away from ArgoCD, SigNoz, Longhorn, or Kargo, tools that reach the cluster's control plane. `friends.jomcgi.dev` exposes none of that: those surfaces have no route there to misconfigure into.

### Why the migration to authentik on private is deferred, not rejected

The mechanism is proven, not experimental: `monolith/dev/deploy/values.yaml` runs `auth: authentik` on `dev.jomcgi.dev` today, and it is what the private route's authentik branch in `httproute-private.yaml` was built and named for. Bringing every route on `private.jomcgi.dev` under authentik groups is a five-chart change plus a decision about what replaces the unpolicied webhook routes, none of which this app needs answered to ship.

Blast radius is the smaller of the two reasons to defer, though. The lane split is not inertia, it is a deliberate recovery constraint recorded in `httproute-preview.yaml`: operator surfaces stay on Cloudflare Access so that **authentik being down cannot lock the operator out of the tools used to fix authentik**. ArgoCD, SigNoz, Longhorn and Kargo are exactly those tools. A migration that moved them behind authentik without first establishing a break-glass path would convert an authentik outage from an inconvenience into an unrecoverable one, and no amount of care in the group bindings addresses that. So the open question a future migration must answer first is not "which charts" but "what does the operator use to log in when the identity provider is the thing that is broken."

It stays independently worth doing once that is answered: it would move private-tier authorization out of the Cloudflare dashboard and into Git, and it is also the fix for Context Forge's MCP tools, where Cloudflare Access consumes the `Authorization` header at the edge so no MCP caller today carries a real actor identity. Recorded here as a deferred option, not a rejected one; it is not this ADR's decision to make.

### Why the app rides in the existing monolith pod

ADR 004 split the public tier into its own artifact because that surface is anonymous and internet-facing, with no meaningful authorization boundary between "logged in" and "not." `/moving` is the opposite case: it is authenticated, small, and the isolation this decision relies on is enforced at the gateway, not inside the pod. The SecurityPolicy on `/moving` never routes a `family`-scoped request to any other path; that is the same argument `httproute-preview.yaml` makes for its own placeholder content, that path isolation at the gateway is the boundary, not process isolation behind it. A separate deployment was considered and rejected for now on cost: it would buy defense-in-depth for a data set that today is nothing more sensitive than family move logistics. The trigger to revisit is concrete: if the Crossing data set grows to hold anything whose exposure would be materially worse than that (financial account numbers, government ID data), split it out the way `monolith-public` was split out, on the same reasoning ADR 004 used.

### Identity vs authorization

The `groups` claim, verified inside the ID token's signature, answers "may this caller see `/moving` at all," the same `defaultAction: Deny` plus claim-matched `Allow` rule shape as `httproute-preview.yaml` and the private route's authentik branch. The `X-Auth-Email` header the jwt filter projects answers a different question, "whose view is this," and handlers may read it for that purpose only because the gateway-wide `ClientTrafficPolicy` in `projects/platform/cloudflare-gateway` strips any inbound `X-Auth-Email` at the listener before either auth filter runs; without that strip a caller could forge the header and have a backend believe it, since Envoy's jwt filter appends its projected value rather than replacing whatever arrived. `/moving`'s middleware resolves the header once to `viewer=joe|anna`; an email that is neither is a 403, never a default viewer, because falling through to a default viewer would silently show one person's plans to the other.

### Design system

Crossing gets a fourth scoped root class, `.moving`, with its own token block, rather than importing or forking `design-system.css` (public tier) or any other existing scoped system. `.impeccable.md` records that this repo runs several deliberately distinct, non-converging design systems, each scoped in CSS so they coexist on one origin; `/moving` follows that convention as a new named system rather than as a variant of an existing one, since it serves neither the public tier's skimming-evaluator audience nor an existing system's argument.

---

## Alternatives Considered

- **Path-scoped Cloudflare Access exclusion on `private.jomcgi.dev`.** Rejected: configured in the Cloudflare dashboard, not Git, so `/moving`'s authorization would live outside version control while every other route on the host stays declared in the chart.
- **Migrate all of `private.jomcgi.dev` to authentik groups now.** Deferred, not rejected. The mechanism is proven on `dev.jomcgi.dev`, but the operator surfaces on that host are deliberately on Cloudflare Access so an authentik outage cannot lock the operator out of the tools needed to fix authentik. A migration has to answer that first, and this app does not need it answered. See "Why the migration is deferred" above.
- **Serve `/moving` from `private.jomcgi.dev` with a new group added to that host's existing policy.** Rejected: the Cloudflare Access SecurityPolicy on that host has no `authorization` block, so a narrower group cannot be expressed on it without either the dashboard exclusion above or the full migration. Even granting that, the shared-hostname blast radius argument above still applies.
- **Separate deployment for `/moving`, mirroring `monolith-public`.** Rejected for now: that split earns its cost when a surface is anonymous or its data materially raises the stakes of a pod compromise. Neither is true of a two-person family app today. Recorded as the fallback if the data set grows into something that changes that calculus.
- **A rule appended to the existing `/preview/` HTTPRoute instead of a new route.** Not viable: a SecurityPolicy targets a whole HTTPRoute, so one route cannot carry two independent authorization models (`homelab-admin` for `/preview/`, `family` for `/moving`) without one silently taking priority or Envoy Gateway rejecting the ambiguous binding.

---

## Security

Builds on the `docs/security.md` baseline and on the preview lane's proven posture (`httproute-preview.yaml`).

- `friends.jomcgi.dev` has **no Cloudflare Access application in front of it**. The SecurityPolicy on `/moving`, like the one on `/preview/`, is the only gate between that path and the internet; the cloudflared tunnel's catch-all routes every resolving hostname to this Gateway. Traffic still arrives only through the Cloudflare tunnel, so the origin itself is not directly reachable, but the deny path (an unauthenticated request returning a redirect or 401, never a 200) must be confirmed live before the route serves anything real, the same ordered checklist `context-forge-gateway/deploy/values.yaml` used to arm `/preview/`.
- Cookie names are per-lane: `moving-access-token` / `moving-id-token`, distinct from `preview-access-token` / `preview-id-token` and from `dev-access-token` / `dev-id-token`. Two OIDC policies on one hostname sharing cookie names shadow each other; the failure mode is an infinite login loop, not a clean rejection, because a token that was issued and then rejected loops rather than 401ing.
- Cookie domain is scoped to `friends.jomcgi.dev`, never the apex; widening later is safe, narrowing later is not (a stale wider-domain cookie shadows the new one and presents as the same login loop).
- The OAuth callback and logout paths must fall inside the `/moving` prefix the route matches, or the request 404s before Envoy Gateway ever consults the policy.
- authentik derives the OIDC issuer from the **application slug**, not the provider name. Renaming the `moving` application later silently breaks token validation at the gateway with no error at the discovery endpoint, which still answers 200.
- Authorization gates on the `groups` claim inside the verified ID token, never on the projected `X-Auth-Email` header, for the same reason the preview and dev lanes do: a claim inside a verified signature cannot be forged by a caller, while a header's trustworthiness depends on the listener-level strip staying in place.
- The `family` group must never be added to any other application's policy binding. That single constraint is the entire boundary between "two people can see the move plan" and "two people can see everything `homelab-admin` can."
- authentik blueprints fail silently as a class: a bad `!Env` reference (for example a one-element `!Env [KEY]` sequence, which raises an uncaught `IndexError`) aborts discovery for the **entire** `/blueprints` tree, not just the file that has the mistake, while `/.well-known/openid-configuration` keeps answering 200. Blueprint changes for `moving` are tested against the preview lane first, exactly as `preview-auth.yaml` and `dev-auth.yaml` were, before touching prod authentik, which is pinned to `2026.8.0-rc7` on purpose.

---

## References

| Resource | Relevance |
| --- | --- |
| `projects/mcp/context-forge-gateway/chart/templates/httproute-preview.yaml` | Path-isolation pattern this ADR reuses a third time; source of the deny-path checklist |
| `projects/monolith/chart/templates/httproute-private.yaml` | The `auth: authentik` branch this route's SecurityPolicy is modelled on; the GitHub-webhook precedent for a second unpolicied route on one host |
| `projects/platform/cf-ingress-library/templates/_security-policy.tpl` | The Cloudflare Access lane, and why it cannot express a narrower group |
| `projects/platform/cloudflare-gateway/templates/client-traffic-policy.yaml` | Why `X-Auth-Email` is safe to read: the listener-level strip |
| `projects/platform/authentik/blueprints/preview-auth.yaml` | Blueprint conventions this app's blueprint follows (scalar `!Env`, pinned `client_id`, slug-derived issuer) |
| `projects/platform/authentik/blueprints/dev-auth.yaml` | Second precedent for the same blueprint conventions, on a stricter-group lane |
| `projects/monolith/dev/deploy/values.yaml` | Proven `auth: authentik` precedent this route's mechanism descends from |
| Security 004: Public Read-Only Service Isolation | The isolation-by-artifact argument this ADR declines to apply, and the trigger condition for revisiting that |
| `.impeccable.md` | Scoped, non-converging design systems convention `.moving` follows |
