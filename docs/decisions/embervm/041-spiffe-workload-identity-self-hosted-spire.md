# ADR 041: SPIFFE Workload Identity on Self-Hosted SPIRE

**Author:** Joe McGinley
**Status:** Accepted
**Created:** 2026-09-05
**Amends:** the deferral clauses in [ADR platform/012](../platform/012-cilium-replaces-linkerd.md) (its risk table: "Loss of per-workload mTLS identity weakens a trust property we rely on ... SPIFFE mutual auth available as follow-up if needed"), [ADR networking/003](../networking/003-cilium-capability-adoption.md) ("It does not adopt Cilium's SPIFFE mutual authentication. ADR 012 already deferred that; nothing here revisits it."), [ADR embervm/032](032-federated-identity-adapters-authentik-sso.md) ("a stable adapter seam for Okta, Entra ID, SPIFFE, or another issuer later"), and [ADR embervm/022](022-domain-composition-access-fabric.md) ("Explicitly out of the critical path: ... SPIFFE mesh"). Each of those ADRs' own decisions stand; only the named deferral is resolved. Also supersedes the mechanism (not the policy) of [ADR embervm/024](024-identity-hierarchy-templates-and-registration.md) decision 3's "Identity" half: the audience-scoped projected guest token becomes a SPIRE JWT-SVID rather than a Kubernetes TokenRequest-issued token; decision 3's Capability half (the platform holds the real credential and acts on the guest's behalf) is unchanged and is what decisions 6 and 7 below implement.
**Builds on:** [ADR embervm/001](001-embervm-beam-firecracker-workload-orchestrator.md) (principal as the isolation boundary), [ADR embervm/024](024-identity-hierarchy-templates-and-registration.md) (the identity-not-capability framing this ADR keeps), [ADR embervm/033](033-substrate-threat-model-conformance-encryption-at-rest.md) (the external threat model this closes two rows of), [ADR platform/009](../platform/009-post-merge-chart-versioning-kargo-promotion.md) (chart version write-back timing, unchanged by this ADR)

---

## Problem

EmberVM has three identity gaps that are the same gap seen from three angles.

**Guests have no identity.** A guest holds no cluster credential by
construction: no NIC, no mounted ServiceAccount
(`projects/embervm/ARCHITECTURE.md` section 5, invariant list, and section 9).
Section 9 lists "an audience-scoped projected guest token (audience
`embervm`)" as **Decided direction / Planned**, with no mechanism behind it.
The one lane that works today, monolith-agents over MCP, works by the egress
proxy fetching a broker-minted token on the guest's behalf
(`projects/firecracker/substrate/egress-proxy/cmd/swap.go`, `tokenBroker.token`
calling `/grants/<name>/token`), so the upstream sees the broker's identity,
never the guest's.

**The credential path authorizes by position, not by caller.** In
`projects/embervm/chart/templates/_noded-pod.tpl`, the egress-proxy
container's `EGRESS_LISTEN` environment comment says it plainly: "the loopback
bind is load-bearing: this sidecar has no client authentication, so it is the
only barrier to an arbitrary cluster workload using the credentialed response
path." Injection is keyed by destination host, the secret's `egressTo`, so
every guest on a brick that can reach the sidecar gets the same secrets.
Tracked as #5320 (STPA finding: shared egress allowlist has no per-workload
scoping).

**Platform transport auth is a static bearer.** CP-to-noded gRPC carries one
bearer token from a Kubernetes Secret, the same token and the same value for
every noded pod. `projects/embervm/proto/embervm/node/v1/node.proto` (lines
34 to 38) names this as "Auth v1" and reserves the upgrade: "The upgrade path
to mTLS or SPIFFE identities is additive: a new credential field on the
metadata, not a wire-breaking change to these messages." #5702 (splitting
`netd` out of noded) would otherwise invent its own per-brick token for the
netd-to-egress-proxy hop, and so would the egress proxy's own client-auth
task, each solving the same problem a third and fourth way.

