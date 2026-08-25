# Runbooks

Operational and agent procedures that are **explicit-only**.

Unlike `.claude/skills/` (small auto-matched set: `ship`, `adr`, `stpa`,
`codex-implement`, `pr-workflow`, `ci-triage`), runbooks are **not** selected by
description matching. Open only when:

1. Joe asks for that procedure by name or intent, or
2. A "Where to look next" row in `.claude/CLAUDE.md` names the file, or
3. A routine under `projects/monolith/claude_routines/` points at the path.

## Index

### Ops / cluster

| Runbook | When |
|---------|------|
| [argocd-outofsync.md](argocd-outofsync.md) | ArgoCD OutOfSync / "is my change live?" |
| [public-tier-checklist.md](public-tier-checklist.md) | Public tier / jomcgi.dev / `public_reader` |
| [embervm-node-scratch-setup.md](embervm-node-scratch-setup.md) | EmberVM node scratch |
| [embervm-stateful-generation-quarantine.md](embervm-stateful-generation-quarantine.md) | Stateful generation quarantine |
| [scheduler.md](scheduler.md) | Kick / inspect Postgres scheduled jobs |
| [threat-model-maintenance.md](threat-model-maintenance.md) | Add/close a `security-finding`, refresh `docs/THREAT-MODEL.md`, per-domain security lenses in each `STPA.md`, model review |

### Knowledge graph

| Runbook | When |
|---------|------|
| [knowledge/search.md](knowledge/search.md) | Search/debug the graph (`homelab knowledge`) |
| [knowledge/gardener.md](knowledge/gardener.md) | Decompose raws (hourly routine) |
| [knowledge/classify.md](knowledge/classify.md) | Gap classify routine |
| [knowledge/research.md](knowledge/research.md) | Gap research routine |
| [knowledge/distill.md](knowledge/distill.md) | Distill completed tasks |
| [knowledge/consolidate.md](knowledge/consolidate.md) | Daily/weekly rollups |

### Improve loops (explicit)

| Runbook | When |
|---------|------|
| [improve-ambient/runbook.md](improve-ambient/runbook.md) | `/improve-ambient` |
| [improve-safeguards/runbook.md](improve-safeguards/runbook.md) | `/improve-safeguards` |

### Repo / agents

| Runbook | When |
|---------|------|
| [daily-digest.md](daily-digest.md) | Outstanding work digest (routine + on demand) |
| [refresh-structure-docs.md](refresh-structure-docs.md) | Root README structural refresh |
| [rollup-architecture-docs.md](rollup-architecture-docs.md) | Roll one domain's ADRs up into `ARCHITECTURE.md`, then drop them |
| [update-claude-routines.md](update-claude-routines.md) | Sync claude.ai routines from YAML |
| [bazel.md](bazel.md) | BUILD/gazelle patterns; CI is via `ci` / Workflows |
| [apko.md](apko.md) | apko.yaml + `apko_image` (locks via pre-commit / script) |

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
