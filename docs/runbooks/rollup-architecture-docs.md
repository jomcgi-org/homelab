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
- **`projects/<domain>/README.md`.** Expect this to be the worst offender. It is
  read first and trusted most, so its drift does the most damage.
- **Values-file comments.** In this repo these carry a large share of the real
  architecture, often the hardest-won part. Harvest them.
- **`.claude/CLAUDE.md`** rows touching the domain.
- **CI guards** that assert something structural about it.

### 2. Observe reality

Read the charts, the templates, the ArgoCD Application, and the code. Where a
claim is about runtime behaviour, read the datapath rather than the config.

Run `helm template` rather than trusting values. A green `ci test` proves
nothing about configuration.

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

End with an ADR map: decision, link, status. That is how an ADR stays reachable
by pointer once nothing links to it by path.

### 6. Repoint inbound links

Anything referencing an ADR path in the domain now points at
`ARCHITECTURE.md`. Code comments, BUILD files, values files, README links.
Do this before deleting anything.

### 7. Delete the harvested ADRs

Last step, never a standalone sweep. An ADR is dropped only once
`ARCHITECTURE.md` carries its current state and inbound links are repointed.

Deprecated and Superseded ADRs go first. Draft and Accepted ones go only if the
ledger adjudicated them.

Check for danglers before committing: ADRs cross-link each other heavily, and
one public page deep-links an ADR.

## What does not change

Keep writing ADRs. They remain the full-rationale decision journal. They are
write-and-forget: nothing links to them, this runbook harvests them, and they
are deleted once harvested.

## Landing it

One PR per domain. Land against `main`, **never auto-merge**: the ledger
verdicts are Joe's, and the prose is a human-voice artifact he reviews.

Tracking issue: #4667.
