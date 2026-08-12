---
name: rollup-architecture-docs
invoke: explicit
summary: Reconcile one domain's claims against reality, write ARCHITECTURE.md, drop the harvested ADRs
---

> **Runbook (explicit-only).** Open only when Joe asks for this procedure, or a
> claude.ai routine prompt names this file. Do not auto-load from skill matching.

Roll one domain up into a `projects/<domain>/ARCHITECTURE.md` that carries its
**current state**, then delete the ADRs that state was harvested from.

This is a reconciliation pass, not a writing task. The document is the output.
The adjudication is the point.

## Why it exists

For most domains the ADR tree is the only narrative document, so an agent asking
"how does this work today" has nowhere to go but a decision log written at a
point in time. It then reads a stale decision as current state.

Status hygiene does not fix that. `agents/020` ("Deprecate Context Forge") was
**Accepted**, and correctly so: the decision genuinely was accepted. It was then
half-executed, and Context Forge became the agent gateway instead. Nothing
recorded the gap, and three sources described it three different ways.

`projects/embervm/ARCHITECTURE.md` is the working template.

## Method

### 1. Gather claims

Sweep every source that describes the domain, not just `docs/decisions/`. Each
drifts independently:

- **ADRs** for the domain, whatever their status.
- **`projects/<domain>/README.md`.**
- **Values-file comments.** In this repo these carry a large share of the real
  architecture, often the hardest-won part.
- **`.claude/CLAUDE.md`** rows touching the domain.
- **Open GitHub Issues** referencing the domain or its ADRs. Issues are this
  repo's source of truth for outstanding work, so a decision that looks
  unexecuted is often executed-and-tracked somewhere you have not looked. ADR
  `agents/020` read as an abandoned decision until issues #3831, #3832 and
  #3833 turned out to be its deferred-execution record.
- **CI guards** that assert something structural about it.

**Rank sources by recency and authorship, never by kind.** Stamp every source
in the ledger:

```bash
git log -1 --format=%cs -- <file>
```

A source Joe edited recently is a **pre-adjudicated claim to merge into**, not
stale prose to replace. In the `mcp` pilot the README was the freshest and
best-informed source in the domain, and the pass nearly overwrote it with a
worse summary. If `git log --since` shows he is mid-flight in this area, defer
the domain rather than race him.

### 2. Observe reality

Read the charts, the templates, the ArgoCD Application, and the code. Where a
claim is about runtime behaviour, read the datapath rather than the config.

Run `helm template` rather than trusting values. A green `ci test` proves
nothing about configuration.

**If the domain has a running workload, read the live object.** `kubectl` is
read-only here, and it is the cheapest way to collapse the chart-default,
deep-merge and `envFrom`-shadowing stack into one answer:

```bash
kubectl get pod -n <ns> -l <selector> \
  -o jsonpath='{range .spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}'
```

Know what the cluster can and cannot tell you:

- **Authoritative** for effective values. In the `mcp` pilot it showed
  `MCP_REQUIRE_AUTH=true` where `chart/values.yaml` says `"false"`, and
  surfaced `TRUST_PROXY_AUTH_DANGEROUSLY`, a key present in neither values file.
- **Silent** on reachability (is the branch consuming this key live) and on
  intent (is the inertness deliberate). Those are exactly the highest-value
  claims, and they only ever exist as adjudicated prose. This is why the
  document stays hand-written: a generated one would report a dead flag as live
  config, which is the failure this whole exercise exists to fix.

### 3. Build the ledger before writing any prose

| Claim | Source | Observed | Verdict |
|-------|--------|----------|---------|

Fill Claim, Source and Observed. Leave Verdict for Joe. Do not write a line of
`ARCHITECTURE.md` before the ledger is adjudicated: an unadjudicated claim
promoted into a current-state document is exactly the failure being fixed.

Claims that are directly verifiable from a committed file need no adjudication.
Mark them and move on.

### 4. Sweep for config that looks live and is not

A distinct category, and the one mechanical checks cannot reach. For every flag,
env var and values key the domain sets, ask both questions:

