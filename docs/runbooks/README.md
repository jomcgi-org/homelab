# Runbooks

Operational and agent procedures that are **explicit-only**.

Unlike the job procedures in `docs/agents/` (each listed with its trigger in `AGENTS.md`), runbooks are **not** selected by
description matching. Open one only when:

1. Joe asks for that procedure by name or intent,
2. a "Where to look next" row in `AGENTS.md` names the file, or
3. a task spec or routine prompt names it.

## Index

One row per entry in this directory.

### Ops / cluster

| Runbook | When |
|---------|------|
| [argocd-outofsync.md](argocd-outofsync.md) | ArgoCD OutOfSync / "is my change live?" |
| [public-tier-checklist.md](public-tier-checklist.md) | Public tier / jomcgi.dev / `public_reader`, including the shared response cache contract |
| [embervm-stateful-generation-quarantine.md](embervm-stateful-generation-quarantine.md) | A stateful EmberVM workload refuses to wake with `volume quarantined` |
| [threat-model-maintenance.md](threat-model-maintenance.md) | Add/close a `security-finding`, refresh `docs/THREAT-MODEL.md`, per-domain security lenses in each `STPA.md`, model review |

### Improve loops (explicit)

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
section of `AGENTS.md`. Local Claude Code and Codex session collection is
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
