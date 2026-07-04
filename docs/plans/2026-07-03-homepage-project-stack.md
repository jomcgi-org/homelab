# Homepage Project Stack Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task in the current session. Per repo CLAUDE.md: NO local test execution (no vitest, no bazel test); implementers self-review, all test execution is deferred to end-of-plan CI on the pushed branch. One comprehensive code review at the end of the PR, not per task.

**Goal:** Replace the front-page SLO topology diagram with a static, curated "project stack": apps as story cards on top of platform/compute/metal strata.

**Architecture:** One hand-curated config module drives a pure-CSS-grid Svelte component. No SVG, no dagre, no observability API calls from this section. Card selection mirrors to `?project=<id>` (same pattern as the old `?node=`). Design doc: `docs/plans/2026-07-03-homepage-project-stack-design.md`.

**Tech Stack:** SvelteKit (Svelte 5 runes + `$app/stores`), existing public design system (`src/lib/public/styles/design-system.css`), vitest for the config validation test, visual regression via `frontend/visual/`.

**Worktree:** `/tmp/claude-worktrees/homepage-project-stack`, branch `feat/homepage-project-stack`. All paths below are relative to `projects/monolith/frontend/` unless they start with `projects/` or `docs/`.

**Repo rules that apply here:**

- Never use em-dashes in any copy, comments, or commit messages.
- Conventional Commits, enforced by hook. The format hook may modify generated manifests (docs-manifest.json etc.); if a commit fails with "files were modified by this hook", `git add -A` and commit again.
- Chart bump is manual: `projects/monolith/chart/Chart.yaml` version AND `projects/monolith/deploy/application.yaml` `targetRevision` in the same commit.

---

### Task 1: Curated stack config + validation test

**Files:**

- Create: `src/lib/public/homepage-stack.js`
- Create: `src/lib/public/homepage-stack.test.js`

**Step 1: Write the config module**

Create `src/lib/public/homepage-stack.js` with exactly this content (copy verbatim; the copy has been curated):

