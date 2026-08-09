# ADR 047: Per-Principal Egress Credentials and the Broker Identity Envelope

**Author:** jomcgi
**Status:** Draft
**Created:** 2026-07-27
**Revised:** 2026-08-09 (delegation model, grant sets, and machine-principal identity decided; see Update below)
**Amends:** [023 - Egress Secret Proxy for Agent Sandboxes](023-egress-secret-proxy.md) (decision 6's credential provisioning, decision 3's env-placeholder primitive already superseded in code by header injection, and the risk table's "per-node shared catalog" row)

---

## Problem

EmberVM agent sessions run the Claude Code CLI inside Firecracker microVMs. Today one shared Anthropic subscription credential lives in the egress-proxy sidecar and is injected into every guest request bound for `api.anthropic.com` (`projects/embervm/deploy/values.yaml:279-292`, mechanism in `projects/firecracker/substrate/egress-proxy/cmd/swap.go`, shipped in PR #4100). The guest never holds it: the sidecar sets the `Authorization` header on the way out and discards whatever the guest sent there.

That solves exfiltration and nothing else. Two independent frontier-model reviews reached the same conclusion separately: keeping the credential out of the guest stops a prompt-injected agent from stealing a token it can spend later, outside the session, but it does nothing about spend. Any request the guest sends to a covered host gets the credential attached, so a hostile prompt can exhaust the whole allowance through turns that look entirely legitimate. `projects/embervm/ARCHITECTURE.md` already commits this repo to a specific answer for that class of problem: metering is counting, not enforcement, and fails open by design, and cutting off a principal is an admission action, not a metering one. That posture only holds if spend is self-limiting per principal. A shared credential is the opposite: one careless or compromised session burns an allowance every other session is drawing from, with no platform-side cap to catch it, because none is meant to exist.

The credential is also load-bearing for something outside EmberVM. `projects/embervm/deploy/values.yaml:288-289` states it plainly: the 1Password item is "shared with the monolith gardener, which reads the same item." Revoking the one credential the sidecar holds takes out an unrelated service.

The catalog cannot currently tell principals apart even if it wanted to. `secretFor` in `swap.go` matches a connection's destination host against each entry's `EgressTo` list and returns the first match; there is no caller identity in scope at all, so a second entry with the same `egressTo` would be dead code, silently shadowed by whichever entry `loadSecrets` happened to build first.

---

## Decision

**Move to per-principal credentials, and make the egress-proxy sidecar an explicit credential broker keyed on caller identity, rather than a proxy holding one static shared secret.**

| Aspect | Today | Decided |
| --- | --- | --- |
| Credential scope | one shared token, used by every EmberVM guest and the monolith gardener | one credential per principal |
| Abuse containment | none; a hostile prompt spends the shared allowance | self-contained; a principal can only spend its own credential |
| Catalog selection key | destination host only (`secretFor(host)`), first match wins | (principal, host); `loadSecrets` rejects a duplicate (principal, host) |
| Caller identity on the sidecar connection | none | an identity envelope noded frames before the guest's own bytes, unforgeable by the guest |
| Credential storage | one `secretRef` per catalog entry resolved into an individually-named sidecar env var, so a new credential is a pod-spec change | one catalog Secret with N keys behind a `credentialResolver` interface, membership changes stay in the mounted Secret |
| Missing credential for a principal | not reachable today (one credential covers everyone) | DENY, never a cleartext tunnel |
| Platform-side spend cap | none | still none, deliberately: per-principal credentials make one unnecessary |

**1. Per-principal credentials dissolve the abuse problem instead of mitigating it.** If each principal supplies its own credential, quota burn is self-inflicted. This is why a rate limiter or spend cap on `api.anthropic.com` is explicitly not being built: PR #4100 already named and rejected that shape ("Path allowlisting or rate limiting on api.anthropic.com: does not change abuse economics, since legitimate turns burn quota too. That control belongs to the Discord feature ACL over who may start a session."), and this ADR is the change that makes the rejection correct rather than merely convenient. It is consistent with `ARCHITECTURE.md`'s existing statement that metering fails open by design and that cutting off a principal is an admission action, not a metering one: nothing here adds enforcement, it removes the shared blast radius enforcement would otherwise have to cover.

**2. AWS Lambda is the reference model, and it is instructive because it does the opposite of concealment.** Lambda runs on Firecracker, the same isolation boundary this repo builds on, and hands the function's execution-role credentials to the guest as plain environment variables. What makes that safe is four properties of the credential, not of the sandbox: short-lived, scoped, attributable, and independently revocable. Lambda's abuse control is separate and explicit (timeouts, concurrency limits, budget alarms), not concealment. SnapStart, Lambda's Firecracker snapshot-restore feature, hit the same snapshot-persistence hazard this repo records for its own memory snapshots, and AWS answered it with rotate-after-restore hooks rather than concealment. EmberVM cannot simply copy this because an Anthropic subscription OAuth token has none of the four properties: it is long-lived, unscoped to one principal, unattributable on the wire, and, per the Problem section, shared with the monolith gardener through the same 1Password item. **The injection apparatus built in PR #4100 exists to compensate for a credential that lacks the properties AWS's model presumes.** Giving each principal their own credential is the more direct fix; it does not remove the need for injection (see 3), but it removes the reason concealment is the only lever available.

**3. Concealment stays even under per-principal credentials.** Do not read this ADR as "credentials can now live in the guest." Guest RAM is the memfile that a bank snapshots to disk, and `docs/decisions/embervm/027-snapshot-modes-workload-property.md:139` records the general shape of the hazard: "a snapshot at rest is a leak amplifier compared to the live credential it copied, since the artifact persists and gets replicated/archived long after the credential's original validity window." That reasoning was written about the filesystem workspace tier's capture manifest, but it applies without modification to the memory snapshot: a credential resident in guest RAM at bank time is a credential in an archived artifact that outlives the VM. So injection (the sidecar sets the header, the guest never sees the value) is retained exactly as PR #4100 built it. Per-principal changes who owns the loss if the sidecar or its Secret is compromised; it does not change where the credential is allowed to live.

**4. Caller identity has to come from noded, unforgeable by the guest.** noded already forwards each guest's vsock egress to the sidecar (`startEgress`, `projects/embervm/noded/server/server.go:2329`), and it already has the one piece of identity that exists today, the VM handle's ID, in scope at the call site (`s.startEgress(uds, h.ID)`). PR #4100's own deferred-work list named this precisely: "Host-asserted per-connection identity: buys nothing with one credential and one workload. The hook already exists (`startEgress` has the vmID). Trigger: a second principal class, or #4034 starting." This ADR is that trigger firing. The identity envelope this decision requires is noded framing a principal marker onto the sidecar connection before the guest's own preamble bytes, so the bytes the sidecar trusts for identity never pass through guest-writable memory.

That framing is only trustworthy if nothing else can produce it. Until PR #4112 the sidecar's listener bound every interface in the pod's network namespace (`EGRESS_LISTEN` was `":8888"`) while noded dialled it at `127.0.0.1:8888` (`projects/embervm/noded/config/config.go`), and this chart defines no `NetworkPolicy`, so any pod that could route to the DaemonSet pod's IP could open the port directly. PR #4112 moved that bind to loopback and scoped the lane to named workloads. Record why, because the reason is no longer visible from the diff: loopback is not incidental hardening here, it is the trust anchor for the whole envelope. The moment anything other than noded can reach the listener, that caller can claim to be any principal, and every later refinement of the envelope's contents is decoration on an unauthenticated channel.

`principal` is real elsewhere in this repo's design (`ARCHITECTURE.md:64` defines it as "the identity boundary a workload runs as"; section 9, `ARCHITECTURE.md:486`, states "Only `principal` and `domain` ship now"; line 287 states "No VM or snapshot lineage ever crosses a principal"), but it is not yet plumbed to where this decision needs it. It appears in `projects/embervm/noded/` only inside comments (`server.go:2568`, `registry.go:14`, `server_test.go:1766`, `fcvm/driver/driver.go:1190`), never as a field, and the workload CRD (`projects/embervm/chart/crds/workload-crd.yaml`) carries no principal field at all. Getting a principal identifier from wherever a session is assigned through to the byte noded writes onto the sidecar connection is real, not yet built, work.

**5. The catalog key becomes (principal, host), and duplicate ownership must be rejected, not silently shadowed.** `secretFor` matches on `EgressTo` alone and returns the first entry whose allowlist contains the host (`swap.go`). Once two principals both have an entry for `api.anthropic.com`, that lookup stops discriminating and hands every guest whichever entry `loadSecrets` built first, regardless of who actually asked. Selection has to become a lookup on (principal, host), and `loadSecrets` has to fail the same way it already fails on a malformed catalog: refuse to start rather than let one principal's credential silently serve another's traffic.

**6. Credential storage: one catalog Secret with N keys, behind a resolver interface.** Reject one-Secret-per-principal (a separate k8s Secret, or even a separate `secretRef` entry, per person). Today's catalog already shows why that shape is wrong at this scale: each `egress.secrets[]` entry becomes its own named env var in the pod spec (`_noded-pod.tpl:357-365`, `env: {{ $s.env }}` sourced via `secretKeyRef`), so adding a credential today means editing the chart and rolling the DaemonSet across every node, disrupting whatever live microVMs are running. One 1Password item with a field per principal, synced into one k8s Secret, keeps membership changes out of git and out of the pod spec entirely: kubelet updates the mounted file in place, and the sidecar reads it at request time instead of the fixed env var it reads today. A `credentialResolver` interface (`Resolve(principal, host) (value, ok)`) makes fetching from this mounted Secret a swap of one implementation, so a future move to a secrets manager, or to the control plane acting as distributor, does not touch the call sites around it.

This is the same shape PR #4100 named and deferred: "Envelope-encrypted credentials, per-principal keys, CP as distributor: solves multi-tenant credential distribution that does not exist yet. Trigger: per-user credentials being minted." At the current scale, hard-bounded under 50 users (issue #4034's stated non-goal, "the owner plus friends testing," pinned to the Cloudflare Zero Trust free-tier line), a mounted Secret and a resolver interface deliver the multi-tenant part of that deferred idea without the operational cost of envelope encryption or a control-plane distribution service, neither of which this decision needs yet. Accept the costs explicitly: every principal's credential sits in one etcd object, revocation latency is bounded by kubelet's secret sync interval rather than being instant, and there is no per-principal access audit trail beyond what the sidecar's own request log already gives.

**7. Custody obligation, stated plainly.** Per-principal means the node holds other people's credentials, not just its own workload's. Host compromise moves from one person's loss to everyone's whose credential is in that Secret. This is the same trade Lambda makes (point 2), and it is the right place for the trust to sit given the alternative (a shared credential with no attribution at all), but it raises the bar on host and sidecar security rather than on guest isolation, and it should be a conscious acceptance rather than a side effect of following the Lambda comparison this far.

---

## Architecture

```mermaid
graph LR
    subgraph guest["Firecracker guest (untrusted, principal P)"]
        H["claude CLI<br/>Authorization: dummy"]
    end
    subgraph pod["noded DaemonSet pod (per node)"]
        ND["noded<br/>startEgress(uds, vmID)<br/>knows: this connection is principal P"]
        PX["egress-proxy sidecar<br/>credential broker"]
        SEC[("one k8s Secret, N keys<br/>via OnePasswordItem")]
    end
    EXT["api.anthropic.com"]

    H -->|"vsock:1025, cleartext"| ND
    ND -->|"127.0.0.1:8888 ONLY<br/>identity envelope: principal=P, then guest bytes"| PX
    SEC -.->|mounted, kubelet-synced| PX
    PX -->|"Resolve(P, api.anthropic.com)"| PX
    PX -->|"P's credential injected"| EXT
```

The envelope is a framing change on an existing connection, not a new channel: noded already owns the byte stream between the guest's vsock 1025 tunnel and the sidecar's loopback listener, so it is the only party positioned to assert identity the guest cannot write itself. The sidecar's obligation changes from "read the catalog, match the host, inject" to "read the envelope's principal, resolve (principal, host) against the catalog, inject or deny." A guest with no credential registered for its principal is denied, never tunnelled in cleartext: that follows the fail-closed posture PR #4100 already established for a catalog entry whose secret fails to resolve, extended to a principal the catalog has never heard of.

This ADR does not change the split-horizon egress posture, the TLS-MITM-optional lane, or the cleartext vsock hop's reasoning; all of that is decisions 2 through 5 of ADR 023 and stands as written.

---

## Alternatives Considered

- **A platform-side spend cap or rate limiter on `api.anthropic.com`.** Rejected, and already rejected once in PR #4100's deferred-work list: it does not change abuse economics, since legitimate turns burn quota too, and it duplicates the admission control this repo already places at the Discord feature ACL layer over who may start a session.
- **One k8s Secret (or one `secretRef` entry) per principal.** Rejected: every named credential today becomes its own pod-spec env var (`_noded-pod.tpl:357-365`), so adding a principal means editing the chart and rolling the DaemonSet across every node, which disrupts live microVMs for a membership change that should be as cheap as adding a 1Password field.
- **Standalone per-thread `SecretProxy` CRD and operator (ADR 023's Future Work).** Deferred, not rejected: it buys per-thread proxy selection and independent scaling that per-principal credentials do not need yet. The identity envelope and the resolver interface are deliberately shaped so that migration is a swap of the resolver's backing store, not a rewrite of the broker.
- **Leave the credential shared and rely on guest-side prompt hygiene or output filtering to bound spend.** Rejected: this is exactly the concealment-without-abuse-control gap the Problem section describes, and it does not survive a prompt injected specifically to run up cost through requests that look like normal tool use.

---

## Security

Baseline: `docs/security.md`. Deviations and security-relevant properties beyond what ADR 023 already states:

- **Concealment is unchanged.** The credential still never enters the guest, still never enters a memory snapshot (`docs/decisions/embervm/027-snapshot-modes-workload-property.md:139`), and injection remains the mechanism.
- **The identity envelope's trust rests entirely on the sidecar listener being unreachable except from noded.** PR #4112 bound the listener to loopback, matching how noded already dials it, so no other pod can produce a forged envelope. The chart still defines no `NetworkPolicy`, so the bind address is the only control and must not be widened.
- **Custody obligation.** The node now holds every registered principal's credential, not just one shared one. A host or sidecar compromise now exposes every principal in that catalog, not one shared token everyone was already implicitly trusting. This is accepted as the cost of the Lambda-style trade in decision 2, not treated as a side effect.
- **Revocation latency moves from "edit one value" to "kubelet's Secret sync interval."** Acceptable at the stated scale (under 50 users); noted as a residual cost in Risks rather than solved here.

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| Sidecar listener remains reachable from outside the pod, letting any node-local pod forge the identity envelope | Low, once landed | High | Closed by PR #4112, which binds `EGRESS_LISTEN` to `127.0.0.1:8888` in the binary default and both charts. Any future change that widens that bind reopens the whole envelope, so treat the bind address as a security invariant rather than a config knob |
| `loadSecrets` accepts two catalog entries for the same (principal, host), silently shadowing one | Medium | High | Reject at load time, the same fail-closed posture already used for a malformed catalog |
| Centralized catalog Secret: a host or sidecar compromise now exposes every registered principal's credential, not one shared token | Medium | High | Accepted custody obligation (Security, above); scope to node hardening, not guest isolation, since Firecracker already isolates the guest |
| Revocation latency bound by kubelet's Secret sync interval rather than instant | Low | Medium | Acceptable at current scale (under 50 users); document, do not build a faster path until it is asked for |
| Principal plumbing lands partially (CRD or control plane before noded, or vice versa), leaving a window where the envelope cannot be produced | Medium | Medium | The sidecar's existing fail-closed default (deny an unresolvable credential) applies unchanged to a missing or malformed envelope; no partial-rollout state can fall through to cleartext or to the wrong principal's credential |

---

## Open Questions

1. **Which workload classes need this first.** The claude-runtime session workload is the only egress-proxy consumer today; whether task-class or future workload classes need per-principal credentials on the same timeline, or can keep sharing a credential because they never hold a principal-attributable session, is not settled here.
2. **Whether kubelet's Secret sync interval is fast enough for revocation in practice**, or whether a compromised-credential incident would need a faster path (a sidecar restart, a watch-based reload) than a config change earns on its own.
3. **Whether the `credentialResolver`'s first non-mounted-Secret backing is the control plane acting as distributor**, as PR #4100's deferred-work note framed it, or a secrets manager client, once the resolver interface makes that a real choice rather than a guess.

---

## References

| Resource | Relevance |
| --- | --- |
| [ADR 023 - Egress Secret Proxy for Agent Sandboxes](023-egress-secret-proxy.md) | The injection mechanism and fail-closed posture this ADR amends, not replaces |
| [ADR 027 - Snapshot Modes as a Workload Property (embervm)](../embervm/027-snapshot-modes-workload-property.md) | "A snapshot at rest is a leak amplifier" (line 139), the reason concealment survives per-principal credentials |
| `projects/embervm/ARCHITECTURE.md` | `principal` definition (line 64), section 9 (line 486, only `principal` and `domain` ship), metering-fails-open posture (lines 292-299) |
| PR #4100 (`feat(egress-proxy): inject credentials instead of swapping a placeholder`) | Shipped the injection mechanism this decision builds on; named and deferred both the identity-envelope hook and the per-principal-keys shape this ADR now picks up |
| `projects/firecracker/substrate/egress-proxy/cmd/swap.go` | `secretFor`, `loadSecrets`; the host-only selection key this decision changes to (principal, host) |
| `projects/embervm/noded/server/server.go:2329` (`startEgress`) | Where the vmID is already in scope; the call site the identity envelope extends |
| `projects/embervm/chart/templates/_noded-pod.tpl` | `EGRESS_LISTEN` binding (line 320) and the per-catalog-entry env-var wiring (lines 357-365) this decision moves off of |
| `projects/embervm/deploy/values.yaml:279-292` | Today's single shared credential, and its "shared with the monolith gardener" note |
| `docs/security.md` | Security baseline |

## Update (2026-08-09): delegation model, grant sets, and machine-principal identity

Issue #4584's design discussion on the graph engine's capability track surfaced
three questions the original decision left implicit: who a minted capability is
*for* when the triggering party is not the human at a keyboard, how a grant
reaches the broker in the first place, and which identity plane a machine
principal authenticates against. These extend decisions 4 through 6 below; they
do not revise them. The identity envelope (decision 4) and the `Resolve(principal,
host)` resolver (decision 6) are exactly the seam this update plugs into, the
same way ADR 048 already plugged a second provider's credential shape into that
seam.

**8. Delegation, not impersonation, is the default relationship between the
triggering human and the credential a node runs with.** Authority is one chain:
human trigger, then run, then plan node, then grant. Every minted capability is
`intersection(triggering human's allowed scopes, node's declared capabilities)`,
computed and enforced by deterministic broker policy, never by the node's own
judgment. The agent never presents the human's credential. It holds a derived,
attenuated credential under a distinct actor identity (a GitHub App bot identity
per [ADR 027](027-agent-github-app-roles.md), or a per-capability k8s
ServiceAccount for the resolver added in decision 10 below); the ledger records
subject (the human) and actor (the node) as separate fields on every grant.

Impersonation, presenting the human's own credential unmodified, was rejected
for this default case on three grounds, not one:

- **Audit.** A ledger entry recorded under the human's own identity cannot
  distinguish "the human did this" from "a node acting for the human did this."
  Attribution collapses to the one thing decision 4's identity envelope exists
  to keep distinct.
- **Revocation granularity.** A shared credential revokes all-or-nothing:
  killing one runaway node's access means killing the human's own access too,
  or not killing anything.
- **The adversarial review gate.** [ADR 027](027-agent-github-app-roles.md)
  gave the implementer and reviewer roles separate GitHub App bot identities
  specifically so the implementer cannot approve its own work; the whole point
  is that `jomcgi-implementer[bot]` cannot exercise `jomcgi-reviewer[bot]`'s
  capabilities. If both roles instead ran under the triggering human's own
  identity, that separation dissolves: implementer and reviewer become the same
  actor wearing two labels, and the gate becomes self-approval by construction.
  Delegation with distinct actor identities per node is what lets a multi-node
  plan preserve that gate at all.

This distinction is dormant today, because the only trigger is the operator, and
one principal delegating to itself is not yet a case that can go wrong. It
becomes load-bearing the moment a second principal class can trigger runs: the
`mcp-friends` group referenced in issue #4584 already exists as a group, and a
guest-triggered run under it must be bounded by that guest's own allowed scopes,
not silently inherit the operator's.

**9. Impersonation remains acceptable, but only as a bounded per-provider
fallback, never as a second default.** [ADR 048](048-codex-oauth-token-broker.md)
is the live instance: guest Codex traffic presents to OpenAI as the single
subscription-holder identity the broker owns, because a ChatGPT subscription
grant has no delegation primitive to attenuate. That decision stands unchanged;
this update states the general rule it is one instance of, so the next provider
that forces the same shape is a recognized case rather than a fresh debate.
Impersonation is acceptable at a provider boundary only when all three hold:

- The provider offers no delegation primitive at all (ADR 048's Problem section
  is the worked example: subscription OAuth, not an API key that could be
  scoped and minted per caller).
- No internal gate depends on actor distinction at that boundary. Model
  inference has no approval semantics, so one shared external identity costs
  nothing internally. GitHub does have an internal gate (decision 8's third
  bullet), which is exactly why GitHub stays delegated always and is never a
  candidate for this fallback, regardless of what any individual provider's
  API supports.
- The broker ledger still records which node drew each lease, with per-node
  budgets, so internal audit and rate control stay per-actor even though the
  external service sees one shared identity. The fallback narrows what the
  external party can distinguish; it does not narrow what this repo's own
  ledger distinguishes.

**10. Grants attach at session create, minted per node at admission, revoked at
lease reap.** The create-session API gains a grant set; today `StartRequest`
carries only `restore_lineage` and the broker's own credential catalog is static
env config with no caller identity in scope at request time. Under this update,
each node's capabilities become short-lived leases minted when the node is
admitted, tied to the node rather than the session as a whole, and revoked when
the node reaches a terminal state. Composing lease reap with guest cleanup
(issue #4578) means revocation has exactly one place to happen, not a second
cleanup path that can drift out of sync with the first. The broker gains two
things it does not have today: caller authz (so a `Resolve` call carries and
checks who is asking, not just what they are asking for) and dynamic lease
minting (so a grant is computed per admission against decision 8's intersection
rule, not read verbatim from a static catalog entry).

**11. The machine principal is a k8s ServiceAccount, authenticated through the
TokenReview edge that already exists; Authentik stays the human identity plane
and gains no new role.** Authentik authenticates the trigger (Discord, the
`/agents` console) and hosts the decision inbox where a `human_decision` node
surfaces for approval; it has no client-credentials grant type in use in this
cluster and no RFC 8693 token exchange, so there is no IdP-native place to
record "node N is acting for human H under grant G." That record lives in
broker policy code and the ledger instead, which is where authority decisions
belong given the constraint, not a gap to be closed by adding a second identity
system for machines. A k8s ServiceAccount, verified the same way the k8s-read
capability (decision 12) verifies one, already gives every node a real,
short-lived, audience-bound machine identity with no new plumbing.

**12. k8s reads are a typed brokered capability, not ambient RBAC on a shared
identity.** A dedicated read-only ServiceAccount carries an explicit resource
list (`get`/`list`/`watch` only, named resources, no wildcard), and callers
receive short-lived, audience-bound tokens minted per request via the
TokenRequest API rather than a long-lived mounted token. The apiserver egress
rule attaches only for the duration of the capability's lease, following the
same lease-scoped shape as decision 10. Principal reads (the console backend
querying cluster state for a human) and node reads (a guest reading state
through the egress proxy) are the same capability; they differ in who may
request it and which surface consumes the result, not in the RBAC shape
underneath. Two constraints from this repo's own history apply unchanged and
are worth stating here rather than rediscovering:

- Cilium enforces egress policy after the destination address is already
  service-DNAT'd, so the rule has to target the apiserver's real port, 6443,
  not 443; targeting 443 fails as a silent dial timeout rather than a readable
  deny ([PR #4294](https://github.com/jomcgi/homelab/pull/4294)).
- ServiceAccount verbs and the code that consumes them have to land in the same
  PR. A verb missing from the role fails at runtime as `Forbidden`, which
  surfaces in dashboards as a generic 5xx, not as an obviously RBAC-shaped
  error.

**13. The forcing function for why no real credential may ever reach a guest,
stated once rather than re-derived per provider.** EmberVM snapshots capture
guest memory, and a restore is memory-identical to the state at bank time
([ADR 027 (embervm) - Snapshot Modes as a Workload Property](../embervm/027-snapshot-modes-workload-property.md),
line 139). Any real credential resident in guest memory at bank time is
therefore resident in every later warm restore and every lineage descendant of
that snapshot, not just the session that first held it. This is the same
reasoning decision 3 already applies to the Anthropic leg; this update states it
as the general boundary this whole broker exists to hold, because decisions 8
through 12 add more capability types crossing that boundary, and each one
inherits the constraint rather than re-earning it. The existing presence-keyed
injection at the egress proxy (the sidecar injects a real value only when it
sees a placeholder present, superseding an earlier placeholder-substitution
primitive that was rejected because a placeholder was itself URL-spliceable and
therefore guest-forgeable) remains the only place a real credential is allowed
to exist on the request path. Nothing in this update opens a second one.

Implementation for decisions 8 through 12 is issue #4584 item 17 (the identity
envelope, the grant set, and broker caller authz) plus item 18 (the k8s-read
capability); this ADR records why those shapes were chosen, not their delivery
order.
