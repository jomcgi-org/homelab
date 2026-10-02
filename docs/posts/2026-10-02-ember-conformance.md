---
title: A deploy has to agree with the spec
date: 2026-10-02
tags: embervm, tla, conformance, gitops, homelab
public: false
summary: How every EmberVM chart is replayed against its TLA+ invariants on a live cluster before it can promote, and what the checker found when I pointed it at itself.
---

EmberVM runs Firecracker microVMs on my cluster. Its control plane decides
which VM runs which task, when a VM is destroyed, and what to believe after a
restart. I modelled that logic in TLA+ and the model checker proves
statements like "no two tasks ever share a VM" over every path it can
reach.

A proof about the model says nothing about the Elixir that ships. So every
chart version is deployed to a dev copy of the cluster, exercised, and the
system's own trace is checked against the same invariants. The verdict
decides whether the chart promotes to production.

## What a deploy has to pass

![The conformance loop from merge to promotion](figures/conformance-loop.svg)

| Key | Part |
|---|---|
| 1 | A merge publishes the chart. ArgoCD syncs it to the dev cluster and Kargo waits for the rollout. |
| 2 | A runner inside dev drives five scenarios every 30 minutes: two clones exchanging markers over vsock, a session sleeping and relighting, a restart latency check, a guest round trip, and the invariant check. |
| 3 | The control plane writes a trace of what it did while the scenarios ran. |
| 4 | A checker replays that trace against nine invariants from the TLA+ spec and serves the result at `/v1/conformance`. |
| 5 | The runner folds all five scenarios into one verdict for the chart version. |
| 6 | Kargo polls the verdict and promotes the chart to production on a pass, after a five-minute soak. |

The TLA+ specs stay in CI. Thirty-eight model-checking runs across nine specs
go through Bazel on every affected change, including two configs that turn a
guard off and must fail, because each reproduces a bug the cluster has already
had: a wedge after a control-plane restart and a dead node coming back as
healthy.

## The trace

The control plane records 15 kinds of event, each named after an action in
the spec: priming a VM, dispatching a task to it, the task succeeding, a
destroy beginning and the node confirming it, a node's health changing. Every
five seconds it also writes a checkpoint, a snapshot of which VMs it believes
are live on each node and what the node itself reports.

A record is `{run_id, seq, spec, action, vars}`. The run id changes when the
control plane restarts, so a restart reads as a new run and never as a crash
in the middle of one. Writes never block the control plane: records queue
behind a cap and a dropped record is counted, with the count riding on the
next record that gets through.

Five spec actions have no trace site, with the reason written next to each
one. A test fails if the set of emitted actions plus the set of excluded
actions stops matching the spec.

## Watching the checker work

This is the control plane's trace from this morning's run on chart 0.145.5,
with the checker's verdict after every record. Scrub through it.

### The run

### A verdict has three values

Each invariant answers pass, fail or vacuous. Vacuous means nothing in the
window gave it anything to check, and it is reported as its own value. Six of
the nine passed above; the three that stayed vacuous never saw a node go
unhealthy or a restart, because the scenarios did not cause one.

The checker's first job was finding six false passes in itself, two of them
already serving green over HTTP. They all failed the same way. A reader that
cannot parse a record yields an empty collection. Every invariant asks "is
there a violation in this collection". On an empty collection the answer is
no. A parse failure and a clean bill of health were the same value.

So an unreadable record is now a vacuous verdict, never a pass. A run in
which every invariant is vacuous fails the suite. And each invariant has to
have a test that produces all three verdicts through the real writer and
store, so a checker that can never say fail cannot ship.

## The gate

Kargo reads `/verdict` for the dev stage and applies four rules.

| Verdict | Outcome |
|---|---|
| `pass` for the chart version it just rolled | Promote to production after a five-minute soak. |
| `fail`, previous run `pass` | Hold and keep polling. One red after a roll is usually a transient settling. |
| `fail`, previous run `fail` | Promotion fails. The chart stays in dev. |
| A stale version, or no verdict in 75 minutes | Promotion fails. |

A failed promotion keeps the previous chart serving. Approving the freight
by hand in Kargo is the override.

## What it found

| Found | Where |
|---|---|
| Six false passes, two serving green | The checker, on the evening it was first wired up |
| A destroy path that skipped the durable intent record | The control plane, through `destroy_intent_precedes_record` |
| The writer emitting `adopted` while the checker read `vm_ids` | The trace, when the fixtures were replaced by the real write path |
| A sequence counter reset that silently recorded nothing | The writer |
| Tests reading each other's traces | A globally registered writer |
| `inventory_reconciled` red on every dev run, blocking every chart | The checker's node-count oracle, after a node daemon change |

The last one is the gate working in the wrong direction: a checker bug held
every chart in dev until the checker was fixed. The alternative was letting charts through on a verdict nobody had read.

## What it does not do

The live checker is hand-written Elixir that re-implements each invariant.
Feeding a trace window to the real model checker is built, proven on fixture
windows in CI, and switched off in dev until it has run there for real.

Every invariant but one is a safety property: it catches something illegal
happening. A system that stops doing anything satisfies all of them. The one
liveness check is bounded to two checkpoints, which catches a dispatcher that
has wedged but not one that is merely slow.

Tracing is on in dev and off in production. The chart that reaches
production has been checked; the production control plane is not watched the
same way.