This has been deferred three times as "follow-up if needed." ADR platform/012
consciously declined Cilium's SPIFFE mutual authentication when it replaced
Linkerd, on the grounds that the homelab threat model did not require
per-workload cryptographic authorization; ADR networking/003 repeated the
deferral without revisiting it. ADR embervm/032 left "a stable adapter seam
for Okta, Entra ID, SPIFFE, or another issuer later" without committing to
one. ADR embervm/022 put "SPIFFE mesh" explicitly outside its near-term
investment order. None of those three decisions is wrong on its own terms;
what has changed is that items 1 and 2 above are no longer speculative. The
monolith-agents MCP lane needs guest identity to close #5569 and #4462
properly, and #5320 is an open, labelled security finding, not a hypothetical.

A fourth, more specific decision also needs revisiting. ADR embervm/024
decision 3 already named the shape of a guest identity assertion: "an
audience-scoped projected token (audience `embervm`), valid only against
ember's own API," reusing "Kubernetes RBAC as the source of who-is-who
without granting any cluster access." That is a Kubernetes TokenRequest-style
token, cluster-signed, audience-restricted, and validated by Kubernetes
TokenReview. It is not what section 9 or this ADR builds. The policy ADR 024
decided (identity, not capability; the platform holds the credential; the
guest holds only an assertion) is exactly right and is what this ADR
implements; the mechanism it assumed is not, since no path exists to mint a
per-guest, per-principal Kubernetes ServiceAccount token for a guest with no
ServiceAccount at all. This ADR keeps ADR 024's policy and replaces its
mechanism with a SPIRE-issued JWT-SVID.

---

## Options

Ranked by implementation complexity, low to high.

**1. Extend the existing token broker to mint SPIFFE-shaped JWTs.**
`projects/embervm/tokenbroker` already mints and refreshes tokens for the
egress proxy; teaching it to sign a JWT with SPIFFE-shaped claims under its
own key is the cheapest option and adds no new component. Rejected: it has no
attestation, so anyone who can reach the broker's API is whoever they claim
to be, and it federates with nothing outside itself. It would also make the
broker a second root of trust standing next to whatever mechanism platform
mTLS eventually needs, which recreates the fragmentation this ADR exists to
close rather than closing it.

**2. Self-hosted SPIRE. Chosen.** A SPIRE server plus an agent DaemonSet,
`k8s_psat` node attestation, the `k8s` workload attestor for pods, and the
agent's Delegated Identity API for guests, with noded as an authorized
delegate. This is a real root of trust with attestation, one issuer for
platform components, guests, and (later) cloud credentials, and a clear path
to GCP workload identity federation through
`spire-oidc-discovery-provider`. Cost: one more stateful platform component,
an agent DaemonSet on every brick node, and an admin socket mounted into the
noded container until #5702 narrows what holds it.

**3. GKE managed workload identity certificates plus option 1 for guests.**
Google's Certificate Authority Service can issue SPIFFE-shaped certificates
for GKE-managed workload identity, covering platform components on the hub
for free. Rejected as the whole answer: it cannot attest a Firecracker guest
(there is no GKE-managed pod for a guest to be), it does not exist on the
on-prem cluster at all, and combining it with option 1 for guests still needs
a second issuer with no attestation for the harder half of the problem.

---

## Decision

**1. Trust domain.** `embervm.jomcgi.dev`, matching the enrollment label key
decided in #4696 (`embervm.jomcgi.dev/node`). One trust domain spans the GKE
hub and the on-prem cluster; the hub is the first deployment.

**2. SPIFFE ID scheme.** Platform components get
`spiffe://embervm.jomcgi.dev/ns/<namespace>/sa/<serviceaccount>`, the
`spire-controller-manager` default shape. Guests get
`spiffe://embervm.jomcgi.dev/ember/principal/<principal>`. Guest identity is
per principal, not per workload and not per lineage, for two reasons that
both come back to the same fact: SPIRE JWT-SVIDs carry no custom claims, and
`projects/embervm/ARCHITECTURE.md` section 9's hierarchy names Principal as
"THE isolation boundary," the only level invariant 3 (no mutable VM or
snapshot lineage ever crosses a principal) actually binds. Workload and
lineage stay control-plane facts carried in the op-log and in request
context; the SVID asserts who, never what or which instance.

