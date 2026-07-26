# Reading ADRs 019-026

Eight ADRs written as one design pass, answering one question: **what has to change for EmberVM to manage 100k+ workload definitions instead of tens.**

They are numbered in the order they were written, which is not the order they are best read in. This note gives the reading order and the idea they share. Everything decided is in the ADRs themselves; nothing here is a decision.

## The through-line

One principle, applied eight times:

> **The control plane holds sparse facts and derives dense things on demand. It never stores or witnesses anything that scales with the fleet.**

Each ADR is that principle meeting a different subsystem:

| Was O(fleet) | Became |
| ------------ | ------ |
| Request payloads in the ops journal | facts plus a reference, with payloads on their own lifecycle (019) |
| A metering write per dispatch | a local lease debit, reported (020) |
| Placement computed per arrival | assignment precomputed at forecast cadence (020) |
| Session routing state in a directory | an encrypted token the client holds (020) |
| Capacity as a two-dimensional vector | one number, because only memory is reserved (021) |
| A workload definition per tenant | a template plus a tenant record (026) |
| A Kubernetes object per workload | one CR per product, with definitions in the CP datastore (026) |

## Reading order

Three threads. Within each, later ADRs depend on earlier ones.

**Data (standalone, start here).**

- **019** Op-log data structure, payload separation, principal-scoped erasure.

**Runtime.**

- **021** Resource model. Read first: it establishes that capacity is a scalar, which the rest depends on.
- **020** Admission-only control plane, token routing, peer redistribution. The hub of the set, and the most amended: its decision 3 is withdrawn to 023.
- **025** Local disk authoritative, S3 an archive, durability an interval. Withdraws ADR 011's Longhorn decision for stateful volumes.
- **023** Class-scoped ownership arbitration. Resolves 020's withdrawn decision 3, and depends on 025 for the stateful half.

**Product.**

- **022** Domain-scoped composition and the access fabric. Superseded in part by both of the next two.
- **024** Identity hierarchy and guest identity assertion.
- **026** Template composition, GitOps without per-workload CRs, registration.

## What this set changes elsewhere

Five previously-Accepted ADRs carry amendment notes rather than being edited in place:

| ADR | Amended by | What changed |
| --- | ---------- | ------------ |
| 001 | 025 | R7's "a placement move is a copy, never a rebuild" no longer holds for stateful |
| 007 | 020 | the creation-critical-path rejection is reversed for the metering write only |
| 011 | 025 | stateful volumes stay on local disk; the Longhorn move is withdrawn |
| 016 | 020, 025 | the CP-owned placement loop, and decision 5's Longhorn-plus-S3 durability clause |
| 018 | 020, 023 | `meteringFailOpen` becomes the default; a brick may stop a *running* session under silence |

## Confidence

All eight are **Draft**. Where a number is a guess rather than a derivation, the ADR says so: ADR 021's 1,024 MiB pivot, ADR 025's 8h continuity floor, and ADR 020's >90% utilization target are each labelled provisional with what would move them.

The known-unfixed wall: the stateful class is hard-capped at ten workloads by `statefulTcpPortRange` (5400-5409), CRD-validated. No ADR here fixes it; ADR 026 constrains the remedy (it cannot be per-workload ClusterIP Services) without choosing one.
