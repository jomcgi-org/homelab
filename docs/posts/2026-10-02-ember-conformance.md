---
title: Checking a deploy against its spec
date: 2026-10-02
tags: embervm, tla, conformance, gitops, homelab
public: false
summary: How I check every EmberVM chart against its TLA+ invariants on a live cluster before it can promote, and the bugs it found in itself.
---

## 1. EmberVM

[EmberVM](https://github.com/jomcgi-org/homelab/tree/main/projects/embervm) is my Firecracker microVM orchestrator: an Elixir control plane that decides which VM runs which task, when a VM gets destroyed, and what to believe about the nodes after it restarts.

I modelled that control plane in TLA+ and the model checker (TLC) runs in CI. That proves the model. It says nothing about the Elixir that actually ships, so this post is about the harness in between: every chart version goes to a dev copy of the cluster, gets exercised, and the control plane's own trace is checked against the same invariants before Kargo lets it into production.

## 2. Specifications

| Part | Specification |
|---|---|
| Specs | 9 TLA+ modules, 38 TLC configs, run through Bazel on every affected change |
| Invariants checked live | 9, from `adoption.tla` |
| Trace | 15 event kinds named after spec actions, 5 excluded with a reason each, a checkpoint every 5 s |
| Trace retention | 24 h, writer queue capped at 10,000 records |
| Scenarios | S1 to S5, every 30 min in dev, 55 s for this morning's run |
| Dev cluster | `embervm-dev` on the GKE hub, one 2 GiB spot brick |
| Promotion rule | `pass` for the rolled version promotes; two consecutive `fail` verdicts or 75 min without one fails the promotion |
| Prod soak | 5 min |

## 3. The loop

![The conformance loop from merge to promotion](figures/conformance-loop.svg)

| Key | Part |
|---|---|
| 1 | A merge publishes the chart (a versioned deploy). ArgoCD syncs it to dev and Kargo waits for the rollout to go Healthy. |
| 2 | A runner inside dev drives five scenarios every 30 minutes: two clones exchanging markers over vsock, a session sleeping and relighting, a second session's start time, the invariant check, and a guest round-tripping non-ASCII output. |
| 3 | The control plane writes a trace of what it did while the scenarios ran. |
| 4 | A checker replays that trace against the nine invariants and serves one verdict per invariant at `/v1/conformance`. |
| 5 | The runner folds the five scenarios into one verdict for the chart version at `/verdict`. |
| 6 | Kargo polls `/verdict` and promotes the chart to production on a pass. |

Two of the 38 TLC configs turn a guard off and must fail or the build goes red. Each one reproduces a bug the cluster has already had: a wedge after a control-plane restart, and a dead node coming back as healthy.

## 4. How does the checker work?

Every time the control plane does something the spec has a name for, it writes a record: `{run_id, seq, spec, action, vars}`. Priming a VM, dispatching a task to it, the task succeeding, a destroy starting and the node confirming it, a node changing health. Every 5 s it also writes a checkpoint: which VMs it thinks are live on each node and what the node itself reports.

The run id changes when the control plane restarts, so a restart reads as a new run and never as a crash in the middle of one. Writes never block the control plane. Records queue behind a cap, a dropped record is counted, and the count rides on the next record that gets through (one record in this morning's window carries `dropped_before: 1`).

The checker groups records by run, sorts them, and asks each invariant one question: is there a violation in this window? It answers `pass`, `fail` or `vacuous`.

## 5. This morning's run

Chart 0.145.5, run `b09f9ba0`, 70 records in 55 s. The verdict after each record is the live checker's answer for the window from suite start to that record; I queried it 70 times. Scrub through it.

### 5.1 Replay

### 5.2 Three verdicts

`vacuous` means the window gave the invariant nothing to check. Six of the nine passed above. The other three never saw a node go unhealthy or a control-plane restart, because the scenarios don't cause one, so they stay vacuous on every scheduled run. That is reported as its own value and never counted as a pass.

The evening I first wired the checker up it found six false passes in itself, two of them already serving green over HTTP. The cause was the same every time. A reader that couldn't parse a record returned an empty collection, every invariant asks "is there a violation in here", and an empty collection has none. I had written a checker that reported parse failures as a clean bill of health.

Three changes came out of that:

| Change | Effect |
|---|---|
| An unreadable record is a `vacuous` verdict | "I could not check this" is a value the API returns, never a `pass` |
| A run where every invariant is vacuous fails the suite | A trace gate that is off, or a store that is empty, reads red |
| Every invariant needs a test that produces all three verdicts through the real writer and store | A checker that can never say `fail` can't ship. One invariant is exempt, with an 80-character reason, because no real emitter can produce its failure |

## 6. The gate

Kargo reads `/verdict` for the dev stage and applies four rules:

| Verdict | Outcome |
|---|---|
| `pass` for the version it just rolled | Promote to production after a 5 min soak |
| `fail`, previous run `pass` | Hold and keep polling. One red straight after a roll is usually the cluster settling |
| `fail`, previous run `fail` | Promotion fails, the chart stays in dev |
| Stale version, or no verdict in 75 min | Promotion fails |

A failed promotion leaves the previous chart serving. Approving the freight by hand in Kargo is the override.

## 7. Bugs it found

| Found | How | Fix |
|---|---|---|
| Six false passes, two serving green | The checker, on its first evening | Section 5.2 |
| A destroy path that skipped the durable intent record and recorded `destroyed` before the node confirmed | Reading the control plane against the two destroy invariants, before any run caught it | [#4813](https://github.com/jomcgi-org/homelab/issues/4813) |
| The writer emitted a key called `adopted`; the checker read `vm_ids` | The hand-built test fixtures used the checker's spelling, so they agreed with it | Fixtures replaced by the real write path |
| A writer restart silently stopped recording | Reviewing the writer for a runtime toggle | [#4841](https://github.com/jomcgi-org/homelab/issues/4841) |
| Tests reading each other's traces | One scenario asserted on its own trace and saw a node it never created | A per-test writer scope ([#4833](https://github.com/jomcgi-org/homelab/issues/4833)) |
| `inventory_reconciled` red on every dev run, so no chart could promote | A node daemon change the checker's node-count check didn't expect | [#6422](https://github.com/jomcgi-org/homelab/issues/6422). The gate held production still until I fixed the checker |

## 8. What it doesn't do

| Not covered | Why |
|---|---|
| Running TLC over the live trace | Built and proven on fixture windows in CI, switched off in dev until it has run there for real. The live checker is hand-written Elixir that re-implements each invariant |
| Liveness | 8 of the 9 invariants are safety properties: they catch something illegal happening. A control plane that stops doing anything passes all of them. The one liveness check is bounded to two checkpoints, so it catches a wedged dispatcher and misses a slow one |
| Production | Tracing is on in dev and off in prod. The chart that reaches prod has been checked; the prod control plane isn't watched the same way |

## 9. What does this actually mean?

Is this worth it for a homelab? It has blocked production once because the checker itself was wrong (#6422), and the rest of the table in section 7 is bugs in the control plane and the harness that nothing else was going to find. I'd rather have the gate hold a chart than find out from a session that lost its VM.

To follow: turning TLC on against the live dev windows, which is the piece that would let me delete the hand-written checker.