**3. SVID types.** X.509-SVIDs for platform components: mTLS between the
control plane, noded, netd, the egress proxy, the token broker, and
monolith-agents. JWT-SVIDs for guests, so no private key ever lives in guest
RAM and a banked snapshot carries at most an about-to-expire bearer credential
rather than a signing key. Guest JWT-SVID TTL is 5 minutes, a number the
tests will assert; grep before changing it. Audiences are per consumer:
`egress-proxy` and `monolith-agents` now, `gcp-wif` in phase 5.

**4. Guest delivery.** noded is registered as an `authorized_delegates` entry
on its node's SPIRE agent, with the agent's admin socket hostPath-mounted
into the noded container. noded fetches JWT-SVIDs for the principal's entry
using the Delegated Identity API and a custom selector the delegate presents,
and delivers the SVID over vsock at guest boot and on every relight; the
guest's own agent re-requests on expiry. The SVID is never written to
scratch and never enters a snapshot on purpose. This is the "audience-scoped
projected guest token" ARCHITECTURE.md section 9 names as Planned, now with
a mechanism, and it is a different mechanism than ADR embervm/024 decision 3
assumed (see Problem): SPIRE and its Workload API replace the Kubernetes
TokenRequest path that decision described, because no ServiceAccount exists
for the guest to request a token as. #5702's planned split may eventually
move the delegate role off noded onto a narrower successor container; this
ADR names noded because that is where the role lives until that split lands.

**5. Registration entries.** Platform components register via
`ClusterSPIFFEID` objects from `spire-controller-manager`, keyed by namespace
and ServiceAccount, the same declarative shape every other platform
component in this repo uses. Guest principal entries are different: they are
reconciled by the EmberVM control plane itself, one entry per active
principal, because the principal set is a control-plane fact tracked in the
op-log, not a Kubernetes object, and there is no Kubernetes resource that
would already exist to hang a `ClusterSPIFFEID` selector off.

**6. Request-scoped authorization.** The egress proxy validates the guest's
JWT-SVID against the trust bundle fetched from the local agent's Workload
API, strips the SVID from the outbound request, and injects the stored
credential keyed by `(principal, host)` rather than by host alone. Every
audit line the proxy writes carries the SPIFFE ID. This closes #5320
structurally: two guests on the same brick can no longer receive the same
injected secret, because the proxy now knows which principal is asking. The
monolith-agents MCP lane validates the guest's JWT-SVID (audience
`monolith-agents`) in place of the broker's tier token, addressing #5569 and
letting #4462's grant selection key off the principal the SVID actually
names rather than off a shared broker credential.

**7. Platform mTLS.** CP-to-noded gRPC presents X.509-SVIDs as the additive
metadata credential `node.proto`'s Auth v1 header already reserves. The
existing bearer token is accepted alongside for one release, then retired.
#5702's netd-to-egress-proxy hop and the egress proxy's own client-auth task
(named but not built in that issue) use the same SVIDs instead of each
inventing a separate per-brick token, which was the third and fourth
reinvention this ADR is written to prevent. See the 2026-09-05 amendment
below for how this hop's credentials are actually delivered, staged, and
sequenced.

**8. Cloud federation.** `spire-oidc-discovery-provider` is exposed on a
public HTTPS URL so GCP can fetch its JWKS from outside the cluster;
`docs/runbooks/public-tier-checklist.md` applies to that surface. A GCP
workload identity pool and provider are configured for the trust domain.
noded's S3 client exchanges a JWT-SVID for short-lived GCS access tokens,
retiring the static keys currently held in the `store` 1Password item.

**9. Deployment shape.** `projects/platform/spire` wraps the upstream
`spiffe/helm-charts-hardened` chart as a `Chart.yaml` dependency, the same
pattern `projects/platform/cert-manager` uses for the jetstack chart: one
chart directory, `values.yaml` as the shared base, and a `values-gke.yaml`
overlay layered on top via the Argo CD Application's `helm.valueFiles`. That
Application, not a separate chart copy, is the twin:
`projects/platform-gke/spire/application.yaml` (plus a `kustomization.yaml`
entry into the GKE root) points at the same `projects/platform/spire` path
with `values.yaml` plus `values-gke.yaml`, exactly as
`projects/platform-gke/cert-manager/application.yaml` does today for
cert-manager. There is one chart, two Applications, two value overlays; no
`projects/platform-gke/spire/` directory holds its own chart. The server's
datastore is its own database on the existing `monolith-pg` CloudNativePG
cluster, the same shape as the op-log
(`projects/embervm/deploy/values.yaml`, `opLog.postgres` block: its own
database on a shared cluster, starting empty, no migration). The agent
DaemonSet runs on every node and tolerates the brick taint from #4696: brick
nodes for phase 3's guest delegation, the core pool because phase 2's
platform components (control plane, token broker, monolith-agents) reach the
Workload API through the SPIFFE CSI driver on their own node. The OIDC
discovery provider stays off until phase 5.