```js
// Hand-curated content for the front-page project stack. This is the single
// source of truth for what the homepage says we run. Add new apps here.
// kind: "projects" items render as story cards; kind: "strip" items render
// as plain labeled chips. Story cards are only for things built in this repo,
// not things merely run here.

const GH = "https://github.com/jomcgi/homelab/tree/main";

export const stack = [
  {
    id: "apps",
    label: "APPS",
    kind: "projects",
    items: [
      {
        id: "ships",
        name: "SHIPS",
        blurb: "Live AIS map of ship traffic off the BC coast.",
        engineering:
          "CDN-cached SSR snapshots over daily-partitioned Postgres, with a GPU heatmap of every voyage ever seen.",
        tags: ["SvelteKit", "Postgres", "MapLibre"],
        links: { live: "/app/ships", readme: `${GH}/projects/monolith/ships` },
      },
      {
        id: "stars",
        name: "STARS",
        blurb: "Stargazing forecast for anywhere in western Canada.",
        engineering:
          "Clear-dark-hours scoring on a 14,000-point grid, blended with CERRA cloud climatology and edge-cached for a year.",
        tags: ["SvelteKit", "Python", "ERA5/CERRA"],
        links: { live: "/app/stars", readme: `${GH}/projects/monolith/stars` },
      },
      {
        id: "hikes",
        name: "HIKES",
        blurb: "Trail catalog with conditions and weather.",
        engineering:
          "Seeded out-of-band to keep bulk data away from the GitOps migration path.",
        tags: ["SvelteKit", "Postgres"],
        links: { live: "/app/hikes", readme: `${GH}/projects/monolith/hikes` },
      },
      {
        id: "campsites",
        name: "CAMPSITES",
        blurb: "BC Parks campsite availability crossed with weather.",
        engineering:
          "Availability polling joined against forecast windows so a free weekend actually means a good weekend.",
        tags: ["SvelteKit", "Postgres"],
        links: {
          live: "/app/campsites",
          readme: `${GH}/projects/monolith/campsites`,
        },
      },
      {
        id: "trips",
        name: "TRIPS",
        blurb: "Shared trip planning and itineraries.",
        engineering:
          "Folded from a standalone service into the monolith to cut operational surface.",
        tags: ["SvelteKit", "Postgres"],
        links: { live: "/app/trips", readme: `${GH}/projects/monolith/trips` },
      },
      {
        id: "dr-jobs",
        name: "DR JOBS",
        blurb: "NHS Scotland anaesthetics vacancy aggregator.",
        engineering:
          "Scrapes and normalises health-board job feeds into one searchable view.",
        tags: ["Python", "Postgres"],
        links: {
          live: "/app/dr-jobs",
          readme: `${GH}/projects/monolith/dr_jobs`,
        },
      },
      {
        id: "wc2026",
        name: "WC 2026",
        blurb: "Scotland's World Cup 2026 odds, updated as results land.",
        engineering:
          "Elo-driven Monte Carlo simulation of the full tournament, re-run on every fixture.",
        tags: ["Python", "Monte Carlo"],
        links: {
          live: "/app/wc2026",
          readme: `${GH}/projects/monolith/worldcup`,
        },
      },
      {
        id: "chat",
        name: "CHAT",
        blurb: "Self-hosted LLM chat running on a local GPU.",
        engineering:
          "vLLM serving a Qwen MoE model, with tool use wired to the same MCP surface the agents use.",
        tags: ["vLLM", "Qwen", "MCP"],
        links: { live: "/chat", readme: `${GH}/projects/monolith/chat` },
      },
      {
        id: "knowledge",
        name: "KNOWLEDGE",
        blurb: "A fileless knowledge graph that gardens itself.",
        engineering:
          "Raw captures decomposed into atomic notes by scheduled agents; bodies in Postgres, embeddings for RAG, no files anywhere.",
        tags: ["Postgres", "Embeddings", "Agents"],
        links: {
          live: "/app/notes",
          readme: `${GH}/projects/monolith/knowledge`,
        },
      },
      {
        id: "agents",
        name: "AGENTS",
        blurb: "An agent platform with hardware-isolated sandboxes.",
        engineering:
          "Firecracker microVMs per session, vsock-only egress with token swapping, recipes compiled at runtime.",
        tags: ["Firecracker", "Go", "MCP"],
        links: { readme: `${GH}/projects/agent_platform` },
      },
      {
        id: "grimoire",
        name: "GRIMOIRE",
        blurb: "A Postgres-first D&D campaign manager.",
        engineering:
          "Long-form book content chunked and sequenced in Postgres with an omnibox over the lot.",
        tags: ["SvelteKit", "Postgres"],
        links: { readme: `${GH}/projects/monolith/grimoire` },
      },
      {
        id: "docs",
        name: "DOCS",
        blurb: "This repo's design docs and ADRs, published.",
        engineering:
          "Markdown indexed at build time and served by the same monolith that everything else runs on.",
        tags: ["SvelteKit", "Markdown"],
        links: { live: "/docs", readme: `${GH}/docs` },
      },
    ],
  },
  {
    id: "platform",
    label: "PLATFORM",
    kind: "strip",
    items: [
      { name: "ArgoCD", href: "https://github.com/argoproj/argo-cd" },
      { name: "Linkerd", href: "https://linkerd.io" },
      { name: "SigNoz", href: "https://signoz.io" },
      { name: "Envoy Gateway", href: "https://gateway.envoyproxy.io" },
      {
        name: "1Password Operator",
        href: "https://github.com/1Password/onepassword-operator",
      },
      { name: "Atlas", href: "https://atlasgo.io" },
    ],
  },
  {
    id: "compute",
    label: "COMPUTE",
    kind: "strip",
    items: [
      { name: "Kubernetes", href: "https://kubernetes.io" },
      { name: "Firecracker", href: "https://firecracker-microvm.github.io" },
      { name: "Longhorn", href: "https://longhorn.io" },
      { name: "SeaweedFS", href: "https://github.com/seaweedfs/seaweedfs" },
      { name: "vLLM on GPU", href: "https://github.com/vllm-project/vllm" },
    ],
  },
  {
    id: "metal",
    label: "METAL",
    kind: "strip",
    items: [
      { name: "4 nodes" },
      { name: "1 GPU" },
      { name: "Cloudflare edge", href: "https://www.cloudflare.com" },
    ],
  },
];
```

