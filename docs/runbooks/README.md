# Runbooks

Operational and agent procedures that are **explicit-only**.

Unlike `.claude/skills/`, runbooks are **not** auto-selected by description
matching. An agent (or a claude.ai routine) opens a runbook only when:

1. Joe asks for that procedure by name or intent, or
2. A Context Loading Rule in `.claude/CLAUDE.md` names the file, or
3. A routine YAML under `projects/monolith/claude_routines/` points at the path.

## Index

| Runbook | When to open |
|---------|----------------|
| [argocd-outofsync.md](argocd-outofsync.md) | ArgoCD OutOfSync / "is my change live?" |
| [public-tier-checklist.md](public-tier-checklist.md) | Public tier / jomcgi.dev / `public_reader` changes |
| [embervm-node-scratch-setup.md](embervm-node-scratch-setup.md) | EmberVM node scratch provisioning |
| [embervm-stateful-generation-quarantine.md](embervm-stateful-generation-quarantine.md) | Stateful generation quarantine |
| [knowledge/gardener.md](knowledge/gardener.md) | Knowledge gardener (also: hourly routine) |
| [knowledge/classify.md](knowledge/classify.md) | Gap classify routine |
| [knowledge/research.md](knowledge/research.md) | Gap research routine |
| [knowledge/distill.md](knowledge/distill.md) | Distill completed tasks |
| [knowledge/consolidate.md](knowledge/consolidate.md) | Daily/weekly task rollups |
| [improve-ambient/runbook.md](improve-ambient/runbook.md) | `/improve-ambient` feedback loop |
| [improve-artifacts/runbook.md](improve-artifacts/runbook.md) | `/improve-artifacts` |
| [improve-recipes/runbook.md](improve-recipes/runbook.md) | `/improve-recipes` |
| [improve-safeguards/runbook.md](improve-safeguards/runbook.md) | `/improve-safeguards` |

## Format

```markdown
---
name: short-slug
invoke: explicit
summary: one line
---

> **Runbook (explicit-only).** ...

# Title
...
```
