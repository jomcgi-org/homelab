# Design

A catalog of designers and artists worth studying. One file per person. Each entry has to point to a source.

## Structure

```
design/
  README.md          index and categories
  designers/         one file per designer
  artists/           one file per artist
```

## Categories

Every designer gets tags on three axes.

| Axis        | Values                                                     |
|-------------|------------------------------------------------------------|
| Discipline  | `industrial` · `editorial` · `graphic` · `digital` · `type` · `painting` |
| Density     | `reductive` ↔ `maximal`                                     |
| Mode        | `systematic` · `conceptual` · `humorous` · `observational`  |

## Index

| Person                                | Discipline              | Density   | Mode                    |
|---------------------------------------|-------------------------|-----------|-------------------------|
| [Dieter Rams](designers/dieter_rams.md) | industrial              | reductive | systematic              |
| [Tracy Ma](designers/tracy_ma.md)       | editorial, graphic      | maximal   | conceptual, humorous    |
| [Caroline Walker](artists/caroline_walker.md) | painting          | reductive | observational           |

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