**10. Phasing.** Phase 0 is this ADR. Phase 1 deploys SPIRE with no consumer.
Phase 2 issues platform SVIDs and moves CP-to-noded auth to mTLS. Phase 3
delivers guest identity. Phase 4 makes authorization request-scoped at the
egress proxy and the monolith-agents lane. Phase 5 federates to GCP. Each
phase ships as its own PR set and is verified live in dev before the next
begins; the phase plan and its per-phase verify steps live in #5706, not
here. No PR in any phase touches `Chart.yaml` `version:` or
`targetRevision:` (ADR platform/009); platform charts deploy off `HEAD`.

---

## Non-goals

**Isolating the credential parser.** A compromised egress proxy still holds
every model-provider key it was ever configured with; SPIFFE identifies the
caller, it does not shrink the blast radius of the proxy process itself.
Splitting the proxy into a per-principal pod is a separate decision, not
this one.

**Making third-party APIs speak SPIFFE.** They do not and will not.
Credential injection at the egress hop stays exactly as it is; SPIFFE only
changes who the proxy decides is entitled to which injected credential.

**Cilium mutual authentication.** The GKE hub runs GKE Dataplane V2, which
carries no Cilium CRDs, so Cilium's own SPIFFE mutual-auth feature is not
available there regardless of this decision. It is not adopted, on the hub
or elsewhere.

**Revocation faster than SVID TTL.** Short TTLs (5 minutes for guest
JWT-SVIDs) are the control, matching the class 1 credential rule already in
`projects/embervm/ARCHITECTURE.md` section 9: revocation at the validator,
by expiry, not an active-revocation channel. Nothing here builds one.

---

## Consequences

What becomes possible:

- One issuer covers guest identity, platform transport identity, and (from
  phase 5) cloud credential exchange, where today there are three separate,
  partial, or absent mechanisms.
- #5320 is closed by construction rather than by a scoping convention that
  has to be maintained: the egress proxy cannot inject principal A's
  credential for principal B's request once it can tell them apart.
- The upgrade path `node.proto`'s Auth v1 header names is actually used,
  rather than staying a comment for another year.
- ARCHITECTURE.md section 10's threat rows 4 and 10 (mutual authentication
  and encrypted transport between components; envelope encryption and
  granular admin access) move from "one shared bearer Secret" toward mTLS,
  and the "Planned: mTLS/SPIFFE as the noded transport-auth upgrade" line
  becomes a decided mechanism with a phase plan.

What this costs, accepted:

- A new stateful platform component (SPIRE server, its own Postgres
  database, an agent DaemonSet) that has to be operated, backed up, and
  reasoned about during an outage, on top of everything else the brick
  fleet already carries.
- An admin socket lives in the privileged noded container from phase 3
  onward, narrowing only as far as #5702's container split gets before it
  lands; until then, a noded compromise is also a SPIRE delegate compromise.
- SVID TTL bounds how fast anything can be revoked; a 5-minute guest token
  is a 5-minute window in the worst case, not zero.
- `spire-oidc-discovery-provider` is a new public HTTPS surface from phase
  5 on, subject to the public-tier checklist and everything that implies.
- On-prem parity lags the hub by construction: the hub is the first
  deployment, and the on-prem cluster gets phase 1 onward only once the hub
  proves it out.

---

## Amendment 2026-09-05: phase 2 credential delivery and sequencing

Records the design review on #5706 that settled how decision 7's CP-to-noded
mTLS is actually delivered and sequenced, three points decision 7's original
text left implicit.

