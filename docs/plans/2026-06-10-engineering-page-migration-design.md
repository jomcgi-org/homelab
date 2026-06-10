# Engineering Page Migration: Astro to Neo-Brutalist SvelteKit

**Date:** 2026-06-10
**Status:** Approved

## Goal

Migrate the engineering portfolio page from the legacy Astro site
(`projects/websites/jomcgi.dev/src/pages/engineering.astro`, served at
`jomcgi.dev/engineering/`) to a new route in the monolith SvelteKit frontend
at `public.jomcgi.dev/engineering`, restyled in the site's neo-brutalist
design system and with content refreshed to match what actually runs in the
homelab today.

The Astro site stays untouched and live. Decommissioning it is a separate,
later task.

## Architecture and placement

- New route: `projects/monolith/frontend/src/routes/public/engineering/+page.svelte`.
- Nav change: the ENGINEERING item in `src/lib/public/components/Nav.svelte`
  switches from the external `https://jomcgi.dev/engineering/` href to
  `/engineering`. Its existing `slug: "engineering"` active-state logic then
  works without changes.
- Content lives in a data module `engineering-data.js` next to the page,
  mirroring the `cv/cv-data.js` pattern. One array of project entries:
  `id, title, tag, oneLiner, motivation, facts[], links[], diagram`.
  The page maps over it for both the expo grid and the deep-dive sections,
  so future content edits are data edits, not markup surgery.

## Page structure

1. **Hero**: "ENGINEERING" in Instrument Serif, sticker tags (e.g.
   `12 SYSTEMS`, `BARE METAL`, `GITOPS`), a one-line intro, and the existing
   Marquee component as a tech-stack ticker.
2. **Expo grid**: `card-hard` cards, one per project, each with a mono
   category sticker (AGENTS / DATA / OPERATORS / APPS / BUILD), the
   one-liner, and an anchor jump to its deep-dive section. Color-coded by
   category using the existing accent palette.
3. **Deep-dive sections**: one per project, keeping the old page's proven
   shape but restyled: motivation callout (accent-bordered box), hand-built
   diagram, facts table (the old "execution table" as a 2px-ruled definition
   grid), and `btn`-styled links to live sites and repo paths.

## Roster

11 deep dives (8 refreshed, 3 new, 1 dissolved):

**New:**

- **Agent Platform** (flagship): sandboxed Claude/Goose agents in Kubernetes
  pods, Go orchestrator over NATS JetStream, Context Forge MCP gateway with
  per-team RBAC. Absorbs the inference half of the old "Self-Hosted AI
  Stack" section (vLLM on the 4090, current Qwen MoE model) as the substrate
  it serves.
- **Knowledge Graph**: LLM decomposition pipeline with self-critique, voyage
  embeddings, pgvector semantic search, MCP tools, Cmd+K search overlay.
  Absorbs the knowledge half of the old AI Stack section.
- **Loom**: framed explicitly as pre-alpha, collaborative, soon-to-be
  open-source. Rust + DataFusion + DuckLake typed-object data platform with
  governance treated as an STPA safety property.

**Refreshed:** Sextant, OCI Model Cache Operator, Cloudflare Operator,
Trips, Ships, Stargazer, Bazel, rules_semgrep.

**Dissolved:** "Self-Hosted AI Stack". It is the stale section (names a
retired model, says Qdrant where production is pgvector); its two halves
move into Agent Platform and Knowledge Graph as above.

## Diagrams

No Mermaid, no CDN dependency. A small set of shared primitives in
`src/lib/public/components/diagrams/`:

- `DiagramBox`: 2px ink border, hard shadow, mono label, accent fill by role.
- A lane/flow layout that stacks vertically on mobile, with SVG or glyph
  arrows between stages.

Each project gets a thin hand-authored diagram component composing those
primitives. Explicit markup per diagram is the point: it reads as
intentional design rather than auto-generated layout.

## Content accuracy

All copy is fact-checked against the repo and CV during writing. Known
corrections to carry in:

- Sextant: restored, with a CI drift guard over the oci-model-cache state
  machine; spec colocated in `internal/statemachine/`.
- Inference: vLLM serving the current Qwen MoE model on the 4090 (the old
  page names a retired model).
- Knowledge graph: pgvector + voyage embeddings (not Qdrant).
- Loom: pre-alpha status stated plainly; public positioning only.

## Error handling and testing

- Fully static page, no data fetching: error handling is nil.
- Vitest: a small test that every roster entry has the required fields and
  unique anchor ids.
- Existing `build_test` covers the build.
- Visual verification via screenshots before the PR.
- No chart version bump needed unless deploy values change; the frontend
  image rebuild flows through CI as usual.
