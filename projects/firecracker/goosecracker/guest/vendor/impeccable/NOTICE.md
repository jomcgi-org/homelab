# Notice

Impeccable
Copyright 2025 Paul Bakaus

## Anthropic frontend-design Skill

The `frontend-design` skill in this project builds on Anthropic's original frontend-design skill.

**Original work:** https://github.com/anthropics/skills/tree/main/skills/frontend-design
**Original license:** Apache License 2.0
**Copyright:** 2025 Anthropic, PBC

This project extends the original with:
- 7 domain-specific reference files (typography, color-and-contrast, spatial-design, motion-design, interaction-design, responsive-design, ux-writing)
- 20 steering commands
- Expanded patterns and anti-patterns

## Vendoring in this repo

The 7 reference files under this directory are vendored unmodified from
impeccable's `frontend-design` skill (Apache-2.0, see LICENSE). They are baked
into the goosecracker guest image at `/opt/impeccable/` so the artifact recipes
can consult them on demand. Only the reference files and this NOTICE are shipped
in the image; nothing else from impeccable is included.
