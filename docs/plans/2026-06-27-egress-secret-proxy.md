# Plan: Egress Secret Proxy for Agent Sandboxes

Implements [ADR 023](../decisions/agents/023-egress-secret-proxy.md) (builds on
[022](../decisions/agents/022-firecracker-snapshot-restore-controller.md)). Keeps real
secrets out of agent microVMs: the guest holds only placeholders, and the real value is
swapped in on egress, toward allowlisted hosts only, by a sidecar the guest cannot reach
except through it.

## MVP scope (the explicit ask)

**The MVP is functional but a no-op for microVMs: built, deployed, and self-testable, but
not enforced.** Phases 0-2 deliver a working, CI-tested egress-proxy data plane plus the
GitOps catalog and secret wiring, deployed as a sidecar in the `fc-agentd` DaemonSet. It is
**not** wired into the guest egress path: no guest-trusted CA, no vsock 1025 routing, no
placeholders injected into guest env. Real agent threads are therefore completely
unchanged. This derisks the secret-handling data plane and the GitOps surface with zero
production behavior change and no MITM CAs in guest images.

**Enforcement (Phase 3) is out of MVP scope** and turns the no-op into a live control:
placeholder injection, the guest CA, and vsock routing, behind a flag, canaried on one
thread.

## Decisions carried in (settled in ADR 023)

| Question | Decision |
| -------- | -------- |
| Mechanism | Placeholder substitution on egress, not eBPF (microVM has its own kernel; Go harness has no OpenSSL) |
| Interception point | The vsock `EgressPort = 1025` hop (already reserved by ADR 022) |
| Primitive | env-placeholder + literal byte-swap + per-secret destination allowlist; no per-integration auth modeling |
| Exfil control | The allowlist: a non-allowlisted destination receives the literal (useless) placeholder, no explicit block needed |
| TLS | Proxy terminates TLS with a guest-image-scoped CA (the unavoidable cost of swapping inside HTTPS) |
| v1 data plane | A `egress-proxy` sidecar in the per-node `fc-agentd` DaemonSet; `fc-agentd` stays a secret-free forwarder |
| Values | Provisioned via the existing `OnePasswordItem`/ESO path, mounted into the sidecar |
| Catalog | Static config (ConfigMap/values) shaped like the future `SecretProxy` CRD `spec.secrets`, so v2 is a lift |
| CRD + operator | Deferred (future work) |

## Components

- **`egress-proxy`** (new, `projects/agent_platform/egress-proxy/`): a Go TLS-terminating
  forward proxy. Holds the mounted secrets, loads the catalog, does swap + allowlist.
- **Catalog** (config): `env -> {secret key, egressTo[]}`, mounted as a ConfigMap, shaped
  like the future CRD spec.
- **Secret mount**: an `OnePasswordItem` syncing a k8s Secret, projected into the sidecar
  container only.
- **`fc-agentd`** (existing): gains the sidecar in its DaemonSet pod now; gains the vsock
  1025 -> localhost forwarder only at enforcement (Phase 3).
- **`vsockproto`** (existing): gains an `env -> placeholder` map on the assign message only
  at enforcement (Phase 3).

## Phases

Each phase is independently shippable and verifiable. CI is the test loop (push, watch).

### Phase 0 - Foundations (interfaces + pure logic)

- **Catalog types + loader** (Go): parse the catalog file into `[]Secret{Env, SecretKey, EgressTo []string}`. Field names mirror the future `SecretProxy` `spec.secrets` so the type is reused verbatim by the operator later.
- **Placeholder derivation**: a pure `Placeholder(proxyKey, threadID, env) string` returning a fixed-prefix, high-entropy token (HMAC-based, deterministic so it survives restore without storage, per ADR 023 open question 1). Unit-tested for stability + non-collision.
- **Secret resolver**: an interface that reads a value by catalog key, with a file-mount impl (reads the projected Secret) and an in-memory fake for tests.
- **Done when**: catalog loads, placeholders derive deterministically, the resolver reads a fake mount; all green in CI.

### Phase 1 - Egress proxy data plane (the core, standalone)

