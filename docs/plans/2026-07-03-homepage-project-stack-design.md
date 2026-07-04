# Homepage Project Stack (replaces SLO topology view)

**Date:** 2026-07-03
**Status:** Approved design

## Problem

The jomcgi.dev front page currently shows a live service-topology diagram with SLO
drill-downs (`HomepageTopology.svelte` + `DagRenderer.svelte` + dagre layout, fed by
the `observability.topology_snapshot` Postgres table). It is accurate but reads as an
operator dashboard. The primary audience is visitors and recruiters: the page should
tell the story of what has been built, not the current error budgets.

## Decision

Replace the topology section with a **stratified stack**: a geological-cutaway style
diagram with hand-curated content. Apps sit on top; platform, compute, and metal
strata sit beneath. Projects get editorial story cards with links to the live app and
the GitHub readme. No SLO machinery, no live health dots, fully static config.

Chosen over: (B) same but with live stat garnish woven into strata labels (deferred,
cheap to add later), and (C) build-time derivation of the project list from
`projects/*/deploy/` (over-engineering for the churn rate; the curated copy is the
value and is inherently hand-written).

## Content model

One curated config file `projects/monolith/frontend/src/lib/public/homepage-stack.js`:

- `stack`: ordered array of layers, top to bottom: `apps`, `platform`, `compute`, `metal`.
- Each layer has `id`, `label`, `kind`, `items`.
- `kind: 'projects'` (apps layer only): items are story cards with
  `id`, `name`, `blurb` (one line, what it is), `engineering` (one sentence, the
  interesting bit), `tags` (tech), `links.live` (app URL) and `links.readme`
  (GitHub `tree/main/projects/<dir>` URL).
- `kind: 'strip'`: items are labeled chips, optionally `href` to the upstream project
  or an ADR. No card UI. This split is deliberate: story cards are only for things
  built here, not things merely run here.

Initial app roster: ships, stars, grimoire, chat, knowledge graph, agent platform,
trips, dr-jobs, worldcup, docs site. Strips: platform (ArgoCD, Linkerd, SigNoz,
Envoy Gateway, 1Password operator), compute (k8s, Firecracker, Longhorn, SeaweedFS,
GPU/vLLM), metal (node count, GPU, Cloudflare edge).

## Components

- `HomepageStack.svelte` replaces `HomepageTopology.svelte` on the public homepage.
  Pure CSS grid: one full-width row per stratum with hairline separators; the apps
  row wraps as a card grid. No SVG, no dagre.
- `StackProjectCard.svelte`: compact by default (name, blurb, tags); expands in place
  when selected to show the engineering sentence plus two buttons: "Visit live" and
  "Read the code" (GitHub readme).
- Visual language: existing design system only. 2px ink borders, hard shadows,
  cream/paper alternation per stratum, layer label tabs in the left gutter matching
  the current MONOLITH/CLUSTER group tab style, yellow accent on the active card,
  JetBrains Mono labels.

## Interaction

- Card click selects and mirrors to `?project=<id>` (same URL-state pattern as the
  current `?node=` param). Escape or re-click clears. Cards are deep-linkable
  (`jomcgi.dev/?project=ships`).
- Hover lifts the card (shadow offset grows), consistent with existing buttons.
- No calls to `/api/home/observability/topology`. The top stats marquee keeps its
  existing `/api/home/observability/stats` feed untouched.

## Removal scope

- Homepage stops importing `HomepageTopology.svelte` and `HomepageNodeDetail.svelte`.
- Delete those plus `DagRenderer.svelte` and `dag-layout.js` only if nothing else
  imports them (verify at implementation time); drop `@dagrejs/dagre` from
  package.json if it becomes unused.
- Backend topology endpoint, rollup job, and snapshot table stay untouched in this
  PR. If the homepage was their sole consumer, decommissioning is a follow-up PR.

## Testing

- Visual regression (`frontend/visual/`): replace the topology scenario with two
  stack scenarios (default view, one card expanded). Deterministic CSS layout should
  be less flaky than the dagre render.
- Config is static data; no new backend tests.
