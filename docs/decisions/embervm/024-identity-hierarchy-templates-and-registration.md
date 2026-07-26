# ADR 024: Identity Hierarchy and Guest Identity Assertion

**Author:** jomcgi
**Status:** Draft
**Created:** 2026-07-26
**Supersedes in part:** [022 - Domain-Scoped Service Composition](022-domain-composition-access-fabric.md) (its `domain` boundary)
**Split:** the authoring half is [ADR 026](026-template-composition-gitops-registration.md)
**Builds on:** [001 - EmberVM](001-embervm-beam-firecracker-workload-orchestrator.md) (the principal isolation rule), [019](019-op-log-data-structure-payload-separation.md) (principal as the erasure key), [021](021-workload-resource-model-memory-pivot.md) (the single resource dial)

---

## Problem

ADR 022 introduced `domain` as a required boundary without saying whether a domain may span principals. That single omission propagates: same-domain-by-default bindings could cross the one boundary ADR 001 actually enforces, and a domain spanning cells breaks the scoping ADR 020 depends on.

A second omission compounds it: ADR 022 assumes every workload carries a domain, but the Workload CRD carries no `principal` or `tenant` field at all today, so shared definitions have no home in the hierarchy it proposes.

Separately, a guest today has no way to assert *who it is* to the platform without being handed a credential that lets it act as that identity in Kubernetes. That is the same conflation, one level down: identity and capability treated as one thing.

The authoring problem ADR 022 also fails on (per-workload CRs at 100k, the Helm-template surface, and materialising `products x tenants` definitions) is split out into [ADR 026](026-template-composition-gitops-registration.md), because it is a different subject with a different answer.

---

## Decision

Three decisions. The authoring half (templates, GitOps without per-workload CRs, registration and class inference) is split out into [ADR 026](026-template-composition-gitops-registration.md).

### 1. The identity hierarchy, with domain contained in exactly one principal

```
Account        deploying customer     billing / grouping, NO isolation semantics
  └─ Product   grouping               naming, NO isolation semantics
      └─ Principal   consuming tenant  ISOLATION BOUNDARY (ADR 001)
          └─ Domain      env or grouping within one principal
              └─ Workload
```

**A domain is contained in exactly one principal.** ADR 022 left this open, and closing it dissolves both of the review findings against that ADR: same-domain-by-default becomes necessarily same-principal, so a binding cannot implicitly cross the isolation boundary, and a domain is cell-confined by construction wherever principal maps to cell.

The rule that generates this: **the isolation unit is whatever must not leak.** ADR 001 defines a principal as "the identity a workload runs as (a tenant, user, or service account)" and pins the no-crossing rule to it. If two consuming customers' data must never mix, they are principals, whatever the product UI calls them.

Domain is complementary rather than redundant, and the reason is worth stating: **ADR 001's isolation rule gives you nothing inside a principal.** Everything one principal owns is mutually reachable as far as the platform is concerned. Domain is the only sub-principal boundary available, which is what makes prod-versus-staging separation expressible at all.

Everything above Principal is grouping and billing with no isolation meaning. This corrects ADR 022's chargeback-at-domain, which belongs at Account.

**Only `principal` and `domain` ship now.** Account and Product are recorded as the shape above so they can be added without moving the isolation level, which is the expensive part to change later.

### 2. Platform workloads live in a reserved platform principal

The Workload CRD carries no `principal` or `tenant` field today: definitions are principal-agnostic, instances are principal-scoped, and that split is load-bearing. One `sandbox-session` definition serves many principals, each with isolated lineage, which is what makes the session class a multi-tenant sandbox surface at all.

Decision 1 makes every workload carry a domain inside a principal, which would leave shared definitions homeless. They are not exempted; they are **owned by a reserved `platform` principal** and made instantiable by others through an explicit broad grant. The hierarchy stays uniform, nothing is principal-agnostic, and "many principals instantiate this" becomes an ordinary wildcard grant rather than a special case.

This also fixes ADR 022's `access.in: workloadRef: {name: agent-session}`, which as written grants access to **every** principal's session instance. ACL entries name `(workload, principal)` for platform definitions and bare `(workload)` for owned ones.

### 3. A guest asserts a ServiceAccount identity; it never holds a cluster credential

