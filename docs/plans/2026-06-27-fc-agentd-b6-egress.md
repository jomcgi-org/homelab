# fc-agentd B6: egress proxy (ADR 023), staged 6a plain + 6b secret-swap

**Status:** Planned
**ADR:** 023 (egress secret proxy), builds on 022 (FC controller)
**Depends on:** B5 Tasks 1-5 (merged), A base-rootfs builder (merged)

## Context

The substrate is validated in-cluster (no SSH): task delivery over vsock, the
Done lifecycle, snapshot-on-idle, and the base-rootfs builder all work, and goose
launches and runs. The remaining gap is egress: goose cannot reach a model (or
any in-cluster service), and it cannot find its recipe. ADR 023 specifies the
egress design: the guest's only egress is vsock 1025, forwarded by a secret-free
fc-agentd to a co-located egress-proxy sidecar that (eventually) terminates TLS,
swaps placeholders for real secrets, and enforces a per-secret destination
allowlist.

Stage the work so goose runs against in-cluster Qwen first (6a), then add the
secret machinery (6b) onto the same sidecar.

## 6a: plain allowlisted egress + goose environment (goose runs a real turn)

No secrets, no TLS termination. The sidecar is a plain allowlisted forward proxy;
this proves the vsock-1025 -> fc-agentd -> sidecar -> upstream path and gets goose
talking to Qwen.

### Task 6a.1 - egress-proxy sidecar image

A small apko image running a minimal Go HTTP forward proxy (CONNECT + absolute-URI
forward) that only forwards to an allowlist of hosts (config via env/ConfigMap).
New package under `projects/agent_platform/egress-proxy/` (binary + apko image),
pinned into the fc-agentd chart like the rootfs-builder.

### Task 6a.2 - fc-agentd forwards vsock 1025 to the sidecar

Extend the per-thread server: alongside the control listener on
`<uds>_<ControlPort>`, accept guest connections on `<uds>_<EgressPort>` and
io.Copy them to `localhost:<sidecarPort>`. fc-agentd holds no secrets and does no
parsing of the egress bytes (raw tunnel), preserving the ADR 023 blast-radius
split. Started in startControl next to the control server, torn down with it.

### Task 6a.3 - guest egress listener + proxy env

fc-agent-init runs a local TCP listener on 127.0.0.1:<port>; each accepted conn
dials vsock EgressPort and io.Copy tunnels it. Set HTTP_PROXY/HTTPS_PROXY for the
harness to that listener so goose's HTTP client routes through it.

### Task 6a.4 - goose environment baseline

fc-agent-init seeds the goose env the raw boot otherwise lacks: GOOSE_RECIPE_PATH
(=/home/goose-agent/recipes, where the harness image stores recipes; fixes the
"recipe not found" error), GOOSE_PROVIDER + GOOSE_MODEL + the OpenAI base URL for
in-cluster Qwen (injected via the controller's Assign or a values-driven default).

### Task 6a.5 - chart wiring + allowlist

Add the egress-proxy sidecar container to the fc-agentd Deployment (gated on
firecracker.enabled), its allowlist config (default: the inference/Qwen service +
context-forge), and the goose model values. Pin the egress-proxy image via Bazel.

### Task 6a.6 - in-cluster validation

Submit a thread; confirm goose finds the recipe, reaches Qwen through the proxy,
and produces output (a real turn). Validate via fc-agentd logs + the registry +
the sidecar logs. No SSH (the base rootfs auto-rebuilds).

## 6b: secret placeholder-swap + TLS (credentialed egress)

Onto the same sidecar, per ADR 023 decisions 1-4, 6.

### Task 6b.1 - TLS termination with a guest-trusted CA

Bake a guest-scoped CA into the harness image trust store; the sidecar terminates
TLS with a cert under that CA and re-originates TLS to the real destination.

### Task 6b.2 - placeholder substitution

The guest holds only high-entropy placeholders (fixed-prefix ULID, e.g.
`kloak:01J...`). The sidecar swaps the literal placeholder for the real value on
egress, only when the destination is in that secret's allowlist; otherwise the
placeholder passes through untouched (exfiltration fails by construction).

### Task 6b.3 - secret catalog + values

A mounted ConfigMap maps env -> secret key -> egressTo, shaped like the future
SecretProxy CRD spec.secrets. Real values via OnePasswordItem -> k8s Secret
mounted into the sidecar only. fc-agentd never sees them.

### Task 6b.4 - guest placeholder injection

fc-agent-init exposes the placeholders to the harness as env (the catalog's env
names), so goose/tools use them as if real tokens.

## Risks / unknowns

- Does goose's HTTP client (reqwest) honour HTTP_PROXY/HTTPS_PROXY for the OpenAI
  base URL? If not, point GOOSE base URL straight at the local listener instead.
- The PID1-exits kernel panic after the harness exits is cosmetic (controller
  already reclaimed) but should be cleaned up: fc-agent-init should not let PID1
  return; either keep it alive or trigger a clean guest poweroff.
- 6b TLS termination requires the guest to trust the sidecar CA for every upstream
  it talks to; scope the CA to the guest image only, never cluster-wide.

## Validation gates

Per-task self-review; one end-of-PR review. Test execution deferred to CI; the
behavioural proof is the in-cluster validation in 6a.6 (and the 6b equivalent),
which is now SSH-free thanks to the base-rootfs builder.
