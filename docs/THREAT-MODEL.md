# Homelab Threat Model

_@ 809561c47_

The aggregate security view, run the way a company would run it: what we
protect, where untrusted input lands, and the open findings ranked with
enough context to judge them. Per-domain models own the detail; this
page owns the ranking and the links. The live list of open findings is
the [`security-finding` label](https://github.com/jomcgi-org/homelab/issues?q=label%3Asecurity-finding+is%3Aopen);
closing the issue is how a finding leaves this page. ADR
[security/007](decisions/security/007-aggregate-threat-model-index.md)
records the framework.

## 1. What we protect

| Asset | Where it lives | Who wants it |
| --- | --- | --- |
| Credentials held for workloads: model-provider keys, git tokens, Cloudflare service tokens | 1Password Operator, the EmberVM egress sidecar | Prompt-injected agents, anyone who lands code in the cluster |
| Personal data: knowledge graph, notes, calendar, chat history | The monolith's Postgres | Anyone past the private-tier identity wall |
| Tenant state: memory snapshots are the full process state of a workload | EmberVM's object store | A bucket reader, a compromised brick |
| The cluster itself: compute, `/dev/kvm`, residency for everything above | Every node | A guest escaping its VM, a malicious PR reaching CI |
| Public-site integrity: jomcgi.dev says what Joe published | The public tier | Anyone who can turn a public route into a write |

## 2. Surfaces

One row per place untrusted or lower-trust input arrives. Deep model
means a colocated `THREAT-MODEL.md` in the embervm shape: adversary
tables, honest status labels, a ranked residual section.

| Surface | Untrusted input | Baseline controls | Deep model |
| --- | --- | --- | --- |
| Public tier: jomcgi.dev pages, monolith-public, `/app/*` | Anonymous internet, through Cloudflare | WAF, `public_reader` data class, [public-tier checklist](runbooks/public-tier-checklist.md), ADRs security/004 and 005 | None yet |
| Private tier identity: `private.jomcgi.dev`, Cloudflare Access, authentik | Authenticated humans, service tokens | CF Access apps, authentik flows | None yet |
| MCP gateway: Context Forge fronting the monolith and GitHub | Agent callers, including prompt-injected ones | Caller-identity tool distribution at the gateway | None yet; #4569 and #4940 are the open work |
| EmberVM substrate: arbitrary code execution as the product | Guest code, tenant principals | Firecracker, no-NIC guests, brokered egress, envelope encryption | [projects/embervm/THREAT-MODEL.md](../projects/embervm/THREAT-MODEL.md) |
| Supply chain and CI: BuildBuddy runners, GitHub Apps, merge queue, chart publish | PRs, dependencies, base images | Branch protection, merge queue, Renovate pins | None yet |
| Cluster platform: k3s, Cilium, ArgoCD, Kyverno, 1Password Operator | Everything above sits on it | [docs/security.md](security.md) owns the five layers | [security.md](security.md) |

## 3. Top open findings

Ranked by what an attacker gets. Every entry is an open issue carrying
the `security-finding` label; the issue holds the detail and the fix
history.

1. **93 Semgrep rules enforce nothing** ([#4777](https://github.com/jomcgi-org/homelab/issues/4777)).
   Every rule target exits 0 without scanning. The repo's written
   controls for hardcoded digests, service URLs, and secret handling are
   advisory until this is fixed, on every surface at once.
2. **A Firecracker escape is not contained** ([#5255](https://github.com/jomcgi-org/homelab/issues/5255)).
   noded execs the VMM as root in a privileged pod with no jailer, so an
   escape hands over the brick: `/dev/kvm`, the fleet store credential,
   the noded bearer token. Finding 1 of the
   [embervm model](../projects/embervm/THREAT-MODEL.md).
3. **The monolith does not validate the token the gateway forwards**
   ([#4569](https://github.com/jomcgi-org/homelab/issues/4569),
   [#4940](https://github.com/jomcgi-org/homelab/issues/4940)). Any
   caller Context Forge lets through acts with the monolith's full
   authority; tool tiers exist as configuration, and the monolith
   enforces none of it.
4. **The store credential can still destroy any principal's artifacts**
   ([#4691](https://github.com/jomcgi-org/homelab/issues/4691)).
   Envelope encryption and the restore capability closed read and
   restore; write, delete, and list stay bucket-wide, customer-managed
   KEK custody and digest-verified manifests stay designs.
5. **Kargo promotes to production on stage ordering alone**
   ([#4745](https://github.com/jomcgi-org/homelab/issues/4745)). No
   verification gate sits between a dev artifact and prod, so anything
   that reaches the dev chart repo promotes.
6. **Agent spend has no enforced ceiling**
   ([#4784](https://github.com/jomcgi-org/homelab/issues/4784)).
   `budget_usd` is recorded and reported; a runaway or malicious
   principal is bounded by admission caps and concurrency, never by
   cost.

## 4. Maintenance

- An open security residual gets the `security-finding` label when it is
  found. This page ranks the top of that list and re-ranks by hand when
  a labeled finding opens or closes.
- A new per-domain model lands at `projects/<domain>/THREAT-MODEL.md`
  when its surface starts taking untrusted input that the baseline
  controls do not describe. The MCP gateway and the public tier are the
  next two.
- Stamp the commit as a line under the H1, the same convention the
  embervm model uses.
