# Plan: Docs site in the monolith at jomcgi.dev/docs/\*

**Branch:** `feat/monolith-docs-site`
**ADR:** [docs/002](../decisions/docs/002-websites-decommission-docs-into-monolith.md)
**Created:** 2026-06-19

## Goal

Serve the repository documentation from the `monolith-public` SvelteKit tier at
`jomcgi.dev/docs/*`, in the neobrutalist public style, reached from a **Docs**
navbar link. This plan covers only the **non-destructive (additive)** half of
ADR 002: stand up and deploy the new docs site. The old `projects/websites/`,
`trips/frontend`, and `hikes/frontend` stay live and untouched here; their
removal is a separate draft deprecation PR after this is verified live.

## Approach (simplest that satisfies the ADR)

Mirror the existing `repo_docs_manifest` pattern: a generator script globs the
**public-allowlisted** docs, emits a committed manifest with full bodies inline,
and a SvelteKit `/docs/[...slug]` route renders the markdown server-side with
`marked` (already a frontend dependency). Bazel hermeticity rules out reaching
repo-root `docs/` from a Vite glob, so a committed generated artifact is the
clean path, and it is CI-validated fresh exactly like the repo_docs manifest.

Security (ADR 002): the manifest is built from an **explicit allowlist**, never
the RAG ingest (which indexes internal docs). Excluded: `.claude/`,
`docs/plans/`, CV/personal assets, non-doc trees.

## Tasks

### Task 1: Public docs manifest + generator

- Add `projects/monolith/frontend/tools/gen_docs_manifest.py` (or colocate with
  the knowledge generator), globbing the allowlist: `docs/*.md`,
  `docs/decisions/**/*.md` (incl `index.md`), and the top-level reference docs
  (`security`, `services`, `observability`, `observability-alerting`,
  `contributing`, `agents`). Exclude `.claude/`, `docs/plans/`, CV, READMEs not
  meant for publication.
- Emit a committed `src/lib/public/docs/docs-manifest.json`: array of
  `{ path, slug, title, section, order, content }`. Title from the first H1;
  section from the top-level dir; ADRs grouped by category with their numeric
  order.
- Deterministic output (git-ls-files ordering, like the repo_docs generator) so
  CI can assert freshness. Wire a `validate-generate-scripts.sh` check.
- Unit test mirroring `gen_repo_docs_manifest_test.py`.

**Acceptance:** running the generator on a clean tree is a no-op diff; the
allowlist excludes internal paths; the manifest contains the ADR tree + ref docs.

### Task 2: `/docs` route + SSR markdown rendering

- `routes/public/docs/+page.server.js` (index) and
  `routes/public/docs/[...slug]/+page.server.js`: load the manifest server-side,
  resolve the slug, return `{ html, toc, sidebar, meta }`. Render markdown ->
  HTML with `marked` on the server; never ship the manifest to the client.
- Code highlighting and mermaid: reuse whatever the chat/engineering pages use
  (`marked` + the existing mermaid path). Internal links between docs rewritten
  to `/docs/...` slugs; links to excluded files rendered as plain text (ADR-001
  link-stripping behaviour, preserved).
- `404` for unknown slugs via the public `+error.svelte`.

**Acceptance:** `/docs` lists the tree; `/docs/security`, `/docs/decisions/...`
render with working intra-doc links and code blocks; no manifest bytes in the
client bundle (check the built output).

### Task 3: Neobrutalist styling + sidebar + navbar link

- A docs layout using `lib/public/styles/design-system.css` tokens and the
  `Brutalist*`/`Stamp` component vocabulary: hard borders, offset shadows, the
  yellow accent. Sidebar nav (sections + ADR categories), a content column with
  a right-hand in-page TOC.
- Add a **Docs** entry to the shared public `Nav.svelte` (the same nav that
  carries Trips/engineering), consistent placement and hover accent.

**Acceptance:** the page reads as part of the public site, not a bolt-on;
sidebar reflects the manifest tree; navbar link routes to `/docs`.

### Task 4: ADR sidebar generation rehome (additive side)

- Make the monolith docs route derive the ADR tree from the manifest, removing
  the build-time dependency on `docs.jomcgi.dev/.vitepress/adr-sidebar.json` for
  the _new_ surface. (The old `config_links_test` gate is retired only in the
  destructive PR; here we just stop the new surface from needing it.)

**Acceptance:** adding a new ADR and regenerating the docs manifest surfaces it
in `/docs/decisions/` with no VitePress involvement.

### Task 5: Wire, deploy, verify live

- BUILD wiring for the generator + any new route assets; `format`/gazelle.
- Bump `projects/monolith-public/chart/Chart.yaml` + `deploy/application.yaml`
  `targetRevision` together (chart-version-bot keeps them in sync, but verify).
- Push, watch CI, merge, confirm ArgoCD sync, then verify `jomcgi.dev/docs` and
  a few deep links return 200 with rendered content.

**Acceptance:** `jomcgi.dev/docs/*` live (200), styled, navigable; old sites
still up and untouched.

## Out of scope (separate draft deprecation PR)

Deleting `projects/websites/`, `trips/frontend`, `hikes/frontend`, the hikes
data tools, `push_all_pages`, `bazel/vitepress`, the CF Pages projects, and the
`config_links_test`/`generate-docs-sidebar.sh` gate; relocating
`websites/shared:css`; old-subdomain redirects. None of that happens until
`/docs/*` is verified live.

## Risks / watch-items

- Manifest size in the SSR bundle: keep it server-only; if large, lazy-read per
  slug rather than importing the whole array into every load.
- `marked` HTML sanitization: docs are first-party, but render with a consistent
  config and avoid raw HTML passthrough surprises.
- Chart bump rebase churn (chart-version-bot re-bumps on push) - resolve to
  bot-version+1 as usual.
- New plan doc + manifest must be regenerated/registered so Format + repo_docs
  freshness checks pass.