- Is anything **consuming** it?
- Is the branch that consumes it **reachable**?

`TRUST_PROXY_AUTH` is set, correctly spelled, genuinely read by the application,
and documented in the README. It is dead whenever `MCP_CLIENT_AUTH_ENABLED` is
true. Every mechanical check passes on it.

Separate two outcomes. A setting that does nothing is rot. A setting that is
deliberately inert until other work lands is legitimate, and must say so where
it is defined.

**Disposition: remove the dead key, and explain the removal in the PR body.** Do
not add prose to `ARCHITECTURE.md` about a thing that no longer exists. Git
history and the PR carry the archaeology.

### 5. Write ARCHITECTURE.md

Only adjudicated claims. Describe what **is**, never what was removed, what was
considered, or why some alternative was rejected. That belongs in an ADR or a PR
body.

**Compress and point, never transcribe.** State the invariant in a sentence or
two and name the file that carries the mechanism. The point-of-edit comments
stay canonical for how a specific key behaves; this document is canonical for
how the system behaves. Two copies of the same claim at the same level of
detail is a fork waiting to diverge, and the compression is what prevents it.
The `mcp` pilot runs about ten to one against its sources.

Never thin a config comment because the fact now appears here. That layer is
the one with real proximity pressure, and it is healthy.

End with an ADR map: decision, link, status, plus any issue that carries
deferred execution. That is how a decision stays reachable once nothing links
to it by path.

### 5a. Not every domain needs one

A domain earns an `ARCHITECTURE.md` when an agent could read a stale decision
as current state. Concretely, when either holds:

- it has an ADR trail whose decisions were partially executed or reversed, or
- the knowledge other work depends on spans three or more config files.

A domain failing both gets a good README as its entry point and nothing more.
`projects/design-system` is a tokens directory whose real document is
`.impeccable.md`; `projects/home-cluster` is a single kustomization file;
`projects/shared` is build glue. Record the exemption in #4667 so the inventory
stops reporting them as gaps.

### 6. Repoint inbound links

Anything referencing an ADR path in the domain now points at
`ARCHITECTURE.md`. Code comments, BUILD files, values files, README links.
Do this before deleting anything.

### 7. Delete the harvested ADRs

Last step, never a standalone sweep. An ADR is dropped only once
`ARCHITECTURE.md` carries its current state and inbound links are repointed.

Deprecated and Superseded ADRs go first. Draft and Accepted ones go only if the
ledger adjudicated them.

**An ADR category is not a domain, so check who else claims it.** Rollups are
partitioned by `projects/<domain>` while ADRs are partitioned by
`docs/decisions/<category>`, and the two do not line up.
`docs/decisions/agents/` alone holds 54 ADRs spanning mcp, the monolith agents
console, swarm and EmberVM. `agents/020` is claimed by both the mcp rollup and
a future monolith one.

So give the ledger a **harvested by** column, record the claim set in #4667, and
**delete a shared ADR only in the last rollup that claims it**. Otherwise the
second rollup of a shared category opens to find its sources already gone.

Check for danglers before committing: ADRs cross-link each other heavily, and
one public page deep-links an ADR.

### 8. Re-diff every cited source before merging

Rebase onto `main` and re-read every file the ledger cites. The ledger is
gathered on a branch while `main` keeps moving, and two separate near-misses in
the pilot were the same defect: ADR `agents/055` landed on main after the branch
was cut and was missing from the first ADR map, and `projects/mcp/README.md` was
rewritten on main by a PR that merged while the ledger was being written.

A rollup that merges without this step publishes a current-state document that
was already out of date when it landed.

## What does not change

Keep writing ADRs. They remain the full-rationale decision journal. They are
write-and-forget: nothing links to them, this runbook harvests them, and they
are deleted once harvested.

## Landing it

One PR per domain. Land against `main`, **never auto-merge**: the ledger
verdicts are Joe's, and the prose is a human-voice artifact he reviews.

Tracking issue: #4667.