**Delivery: files, not a client library.** Decision 7 stands: the hop is
symmetric X.509-SVID mTLS. What was undecided is how the credential reaches
Erlang's `:ssl`. It arrives as files, written by a `spiffe-helper` sidecar in
the SPIFFE Filesystem Delivery layout (`svid.pem`, `svid_key.pem`,
`svid_bundle.pem`) to a shared `emptyDir`; the control plane passes file
paths, read at dial time, to `:ssl`'s built-in credential and verification
options rather than caching bytes in application state. No SPIFFE client
library is added on the Elixir side: this repo vendors every hex package by
sha256 with no `mix.lock`, so a new dependency means enumerating its entire
transitive closure, and the built-in `:ssl` plus `:public_key` already do
everything decision 7 needs. Peer identity is an exact match on the URI SAN
against the configured noded SPIFFE ID, not a prefix or trust-domain check.

**Why files, and why this is not "wait for Kubernetes to do it."** Kubernetes
1.37 (GA 2026-08-26) made `PodCertificateRequest`, the `podCertificate`
projected volume, and `ClusterTrustBundle` generally available. They are the
intended future *delivery* mechanism for platform-pod X.509: a
`podCertificate` volume can replace the `spiffe-helper` sidecar with no
change to the code that reads the files, once a SPIRE-backed signer for
`PodCertificateRequest` exists. They are not a replacement for SPIRE in this
program, for three reasons that all hold today: core Kubernetes ships no
signer on its own, the mechanism covers pods only, so it has nothing for the
Firecracker guests phases 3 and 4 need, and it is X.509 only, so it has
nothing to offer phase 5's JWT-SVID and OIDC federation. GKE's channels at
decision time: the hub on 1.35.7 regular, rapid at 1.36.3, 1.37 available
only as alpha-cluster previews, none of them usable for this cluster. Revisit
when the hub's channel carries 1.37 and a production SPIRE-backed
`PodCertificateRequest` signer exists; at that point the sidecar is a swap,
not a redesign, because the file layout was chosen for exactly this.

**Rotation is discipline, not a subsystem.** An established TLS connection
has no reason to re-read a rotated certificate, so correctness rests on two
things instead of a channel-invalidation mechanism: the control plane reads
the credential files fresh at every dial, and noded bounds connection
lifetime with gRPC `MaxConnectionAge` (1 hour, 5 minute grace) so a
long-lived channel is forced to re-handshake and pick up whatever is
currently on disk. This is the same discipline a `podCertificate` volume
would require regardless, so it is not a cost specific to the sidecar.

**Rejected.** Asymmetric X.509 server auth plus a JWT-SVID minted by the
control plane as its client credential: cheaper to build now and sidesteps
rotation handling entirely, but a JWT-SVID is exactly the credential
Kubernetes Pod Certificates can never deliver, so choosing it here would keep
a SPIRE client dependency on the Elixir side permanently, for one hop,
forever. Kubernetes bound service-account tokens as the control plane's
credential: native to the platform, but it introduces a second identity
vocabulary for one hop and requires noded to carry TokenReview RBAC it has no
other reason to hold. Envoy sidecars with SDS: no application changes on
either side, but it means an Envoy per brick pod and per control-plane pod,
listeners bound to localhost, and a network-policy rework, more footprint
than one gRPC hop justifies.

**Second listeners, not in-place flips.** Decision 7's original text, "the
existing bearer token is accepted alongside for one release," assumed one
flag could stage both sides of the hop together. It cannot: the chart gates
both sides of the bearer from a single value on purpose, so staging needs two
independent flags. noded instead serves mTLS on a second gRPC port (9443,
`noded.spiffe.grpcTlsPort`) alongside its existing plaintext bearer listener,
and the token broker serves `/token` only on a second, mTLS-only port
(8443); each is its own chart flag, so the control plane can move client by
client (egress-proxy to the broker, then itself to noded) and the plaintext
listeners are the last thing removed, not removed by the same PR that adds
the new ones. Read "accepted alongside" in decision 7 as two independently
staged flags, not one combined switch.

**The token broker's client auth is phase 2 work, not a nicety.** The
broker's `CiliumNetworkPolicy` is its only access control today and it is
not rendered on the GKE hub, so any pod that can reach it can fetch any
grant's live token; closing that gap is why #5755 sits in this phase rather
than later. One consumer is intentionally left out for now: the monolith's
device-code login proxy keeps using the plaintext port for `/login/*` and
`/refresh` until the monolith itself carries an SVID, a follow-up rather than
a blocker for this phase.

