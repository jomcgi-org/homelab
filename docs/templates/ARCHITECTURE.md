# <Domain> architecture

<!--
Template for projects/<domain>/ARCHITECTURE.md. Rules: docs/writing.md
"Document types". Delete every comment before you commit.
Limits: 600 lines for the file, 60 lines for a section without a diagram.
-->

<Five sentences or fewer. What the domain does, for whom, and where it runs.>

```mermaid
flowchart LR
  A[caller] -- "verb or protocol" --> B[component]
  B -- "verb or protocol" --> C[store]
```

| Component | Role | Code | Pin |
| --- | --- | --- | --- |
| <name> | <one line> | `<path>` | <version, stated only here> |

## <Component or flow>

<!-- One section for each component. Present tense: what runs now. -->

<What it does. Draw each flow, state machine and trust boundary.>

**Why.** <The reason, the option rejected, and a link to the decision.>

## Direction

Decided and not yet live. A row leaves when the work ships or its issue
closes.

| Direction | Tracks | State |
| --- | --- | --- |
| <outcome> | HOME-N | <planned, or staged off behind `<flag>`> |

## Decision history

| Decision | Date | Record |
| --- | --- | --- |
| <one line> | <YYYY-MM-DD> | <link to Joe's comment or the HOME issue> |