A live Kubernetes ServiceAccount token is **not** projected into a guest. The guest is the adversary the microVM boundary exists for, a token in guest memory banks with the snapshot (agents/023's founding argument), and a cluster credential routes around the brokered egress path that is ember's default posture. ADR 001's justification for looser handling on session and serving ("tenant-trusted code") is already weakened by model-authored code landing on the session class, so it cannot carry this.

Instead, two halves:

- **Identity:** an **audience-scoped** projected token (audience `embervm`), valid only against ember's own API. A guest can prove "I am SA X" while holding nothing usable against the Kubernetes API. This reuses Kubernetes RBAC as the source of who-is-who without granting any cluster access.
- **Capability:** the platform holds the real credential and acts on the guest's behalf for named operations, treating the Kubernetes API as another `ServiceRef` target under ADR 022's fabric and agents/023's broker pattern.

This keeps ADR 022's three legs symmetric in *policy* while deliberately asymmetric in *credential holding*: a Service→VM caller is a trusted pod presenting a real ServiceAccount, and a VM→Service guest presents an assertion while the platform presents the credential. One side is trusted and the other is not, and the design should say so rather than treat them alike.

---

## Architecture

```mermaid
graph TB
    ACC["Account<br/>billing, no isolation"]
    PRD["Product<br/>grouping, no isolation"]
    PRIN["Principal<br/>ISOLATION BOUNDARY"]
    PLAT["platform principal<br/>shared definitions"]
    DOM["Domain<br/>env within one principal"]
    WL["Workload"]
    SA["guest: audience-scoped<br/>SA assertion"]

    ACC --> PRD --> PRIN --> DOM --> WL
    PLAT -.broad instantiation grant.-> PRIN
    WL -.asserts identity, holds no credential.-> SA
```

Everything above Principal is grouping. Principal is where ADR 001's no-crossing rule applies. Domain is the only sub-principal boundary, which is what makes prod-versus-staging expressible at all.

---

## Alternatives Considered

- **Domain as a tenancy boundary (ADR 022 as written).** Rejected: consuming customers inside one principal get no lineage isolation from each other, which is the guarantee they would be paying for.
- **Namespacing every definition by principal.** Rejected: 1,000 principals needing the same ~20 platform workloads is 20,000 near-identical definitions, and it destroys the shared-definition property that makes the session class multi-tenant.
- **Exempting platform workloads from the hierarchy.** Rejected: a required field with an exception is not a required field. A reserved platform principal costs one row and keeps the model uniform.
- **Per-workload ClusterIP Services to escape the 10-port stateful ceiling.** Rejected: it reintroduces `O(definitions)` etcd objects plus Cilium programming, the exact wall ADR 026 evicts. Name-based L4 (SNI or PROXY-protocol on a few entry ports) is the only remedy consistent with this ADR.
- **Projecting a live Kubernetes ServiceAccount token into a guest.** Rejected; see decision 3 for what replaces it.

---

## Security

Baseline: `docs/security.md`.

- **Principal remains the isolation boundary** and is unchanged by anything above it. Account and Product carry no isolation meaning by construction, so adding them later cannot weaken ADR 001's rule.
- **Erasure follows principal** (ADR 019 makes it the key and `NOT NULL` everywhere). Customer offboarding arrives at Account, so Account-to-principal mapping must exist before ADR 019 moves to Accepted, or offboarding has no defined scope.
- **The platform principal is a privileged identity.** Its broad instantiation grant is the widest grant in the system and should be the most reviewed; it must not become a general-purpose escape hatch for cross-principal access.
- **Registration authority is scoped to the caller's identity**, or the desired-set API becomes a way to define workloads in another principal.
- **Audience-scoped tokens grant identity, not capability.** The distinction is the security decision: assertion of who you are, with no cluster access attached.

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
| ---- | ---------- | ------ | ---------- |
| Hierarchy surfaces in v1 UX and developers answer five identity questions before hello-world | **High** | High | Only `principal` and `domain` ship; both default in a single-tenant deployment; class inference ([ADR 026](026-template-composition-gitops-registration.md) decision 4) lands before the hierarchy is user-visible |
| Platform principal becomes a dumping ground | Medium | Medium | Membership is reviewed in Git; a workload in the platform principal is a platform commitment |

---

## Open Questions

1. ~~What `tenant` currently means.~~ **Checked: it is a deployment-level constant, not a per-customer identity.** It defaults to `"homelab"` (`drain_coordinator.ex:56`) and is stamped onto every op from that config. So it already occupies the **Account** slot in decision 1's hierarchy and should be renamed or documented as such rather than repurposed.
2. **When `domain` becomes required on existing chart workloads**, versus grandfathering empty as a default.

---

## References

| Resource | Relevance |
| -------- | --------- |
| [ADR 001](001-embervm-beam-firecracker-workload-orchestrator.md) | The principal definition and isolation rule this anchors on |
| [ADR 022](022-domain-composition-access-fabric.md) | The composition model and `domain` boundary this corrects |
| [ADR 019](019-op-log-data-structure-payload-separation.md) | Principal as the erasure key, which Account-level offboarding must map onto |
| [ADR 021](021-workload-resource-model-memory-pivot.md) | The single resource dial that makes class inference sufficient |
| [ADR 020](020-admission-control-plane-token-routing-peer-redistribution.md) | The miss-path lookup that template resolution rides |
| [agents/023](../agents/023-egress-secret-proxy.md) | The broker pattern the audience-scoped token decision follows |
| `projects/embervm/control/lib/embervm/workload_watcher.ex` | The informer whose relist behaviour motivates ADR 026 decision 3 |
| `projects/embervm/chart/values.yaml` | `statefulTcpPortRange` (10 ports) and the per-workload template surface |
| `docs/security.md` | Security baseline |