**The restore-capability key stops piggybacking on the bearer.**
`restore_capability.ex` defaults its HMAC key to `:noded_bearer_token`, so
retiring the bearer as decision 7 plans would silently break every
enveloped restore under `requireRestoreCapability: true`. #5756 gives it its
own secret before the bearer retires, not after.

**Sequencing.** Hop-independent pieces land first: `ClusterSPIFFEID`
registrations for the platform components (#5754), broker mTLS (#5755), and
the restore-capability key (#5756). Then the two sides of the CP-to-noded
hop: noded's listener (#5757), then the control plane's dial (#5758). Then
the hub value flips (#5759), one flag per PR, each verified live before the
next. The order is fixed rather than incidental: each step is independently
revertible only as long as nothing after it has shipped.

---

## References

| Resource | Relevance |
| -------- | --------- |
| `projects/embervm/ARCHITECTURE.md` section 9 | Guest identity's "audience-scoped projected guest token," Decided direction / Planned, the gap this ADR gives a mechanism |
| `projects/embervm/ARCHITECTURE.md` section 10 | Threat rows 4 and 10 (transport mutual auth, envelope encryption/admin access), moved from bearer/Planned toward SVID-based mTLS |
| `projects/firecracker/substrate/egress-proxy/cmd/swap.go` | `tokenBroker`, the broker-minted-token pattern the monolith-agents MCP lane uses today, and the injection point decision 6 changes |
| `projects/embervm/chart/templates/_noded-pod.tpl` | The `EGRESS_LISTEN` comment naming the loopback bind as the sidecar's only client authentication, the gap #5320 tracks and decision 6 closes |
| `projects/embervm/proto/embervm/node/v1/node.proto` (lines 34-38) | Auth v1's static bearer token and its reserved additive upgrade to mTLS or SPIFFE, which decision 7 exercises |
| `projects/embervm/deploy/values.yaml` (`opLog.postgres` block) | The shared-cluster, own-database, starts-empty shape decision 9 reuses for SPIRE's datastore |
| `projects/platform/cert-manager/`, `projects/platform-gke/cert-manager/application.yaml` | The one-chart, two-Application, layered-`valueFiles` deployment pattern decision 9 follows exactly |
| #5706 | Umbrella issue: the phase plan, per-phase verify steps, and cross-refs this ADR does not repeat |
| #5702 | netd split out of noded; its client-auth and netd-to-proxy tasks adopt the SVIDs from decision 7 |
| #5703 | Per-tap policy, depends on #5702 |
| #5320 | Shared egress allowlist has no per-workload scoping; closed structurally by decision 6 |
| #4462 | Per-user credential brokering for agent sessions; its grant selection keys off the principal decision 6 names |
| #5569 | MCP client and session-bound token for guests; the consumer decision 6's monolith-agents change serves |
| #4691 | Per-principal envelope encryption and verified restore; the tuple-scoped capability model an SVID-scoped grant could extend later, not built here |
| #4696 | Node enrollment consolidated on `embervm.jomcgi.dev/node`, the label decision 1's trust domain reuses and the taint decision 9's agent DaemonSet tolerates |
| [ADR embervm/024](024-identity-hierarchy-templates-and-registration.md) | Decision 3's identity-not-capability policy, kept; its Kubernetes-TokenRequest mechanism, superseded by decision 4 |
| [ADR platform/012](../platform/012-cilium-replaces-linkerd.md), [ADR networking/003](../networking/003-cilium-capability-adoption.md) | The two prior SPIFFE-mutual-auth deferrals this ADR resolves for EmberVM specifically; Cilium's own feature stays undecided outside this scope |
| [ADR embervm/022](022-domain-composition-access-fabric.md) | "SPIFFE mesh" named as out of the near-term critical path; resolved here for the identity half, not for a full mesh |
| [ADR platform/009](../platform/009-post-merge-chart-versioning-kargo-promotion.md) | Chart version write-back timing; no phase of this ADR's rollout touches `version:` or `targetRevision:` |
