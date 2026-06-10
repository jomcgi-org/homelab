---
name: adr
description: Use when creating, reviewing, or working with ADRs (Architecture Decision Records). Creates structured decision documents in docs/decisions/<category>/ that record what was decided and why. ADRs are rationale, not implementation plans.
---

# ADR: Architecture Decision Records

ADRs record architectural decisions and the reasoning behind them. Unlike ephemeral RFCs, ADRs persist even when superseded, preserving the "why" behind decisions and their evolution.

**ADRs are rationale, not implementation plans.** No phase checklists, no task lists, no Go/No-Go gates. Implementation work is tracked in plans (`docs/plans/`) or PRs, never in the ADR. An ADR is done when it explains what was decided and why; it does not track whether the work shipped.

## Location

ADRs live in `docs/decisions/<category>/` as numbered Markdown files.

Discover current categories with `ls docs/decisions/` (do not assume; new categories are created as needed and should be broad enough to be useful).

## Usage

```
/adr create <category> <slug>   # Scaffold a new ADR
/adr list                       # List existing ADRs
/adr <category>/<number>        # Read and summarize an ADR
```

## Creating a New ADR

### Step 1: Determine the number

Look at the highest-numbered file in the target category and increment by one. If the category doesn't exist yet, start at 001. Numbers are never reused.

```bash
ls docs/decisions/<category>/
```

### Step 2: Create the file

Create `docs/decisions/<category>/NNN-<slug>.md` using this template:

````markdown
# ADR NNN: <Title>

**Author:** <Name>
**Status:** Draft
**Created:** <YYYY-MM-DD>
**Supersedes:** <link to previous ADR, if applicable>

---

## Problem

What problem does this solve? Why now?

---

## Decision

What was decided, in 2-3 paragraphs. Include a Before/After table if helpful:

| Aspect | Today | Decided |
| ------ | ----- | ------- |
| ...    | ...   | ...     |

---

## Architecture

High-level design. Use mermaid diagrams for data flow:

```mermaid
graph LR
    A[Service A] --> B[Service B]
    B --> C[Service C]
```
````

## Alternatives Considered

What else was on the table, and the one-line reason each was rejected.

## Security

Reference `docs/security.md` for baseline. Document any deviations.

## Risks

| Risk | Likelihood | Impact | Mitigation |
| ---- | ---------- | ------ | ---------- |
| ...  | ...        | ...    | ...        |

## Open Questions

1. Unresolved design decisions

## References

| Resource    | Relevance      |
| ----------- | -------------- |
| [Link](url) | Why it matters |

````

### Step 3: Commit with conventional commit format

```bash
git commit -m "docs(adr): <short description>"
````

## ADR Statuses

| Status                | Meaning                                                                      |
| --------------------- | ---------------------------------------------------------------------------- |
| **Draft**             | Under discussion, not yet decided                                            |
| **Accepted**          | The decision is made. Set this when the decision lands, not when work ships. |
| **Superseded by NNN** | Replaced by a newer ADR (link to it). Keep the file; it preserves context.   |
| **Deprecated**        | Abandoned without replacement                                                |

## Superseding an ADR

When a decision is reversed or evolved:

1. Create the new ADR with a `Supersedes:` field linking to the old one
2. Update the old ADR's status to `Superseded by [NNN-slug](NNN-slug.md)`
3. **Do not delete the old ADR**; it preserves the reasoning and context that led to the change

## Conventions

- **File naming**: `docs/decisions/<category>/NNN-<kebab-case-slug>.md`
- **Numbering**: Sequential within each category (001, 002, ...). Numbers are never reused.
- **Commit prefix**: `docs(adr):` for new ADRs and updates
- **Diagrams**: Mermaid for all architecture and flow diagrams (renders natively on GitHub)
- **Sections**: Problem, Decision, Architecture, Alternatives, Security, Risks, References
- **No work tracking**: implementation lives in `docs/plans/` and PRs, not in the ADR