**Step 2: Write the validation test**

Create `src/lib/public/homepage-stack.test.js`. This guards future config edits (typo'd ids, broken readme links), which is the only failure mode this static data has:

```js
import { describe, it, expect } from "vitest";
import { stack } from "./homepage-stack.js";

const projects = stack
  .filter((l) => l.kind === "projects")
  .flatMap((l) => l.items);

describe("homepage stack config", () => {
  it("has the four strata in order", () => {
    expect(stack.map((l) => l.id)).toEqual([
      "apps",
      "platform",
      "compute",
      "metal",
    ]);
  });

  it("has unique project ids", () => {
    const ids = projects.map((p) => p.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("every project has the required story fields", () => {
    for (const p of projects) {
      expect(p.name, p.id).toBeTruthy();
      expect(p.blurb, p.id).toBeTruthy();
      expect(p.engineering, p.id).toBeTruthy();
      expect(p.tags?.length, p.id).toBeGreaterThan(0);
      expect(p.links?.readme, p.id).toMatch(
        /^https:\/\/github\.com\/jomcgi\/homelab\/tree\/main\//,
      );
    }
  });

  it("live links are same-origin paths", () => {
    for (const p of projects) {
      if (p.links.live) expect(p.links.live, p.id).toMatch(/^\//);
    }
  });

  it("strip items have names", () => {
    for (const layer of stack.filter((l) => l.kind === "strip")) {
      for (const item of layer.items) expect(item.name, layer.id).toBeTruthy();
    }
  });
});
```

Do NOT run vitest locally (repo rule). The Bazel `test_lib` glob in `projects/monolith/frontend/BUILD` picks up `src/**/*.test.js` automatically; no BUILD edit needed.

**Step 3: Commit**

```bash
git add projects/monolith/frontend/src/lib/public/homepage-stack.js \
        projects/monolith/frontend/src/lib/public/homepage-stack.test.js
git commit -m "feat(frontend): add curated homepage project stack config"
```

---

### Task 2: StackProjectCard component

**Files:**

- Create: `src/routes/public/StackProjectCard.svelte`

**Step 1: Write the component**

Study `src/routes/public/HomepageNodeDetail.svelte` and `src/lib/public/styles/design-system.css` first to match idiom (Svelte 5 runes, token usage). Then create:

```svelte
<script>
  let { project, expanded = false, onselect } = $props();
</script>

<article class="card" class:expanded>
  <button class="card-face" onclick={onselect} aria-expanded={expanded}>
    <h3>{project.name}</h3>
    <p class="blurb">{project.blurb}</p>
    <ul class="tags">
      {#each project.tags as tag}
        <li>{tag}</li>
      {/each}
    </ul>
  </button>
  {#if expanded}
    <div class="story">
      <p>{project.engineering}</p>
      <div class="actions">
        {#if project.links.live}
          <a class="btn btn-live" href={project.links.live}>Visit live &nearr;</a>
        {/if}
        <a class="btn" href={project.links.readme} target="_blank" rel="noopener">
          Read the code &nearr;
        </a>
      </div>
    </div>
  {/if}
</article>

<style>
  .card {
    background: var(--paper);
    border: 2px solid var(--ink);
    box-shadow: var(--shadow-hard-sm);
    transition: transform 0.12s ease, box-shadow 0.12s ease;
  }
  .card:hover {
    transform: translate(-2px, -2px);
    box-shadow: var(--shadow-hard);
  }
  .card.expanded {
    background: var(--accent);
    box-shadow: var(--shadow-hard);
  }
  .card-face {
    display: block;
    width: 100%;
    padding: 14px 16px;
    text-align: left;
    background: none;
    border: none;
    cursor: pointer;
    font: inherit;
    color: var(--ink);
  }
  h3 {
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.08em;
    margin: 0 0 6px;
  }
  .blurb {
    font-size: 13px;
    line-height: 1.4;
    margin: 0 0 10px;
    color: var(--ink-2);
  }
  .card.expanded .blurb {
    color: var(--ink);
  }
  .tags {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .tags li {
    font-family: var(--mono);
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    padding: 2px 6px;
    border: 1px solid var(--rule);
  }
  .story {
    padding: 0 16px 14px;
  }
  .story p {
    font-size: 13px;
    line-height: 1.5;
    margin: 0 0 12px;
  }
  .actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .btn {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    text-decoration: none;
    color: var(--ink);
    background: var(--paper);
    border: 2px solid var(--ink);
    padding: 6px 10px;
    box-shadow: var(--shadow-hard-sm);
  }
  .btn:hover {
    transform: translate(-1px, -1px);
    box-shadow: var(--shadow-hard);
  }
  .btn-live {
    background: var(--ink);
    color: var(--paper);
  }
</style>
```

Adjust class names, exact sizes, and any token names to match what actually exists in `design-system.css` (verify `--ink-2`, `--rule`, `--shadow-hard-sm` exist; the fact-finding pass confirmed them, but re-check before relying on more). Keep the flat-ink language: 2px borders, hard shadows, no border-radius beyond `--radius` if the design system applies it globally.

**Step 2: Self-review, then commit**

Check: button-in-article a11y (`aria-expanded`), no em-dashes in copy, tokens all defined.

```bash
git add projects/monolith/frontend/src/routes/public/StackProjectCard.svelte
git commit -m "feat(frontend): add homepage stack project story card"
```

---

### Task 3: HomepageStack component

**Files:**

- Create: `src/routes/public/HomepageStack.svelte`
- Reference (read first): `src/routes/public/HomepageTopology.svelte` lines 18-35 (URL-state pattern to replicate)

**Step 1: Write the component**

```svelte
<script>
  import { page } from "$app/stores";
  import { goto } from "$app/navigation";
  import { stack } from "$lib/public/homepage-stack.js";
  import StackProjectCard from "./StackProjectCard.svelte";

  // Selection is mirrored to the URL (?project=<id>) so cards are
  // deep-linkable and the browser back button pops selection naturally.
  const selected = $derived($page.url.searchParams.get("project"));

  function setSelected(id) {
    const url = new URL($page.url);
    if (id) url.searchParams.set("project", id);
    else url.searchParams.delete("project");
    goto(url, { keepFocus: true, noScroll: true, replaceState: false });
  }

  function handleKeydown(e) {
    if (e.key === "Escape" && selected) {
      e.preventDefault();
      setSelected(null);
    }
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<section class="stack" aria-label="What this homelab runs">
  <p class="kicker">The stack, top to bottom. Click a project.</p>
  <h2>WHAT RUNS HERE</h2>

  <div class="strata">
    {#each stack as layer, i}
      <div class="layer" class:alt={i % 2 === 1} data-kind={layer.kind}>
        <span class="layer-label">{layer.label}</span>
        {#if layer.kind === "projects"}
          <div class="cards">
            {#each layer.items as project (project.id)}
              <StackProjectCard
                {project}
                expanded={selected === project.id}
                onselect={() => setSelected(selected === project.id ? null : project.id)}
              />
            {/each}
          </div>
        {:else}
          <ul class="strip">
            {#each layer.items as item}
              <li>
                {#if item.href}
                  <a href={item.href} target="_blank" rel="noopener">{item.name}</a>
                {:else}
                  <span>{item.name}</span>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    {/each}
  </div>
</section>

<style>
  .stack {
    max-width: 1360px;
    margin: 0 auto;
    padding: 48px 32px;
  }
  .kicker {
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin: 0 0 8px;
  }
  h2 {
    font-family: var(--mono);
    font-size: 28px;
    letter-spacing: 0.04em;
    margin: 0 0 24px;
  }
  .strata {
    border: 2px solid var(--ink);
    box-shadow: var(--shadow-hard);
    background: var(--paper);
  }
  .layer {
    position: relative;
    padding: 28px 24px 20px;
    border-top: 2px dashed var(--rule-2);
  }
  .layer:first-child {
    border-top: none;
  }
  .layer.alt {
    background: var(--cream);
  }
  .layer-label {
    position: absolute;
    top: -1px;
    left: 16px;
    transform: translateY(-50%);
    font-family: var(--mono);
    font-size: 8px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    background: var(--paper);
    border: 1px dashed var(--rule-2);
    padding: 2px 8px;
  }
  .layer:first-child .layer-label {
    top: 0;
    transform: none;
    border: none;
    padding-left: 0;
    position: static;
    display: block;
    margin-bottom: 12px;
  }
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 16px;
    align-items: start;
  }
  .strip {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 12px;
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .strip li a,
  .strip li span {
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ink-2);
    text-decoration: none;
    border: 1px solid var(--rule);
    background: var(--paper);
    padding: 4px 10px;
    display: inline-block;
  }
  .strip li a:hover {
    color: var(--ink);
    border-color: var(--ink);
  }
</style>
```

Match the section header treatment (kicker + h2) to how other homepage sections do it in `+page.svelte`; reuse their exact classes if shared classes exist rather than redefining.

**Step 2: Self-review, then commit**

```bash
git add projects/monolith/frontend/src/routes/public/HomepageStack.svelte
git commit -m "feat(frontend): add stratified homepage stack section"
```

---

### Task 4: Wire in, unwire topology, delete dead code

**Files:**

- Modify: `src/routes/public/+page.svelte` (import at line 4, usage at lines 178-179)
- Modify: the public page load file that supplies `data.topology` (find it: `grep -rn "topology" projects/monolith/frontend/src/routes/public/+page*.js projects/monolith/frontend/src/routes/public/+page*.server.js`)
- Delete: `src/routes/public/HomepageTopology.svelte`, `src/routes/public/HomepageNodeDetail.svelte`
- Delete (conditional): `src/lib/public/components/dag/` (DagRenderer.svelte, dag-layout.js, index.js)

**Step 1: Swap the section in `+page.svelte`**

Replace:

```svelte
import HomepageTopology from "./HomepageTopology.svelte";
```

with:

```svelte
import HomepageStack from "./HomepageStack.svelte";
```

and replace the section markup (around lines 178-179):

```svelte
<!-- ═══ SLO Topology (blue) ═══ -->
<HomepageTopology topology={data.topology} />
```

with:

```svelte
<!-- ═══ Project stack ═══ -->
<HomepageStack />
```

**Step 2: Remove the topology fetch from the page load**

In the load file found by the grep above, remove the `/api/home/observability/topology` fetch and the `topology` key from the returned data. LEAVE the stats fetch (the marquee still uses it). If topology and stats are fetched in a `Promise.all`, unwind carefully.

**Step 3: Delete dead components, verify importers first**

```bash
grep -rn "HomepageTopology\|HomepageNodeDetail" projects/monolith/frontend/src/
grep -rn "components/dag" projects/monolith/frontend/src/
```

Expected: no remaining importers besides the files being deleted (HomepageTopology imports HomepageNodeDetail and dag; nothing else does, per the fact-finding pass, but verify). Then:

```bash
git rm projects/monolith/frontend/src/routes/public/HomepageTopology.svelte \
       projects/monolith/frontend/src/routes/public/HomepageNodeDetail.svelte
git rm -r projects/monolith/frontend/src/lib/public/components/dag/
```

**Do NOT remove `@dagrejs/dagre` from package.json or BUILD.** It is still used by `src/routes/private/chat/graph-layout.js`.

**Step 4: Self-review the diff, run `format`, commit**

```bash
cd /tmp/claude-worktrees/homepage-project-stack && format
git add -A
git commit -m "feat(frontend): replace homepage SLO topology with project stack"
```

---

### Task 5: Visual regression scenarios and fixtures

**Files:**

- Modify: `projects/monolith/frontend/visual/targets.json` (pages array, `home` entry near line 8)
- Modify: `projects/monolith/frontend/visual/mock-server.mjs` (topology fixture route, line 10)
- Delete (conditional): `projects/monolith/frontend/visual/fixtures/api/home_topology.json`

**Step 1: Add the expanded-card scenario**

In `targets.json`, after the `"home"` entry, add:

```json
{ "id": "home-project", "path": "/?project=ships" }
```

(Match the exact shape of neighboring entries; some entries carry extra flags like maps/SSE. This one needs none.)

**Step 2: Remove the topology mock**

The homepage no longer fetches `/api/home/observability/topology`. Remove that route from `mock-server.mjs` (line 10) and delete `fixtures/api/home_topology.json`, after confirming no other scenario or fixture references it:

```bash
grep -rn "home_topology\|observability/topology" projects/monolith/frontend/visual/
```

Keep the stats fixture and route: the marquee still fetches `/api/home/observability/stats`.

**Step 3: Commit**

```bash
git add -A
git commit -m "test(frontend): visual scenarios for homepage project stack"
```

Note: the first CI run will fail visual regression by design (the `home` screenshots change and `home-project` has no baseline). Follow whatever baseline-update mechanism the visual suite uses; read `projects/monolith/frontend/visual/README.md` or the capture script header to find it (there is an accepted-baselines flow; do not hand-edit PNGs).

---

### Task 6: Chart bump

**Files:**

- Modify: `projects/monolith/chart/Chart.yaml` (version)
- Modify: `projects/monolith/deploy/application.yaml` (targetRevision)

**Step 1: Bump both files in lockstep**

Read the current `version:` in `projects/monolith/chart/Chart.yaml` and bump the minor (e.g. 0.271.0 to 0.272.0; use whatever is current at implementation time). Set `targetRevision` in `projects/monolith/deploy/application.yaml` to the identical new version. Both files in the same commit; a mismatch means ArgoCD keeps serving the old chart.

**Step 2: Commit**

```bash
git add projects/monolith/chart/Chart.yaml projects/monolith/deploy/application.yaml
git commit -m "chore(monolith): bump chart to roll out homepage project stack"
```

---

### Task 7: Push, PR, CI, review, merge

**Step 1: Push and open the PR**

```bash
git push -u origin feat/homepage-project-stack
gh pr create --title "feat(frontend): replace homepage SLO topology with curated project stack" \
  --body "$(cat <<'EOF'
Replaces the front-page SLO topology diagram with a static, curated project stack: apps as story cards (blurb, engineering note, tags, live + readme links, deep-linkable via ?project=<id>) over platform/compute/metal strata. No dagre, no SVG layout, no observability API calls from this section.

Design: docs/plans/2026-07-03-homepage-project-stack-design.md

Backend topology endpoint, rollup, and snapshot table are untouched; decommissioning them (if the homepage was the sole consumer) is a follow-up.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

**Step 2: Comprehensive end-of-PR code review**

Dispatch one Opus code-review of the full PR diff (repo cadence: one review per PR, not per task). Reviewer focus: design-system fidelity, a11y of the card button, the load-function unwind in Task 4 Step 2 (easy to break the stats fetch), no em-dashes anywhere in copy, chart/targetRevision lockstep.

**Step 3: Watch CI, iterate**

```bash
gh pr checks <number> --watch
```

On failure, read logs via `mcp__buildbuddy__get_invocation` (commitSha selector) then `get_target`/`get_log`. Quote the actual error before hypothesizing. Expect one visual-baseline iteration (Task 5 note).

**Step 4: Merge (rebase) once green**

```bash
gh pr merge <number> --auto --rebase
```

Then poll `gh pr view <number> --json state,mergeStateStatus` until merged, and verify rollout: `kubectl get applications -n argocd` for the monolith app sync, then load jomcgi.dev and eyeball the new section (and `?project=ships` deep link).