- Go HTTP(S) forward proxy: accept a client connection, resolve the destination host (CONNECT target / SNI / Host), TLS-terminate with a configured CA, scan the plaintext request, **literal-swap** any known placeholder -> resolved value **iff** the destination is in that secret's `egressTo`, else pass the literal through untouched; re-originate TLS upstream; stream the response back.
- Buffered scan so a placeholder split across read boundaries is still matched (ADR 023 risk: streaming bodies).
- **Done when** (all in CI, no microVM): an integration test with a throwaway CA + a local HTTPS upstream proves (a) allowlisted destination -> upstream receives the real value; (b) non-allowlisted destination -> upstream receives the literal placeholder; (c) a placeholder straddling chunk boundaries is swapped; (d) a request with no placeholder is byte-identical end to end.

### Phase 2 - Packaging + GitOps deploy (MVP terminal state, still no-op)

- apko image for `egress-proxy` (dual-arch x86_64 + aarch64, non-root uid 65532), per the repo image conventions.
- Add the sidecar container to the `fc-agentd` DaemonSet chart with: the catalog ConfigMap, an `OnePasswordItem`-mounted Secret projected **only** into the sidecar, and resource limits. The sidecar exposes a **localhost-only** listener; it is **not** connected to vsock 1025 and guests are not routed to it.
- Seed the catalog with one low-risk secret entry so the pod can self-test end to end.
- A self-test (init/sidecar exec inside the pod, or a CI integration target) drives the proxy over its localhost listener and asserts the Phase 1 behavior with the real mounted secret.
- Bump `Chart.yaml` version and `deploy/application.yaml` `targetRevision` together.
- **Done when**: ArgoCD syncs; the sidecar runs in every `fc-agentd` pod with secrets mounted and catalog loaded; the in-pod self-test passes; **real microVM egress is provably unchanged** (no guest routes through the proxy, no guest CA, no placeholders injected). **This is the MVP.**

### Phase 3 - Enforcement (post-MVP; flips the no-op to live)

- **3a Placeholder injection**: extend `vsockproto.Message` with `Secrets map[string]string` (env -> placeholder); `fc-agentd` populates guest env at `KindAssign` from the catalog. Guest now sees placeholders (still inert until routed).
- **3b Guest CA**: mint the proxy CA, bake its cert into the guest base-image trust store (warm-base rebuild, ADR 022), sidecar terminates with it. Plan CA rotation vs warm bases (ADR 023 open question 2).
- **3c vsock routing + flip**: `fc-agentd` forwards the guest's vsock 1025 stream to the local sidecar; enable behind a per-thread (or global) enforcement flag; canary on a single thread before default-on.
- **Done when**: a real microVM thread's egress is mediated end to end: allowlisted host receives the real value, a request to a non-allowlisted host carries only the literal placeholder, and the guest's env/disk/snapshot contain no real value.

## Risks / notes

- **Transformed credentials are out of scope** (ADR 023): HTTP Basic `base64(user:token)`, AWS SigV4 / webhook HMAC signing never appear literally on the wire, so literal swap cannot find them. Document per-catalog-entry; revisit via a broker only if needed.
- **Guest CA is the trust pivot**: the key lives only in the sidecar, never in `fc-agentd` or the guest; it is added only to the agent guest image trust store, never cluster-wide.
- **Per-node shared catalog**: every thread on a node can reach that node's catalog secrets (per-secret allowlist still bounds destinations). Per-thread subsetting is the future CRD, not this plan.
- **Non-HTTP egress** (git-over-SSH, raw TCP) is out of scope for v1; document it.
- The MVP deliberately stops before any change a hostile guest could observe, so it can ship and bake without risk while Phase 3 is reviewed.

## Future work

The catalog type is the future `SecretProxy` CRD `spec.secrets`. The operator flavor
(independently-scaled proxy Deployments, threads selecting a proxy by name, per-proxy trust
tiers) reuses the Phase 1 data plane unchanged, extracted from the DaemonSet sidecar into a
standalone Deployment. Deferred until per-thread proxy selection is actually needed.

## Execution

Per the repo convention, execute subagent-driven once this plan is approved; one
comprehensive review at the end of each merged PR. The MVP (Phases 0-2) is the first
shippable unit; Phase 3 is a separate, flag-gated follow-up.
