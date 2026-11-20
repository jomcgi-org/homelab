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
| [public-tier-checklist.md](public-tier-checklist.md) | Public tier / jomcgi.dev / `public_reader`, including the shared response cache contract |
| [embervm-stateful-generation-quarantine.md](embervm-stateful-generation-quarantine.md) | A stateful EmberVM workload refuses to wake with `volume quarantined` |
| [EmberVM hub removal](../../projects/embervm/dev/deploy/README.md#hub-removal-home-476) | After HOME-476 merges: delete the `embervm-dev` namespace, RBAC and a leftover CRD; check the brick pool |
| [private-action-synthetics.md](private-action-synthetics.md) | Verify the deployed factory status reader with `POST /api/swarm/factory/synthetics` |
| [threat-model-maintenance.md](threat-model-maintenance.md) | Record or close a security finding, change `docs/THREAT-MODEL.md`, per-domain security lenses in each `STPA.md`, model review |

### Improve loops (explicit)

Ask for these procedures by name or read the linked runbook when explicitly
requested. The slash commands are Joe's local Claude Code commands; this
repository does not install them.

| Runbook | When |
|---------|------|
| [improve-ambient/](improve-ambient/runbook.md) | `/improve-ambient`; a directory holding `runbook.md` and `scripts/improve_ambient_tool.py` |
| [improve-safeguards/](improve-safeguards/runbook.md) | `/improve-safeguards`; a directory holding `runbook.md` and `scripts/improve_safeguards_tool.py` |

### Repo / agents

| Runbook | When |
|---------|------|
| [daily-digest.md](daily-digest.md) | Outstanding work digest (routine + on demand) |
| [refresh-structure-docs.md](refresh-structure-docs.md) | Root README structural refresh |
| [fixture-previews.md](fixture-previews.md) | Preview the public 4090 blog page from a PR commit with fixture data |
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
