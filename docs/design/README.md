# Design

A catalog of designers worth studying. One file per designer. Each entry has to point to a source.

## Structure

```
design/
  README.md          index and categories
  designers/         one file per designer
```

## Categories

Every designer gets tags on three axes.

| Axis        | Values                                                     |
|-------------|------------------------------------------------------------|
| Discipline  | `industrial` · `editorial` · `graphic` · `digital` · `type` |
| Density     | `reductive` ↔ `maximal`                                     |
| Mode        | `systematic` · `conceptual` · `humorous`                    |

## Index

| Designer                              | Discipline              | Density   | Mode                    |
|---------------------------------------|-------------------------|-----------|-------------------------|
| [Dieter Rams](designers/dieter_rams.md) | industrial              | reductive | systematic              |
| [Tracy Ma](designers/tracy_ma.md)       | editorial, graphic      | maximal   | conceptual, humorous    |

## Entry template

```markdown
---
name:
tags: { discipline: [], density: , mode: [] }
---

# Name

One line: what they're known for.

## Key works
- **Work** (year, client): what to look at. [source](url)

## Ideas
- Their idea, paraphrased. [source](url)

## References
- [Title](url): what the source is
```
