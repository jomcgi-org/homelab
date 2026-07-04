# Docs site: ADRs + project READMEs, drop the Reference tier

## Motivation

The public docs site at jomcgi.dev/docs publishes 8 hand-written reference docs
(`docs/agents.md`, `event-bus.md`, etc.) that rot: they live far from the code
they describe, so nothing forces updates (agents.md carries a "Legacy note"
apologizing for describing decommissioned components). ADRs are append-only
decisions and never rot; project READMEs are colocated with code and get
updated by proximity pressure. The public site should publish only those two
self-maintaining tiers.

The reference docs stay in the repo as internal docs (CLAUDE.md context-loading
rules and the RAG `repo_docs` ingest depend on them); they just stop being
published publicly. Public exposure of READMEs is approved (public-by-default).

## Manifest schema contract (shared between tasks 1 and 2)

Entry fields unchanged: `{path, slug, title, section, order, content}`.

- `section` is now `"Projects"` or `"Decisions"` (no more `"Reference"`).
- ADR slugs unchanged: `decisions/...`, `decisions/index.md` -> `decisions`.
- README slugs: `projects/firecracker/README.md` -> `projects/firecracker`,
  `projects/firecracker/goosecracker/README.md` ->
  `projects/firecracker/goosecracker` (README.md collapses to its directory,
  same as index.md).
- Sort order: Projects section first (alphabetical by path), then Decisions
  exactly as today.

## Task 1: manifest generator

Files: `projects/monolith/knowledge/tools/gen_docs_manifest.py`,
`projects/monolith/knowledge/gen_docs_manifest_test.py`, regenerated
`projects/monolith/frontend/src/lib/public/docs/docs-manifest.json`.

- `_should_index`: keep `docs/decisions/**/*.md`; drop the top-level
  `docs/*.md` branch; add `projects/**/README.md` with a vendored-subtree
  blocklist (prefix match): `projects/platform/linkerd/charts/`.
- `make_slug`: collapse a trailing `/README` like `/index` (case-sensitive,
  filename is always `README.md`).
- `derive_title`: for a README with no H1, fall back to the directory name,
  not "README".
- `section_for` / `_sort_key` per the contract above.
- Update the test for the new allowlist/slug/sort behavior.
- Regenerate the manifest with plain `python3`.

## Task 2: sidebar + rendering (frontend)

Files: `projects/monolith/frontend/src/lib/server/docs.js`,
`projects/monolith/frontend/src/routes/public/docs/DocsShell.svelte`,
`DocsSearch.svelte` (if its groups need it), `+page.server.js` (index copy),
`+page.svelte` (index overview, if it references reference docs).

- `buildPathIndex`: alias `.../README.md` to its directory path (same as the
  existing `/index.md` alias) so relative intra-repo links resolve.
- `buildSidebar`: replace `reference` with a nested `projects` tree derived
  from slugs: `{name, title, slug, children: [...]}`. A README at
  `projects/firecracker` is a node; `projects/firecracker/goosecracker` nests
  under it. A child whose parent has no README still nests under a titled
  group node (slug null). Keep `decisions` output shape unchanged.
- `DocsShell.svelte`: render the Projects tree (collapsible groups, same
  accordion pattern as ADR categories), remove the Reference list, update the
  search-index flattening and the top-nav "Architecture" link (currently
  points at a reference doc) to something that still exists (e.g. the projects
  tree root or the decisions index).
- Update the /docs index page description ("Reference documentation and
  architecture decision records" -> reflect the new shape).

## Task 3: mermaid rendering (after task 2)

Files: frontend `package.json` + pnpm lock, `docs.js` code renderer,
`[...slug]/+page.svelte`.

- Add `mermaid` as a frontend dependency (client-only, dynamic import; regen
  the lockfile offline with the vendored pnpm).
- `docs.js`: render a ` ```mermaid ` fence as
  `<pre class="doc-code doc-mermaid" data-lang="mermaid"><code>...</code></pre>`
  (escaped source, as today, plus the marker class).
- `[...slug]/+page.svelte`: after mount and on doc change, if the rendered doc
  contains `.doc-mermaid`, `await import("mermaid")` and render each block to
  SVG in place; on a render error keep the source block. The dynamic import
  keeps mermaid out of the initial bundle; it must never run during SSR.

## Verification

No local test loop: push the branch, one comprehensive review of the full
diff, then CI (format check regenerates the manifest; visual regression will
show the sidebar change on /docs pages).
