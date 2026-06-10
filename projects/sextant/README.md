# sextant

Generates type-safe Go state machines for Kubernetes operators from a YAML
spec. Invalid state transitions become **compile errors**, and transition
signatures force **idempotency keys** to be obtained before state changes.

The [OCI Model Cache operator](../operators/oci-model-cache/) runs on a
sextant-generated state machine — its spec is
[`modelcache.sextant.yaml`](../operators/oci-model-cache/internal/statemachine/modelcache.sextant.yaml)
and the generated package is drift-checked in CI, so the committed code can
never silently diverge from the spec.

## Why

Operator reconcilers degrade into phase-string soup: `if status.Phase ==
"Resolving"` scattered across a thousand-line Reconcile, transitions that
forget to set fields, retries that duplicate external API calls. Sextant moves
those invariants into the type system:

```go
state := calculator.Calculate(resource) // reconstructs typed state from Status

switch s := state.(type) {
case statemachine.ModelCachePending:
    // s.Resolved(...) and s.CacheHit(...) exist.
    // s.SyncComplete(...) does not — that transition isn't legal from
    // Pending, so calling it is a compile error, not a 3am incident.
case statemachine.ModelCacheResolving:
    ...
}
```

Three ideas carry the design:

1. **Sealed interfaces.** Every state implements a private interface method,
   so the state set is closed — a `switch` over states is exhaustive, and
   only generated transitions can produce new states.

2. **Idempotency by construction.** Transitions that record external actions
   take the external system's ID as a parameter:

   ```go
   // 1. Call external API   → get jobName
   // 2. s.JobCreated(jobName) → get new state
   // 3. Persist via SSAPatch  → if this fails, next reconcile retries safely
   func (s ModelCacheResolving) JobCreated(syncjobname string) ModelCacheSyncing
   ```

   You cannot transition first and call the API second — the signature
   demands the evidence up front.

3. **Level-triggered calculation.** State is recomputed from `Status` every
   reconcile by a pure calculator (no network, no side effects), so the
   machine recovers from crashes, conflicts, and stale caches the same way
   Kubernetes itself does: by looking at what is, not what happened.

## Usage

```sh
# Validate a spec (add --xstate to emit XState JSON for visualization)
sextant validate myresource.sextant.yaml

# Generate
sextant generate myresource.sextant.yaml \
  -o ./internal/statemachine --package statemachine \
  --module github.com/you/operator --api github.com/you/operator/api/v1alpha1
```

In this repo the generated package carries a `go:generate` directive, so
regeneration is:

```sh
go generate ./projects/operators/oci-model-cache/internal/statemachine
```

## The spec

```yaml
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine

metadata:
  name: ModelCache
  group: oci-model-cache.jomcgi.dev
  version: v1alpha1

fieldGroups:          # reusable field bundles embedded into states
  resolveResult:
    resolvedRef: string
    digest: string?   # "?" = optional at group level; see `requires`
    ...

states:
  - name: Resolving
    requeue: 10s
    fieldGroups: [resolveResult]
  - name: Ready
    requeue: 6h
    fieldGroups: [resolveResult]
    requires: [digest]   # optional in the group, mandatory in THIS state
  - name: Failed
    error: true
    requeue: 5m
    fieldGroups: [errorInfo]

transitions:
  - from: Resolving
    to: Syncing
    action: JobCreated
    params:
      - syncJobName: string   # idempotency key, forced into the signature
  - from: Failed
    to: Pending
    action: Retry
    guard: retryable          # guarded transitions return *State (nil = refused)

guards:
  retryable:
    condition: "!s.Permanent"
```

Semantics worth knowing:

- **Field-group carry-forward** — when source and target states share a field
  group and the transition's params don't populate it, the generated code
  copies it from the source state (`ResolveResult: s.ResolveResult`), so data
  survives intermediate hops without hand-written plumbing.
- **Retry policy** — with an `errorHandling` block you get retry-count
  exponential backoff with jitter. Without one, retryability is purely
  guard-driven and `RetryBackoff()` follows the state's `requeue` interval.
- **Deletion** — states marked `deletion: true` generate a graceful-degradation
  deletion path. With none, deletion passes through to normal calculation and
  Kubernetes GC handles removal.
- An **Unknown** error state is always generated: unrecognized or corrupted
  `Status` reconstructs to `Unknown` (with `Reset()` for recovery) instead of
  misbehaving as a guessed state.

## Generated output

One spec produces eight files (per machine `M`, in `m_*.go`): phase constants,
state types + sealed interface, the level-triggered calculator, transition
methods, a visitor for exhaustive handling, OTel observability hooks,
Prometheus metrics, and Server-Side Apply status helpers (`SSAPatch` clears
`managedFields` — the API server rejects SSA patches that include them).

Output is deterministic (sorted fields, stable ordering), which is what makes
the CI drift test possible: it regenerates from the spec and fails on any byte
difference, in either direction. Generated code never gets hand-fixed — the
spec or the templates do.

## Layout

```
cmd/sextant/      # CLI (generate, validate, --xstate export)
pkg/schema/       # YAML DSL parsing + validation
pkg/codegen/      # text/template-based generator (templates/*.tmpl)
pkg/xstate/       # XState JSON interchange for diagram tooling
```
