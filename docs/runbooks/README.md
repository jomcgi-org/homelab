# Runbooks

Operational and agent procedures that are **explicit-only**.

Unlike the job procedures in `docs/agents/` (each listed with its trigger in `docs/agents/reference.md`), runbooks are **not** selected by
description matching. Open one only when:

1. Joe asks for that procedure by name or intent,
2. a "Where to look next" row in `docs/agents/reference.md` names the file, or
3. a task spec or routine prompt names it.

## Operational runbook stages

1. **Start with a minimal runbook.** State when to use it, how to check the
   problem, what to do, and how to verify recovery.
2. **Follow up with interactive steps.** File a HOME issue for useful links,
   checks, or action buttons. Show the target and planned change before an
   operator confirms an action that changes state.
3. **Evaluate automated remediation.** If the case is unclear, defer a HOME
   issue until two weeks after the interactive steps go live. Review how often
   operators used the action, how often it worked, and what went wrong.
   Automate when the evidence shows that the trigger and recovery are reliable
   and the action saves enough operator time.

## Index

One row per procedure: a file in this directory, or a named section of a
project README.

### Ops / cluster

| Runbook | When |
|---------|------|
| [argocd-outofsync.md](argocd-outofsync.md) | ArgoCD OutOfSync / "is my change live?" |
| [threat-model-maintenance.md](threat-model-maintenance.md) | Record or close a security finding, change `docs/THREAT-MODEL.md`, per-domain security lenses in each `STPA.md`, model review |

### Repo / agents

| Runbook | When |
|---------|------|
| [daily-digest.md](daily-digest.md) | Outstanding work digest (routine + on demand) |
| [refresh-structure-docs.md](refresh-structure-docs.md) | Root README structural refresh |
| [apko.md](apko.md) | apko.yaml + `apko_image` (locks via pre-commit / script) |

Bazel and CI debugging live in `docs/agents/ci-triage.md` and the Commands
section of [`docs/agents/reference.md`](../agents/reference.md#commands). Local Claude Code and Codex session collection is
documented in [`tools/session_collector/README.md`](../../tools/session_collector/README.md).

## Format

```markdown
---
name: short-slug
invoke: explicit
summary: one line
---

> **Runbook (explicit-only).** ...

# Title
```
