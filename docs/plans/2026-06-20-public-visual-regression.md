# Public Pages Visual Regression Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** On every PR that touches the monolith frontend, deterministically screenshot all public SvelteKit pages against committed mock data, diff them against committed baselines, and post inline before/after/diff images to the PR for only the pages that actually changed.

**Architecture:** A small, self-contained Node tool under `projects/monolith/frontend/visual/` (its own `package.json`, NOT part of the hermetic app build) drives the pipeline: (1) a mock API server serves committed JSON fixtures for every `/api/*` endpoint the public `load()` functions call; (2) the existing Bazel `:build_public` adapter-node server is booted with `API_BASE` pointed at the mock; (3) Playwright (chromium, software GL) screenshots each public route at desktop + mobile, intercepting `tiles.openfreemap.org` so maps render a committed flat basemap with real overlay data on top; (4) pixelmatch diffs against committed baselines with a signal threshold; (5) changed PNGs are uploaded as GitHub release assets and embedded inline in a single idempotent PR comment. A new `Visual regression` BuildBuddy action wires it together in a pinned Playwright container. Baselines are regenerated in that same container via an opt-in commit marker, never on a workstation.

**Tech Stack:** SvelteKit 2 / Svelte 5 (existing app), Node 20, Playwright (`@playwright/test`), pixelmatch + pngjs, Bazel (`:build_public` only), BuildBuddy Workflows, `gh` CLI (release assets + PR comments).

---

## Context the implementer must absorb first

**This is NOT a `bazel test`.** It is a standalone Node tool run by a dedicated BuildBuddy action, mirroring how `Manifest diff` (`bazel/helm/ci-diff-manifests.sh`) is a standalone script that posts a PR comment. The repo's "apko / dual-arch / uid 65532" rules apply to _shipped runtime images_ only. The CI runner `container_image` here is an external pinned Playwright image, not an apko image. Do not build an apko image for this.

**No local test loop for CI-fidelity.** The mock server, capture, and diff scripts CAN and SHOULD be smoke-run locally on macOS during development (pages render, diff logic works). But pixel baselines are only valid from the Linux Playwright container, because macOS vs Linux font/WebGL rasterization differs. Baseline PNGs are generated in CI only.

**The mock seam is `API_BASE`.** Every public `+page.server.js` `load()` calls `fetch(\`${API_BASE}/api/...\`)`. Because we run the *real* adapter-node app with `API_BASE`pointed at the mock, the app's own same-origin`+server.js`proxy routes (e.g.`/app/ships/heat`, `/app/notes/stats`) run inside the app and call the mock themselves. So the mock only serves the raw `/api/...` backend paths, never the proxy site paths.

### Route inventory (the source of truth for `targets.json`)

Screenshot targets (HTML pages). "map" = renders maplibre (`tiles.openfreemap.org`), needs basemap interception.

| Route                         | `/api/*` endpoints the page (or its on-mount client code) hits                                         | map            | notes                                                                                                 |
| ----------------------------- | ------------------------------------------------------------------------------------------------------ | -------------- | ----------------------------------------------------------------------------------------------------- |
| `/`                           | `/api/home/observability/topology`, `/api/home/observability/stats` (fail-soft to static)              | no             | 30s marquee `setInterval`, `Date.now()` "deployed ago" — freeze clock                                 |
| `/app/dr-jobs`                | `/api/dr-jobs/listings`                                                                                | no             | `Date.now()` "seen < 7d" — freeze clock                                                               |
| `/app/hikes`                  | `/api/hikes/walks`                                                                                     | yes            | 5min `Date.now()` tick                                                                                |
| `/app/ships`                  | `/api/ships/snapshot`                                                                                  | yes            | 120s `invalidateAll`                                                                                  |
| `/app/stars`                  | `/api/stars/sites`                                                                                     | yes            | 30min `invalidateAll`, 5min `Date.now()` tick                                                         |
| `/app/trips`                  | `/api/trips/trips`                                                                                     | no             | imgproxy-signed URLs (deterministic)                                                                  |
| `/app/trips/<slug>`           | `/api/trips/trip/<slug>` (layout)                                                                      | yes            | use fixture slug `demo-trip`                                                                          |
| `/app/trips/<slug>/timeline`  | inherits layout `/api/trips/trip/<slug>`                                                               | yes            |                                                                                                       |
| `/app/trips/<slug>/day/<day>` | inherits layout; own load does no fetch                                                                | yes            | use `day=1`                                                                                           |
| `/app/notes`                  | proxy `/app/notes/stats`→`/api/home/observability/stats`; on-mount graph `/api/knowledge/public/graph` | no (d3 canvas) | SSE chat is interactive — capture static initial state only                                           |
| `/chat`                       | none on SSR (returns `turnstileSiteKey`); chat is SSE                                                  | no             | capture static initial state only                                                                     |
| `/docs`                       | none (bundled `docs-manifest.json`)                                                                    | no             | fully static                                                                                          |
| `/docs/<slug>`                | none (bundled manifest)                                                                                | no             | use a known real slug, e.g. `decisions/tooling/004-ocaml-rules` if present, else first manifest entry |
| `/cv`                         | none (no load)                                                                                         | no             | free deterministic target                                                                             |
| `/engineering`                | none (no load)                                                                                         | no             | free deterministic target                                                                             |

Excluded (non-HTML, do NOT screenshot): `/llms.txt`, `/robots.txt`, `/sitemap.xml`, all `+server.js` JSON/SSE proxies (`/app/notes/graph`, `/app/notes/body/<id>`, `/app/ships/heat`, `/app/ships/track/<mmsi>`, `/app/stars/history/...`, `/app/hikes/walk/<uuid>`, `/chat/session`, `/chat/message`), `/health`, and the redirects `/notes` and `/notes/graph` (308 → `/app/notes`).

Mock `/api/*` endpoints to implement fixtures for (superset of the table): `/api/home/observability/topology`, `/api/home/observability/stats`, `/api/dr-jobs/listings`, `/api/hikes/walks`, `/api/ships/snapshot`, `/api/stars/sites`, `/api/trips/trips`, `/api/trips/trip/demo-trip`, `/api/knowledge/public/graph`. (Add `/api/knowledge/public/notes/<id>` only if a target needs it; current targets do not.)

---

## Task 1: Scaffold the `visual/` tool package

**Files:**

- Create: `projects/monolith/frontend/visual/package.json`
- Create: `projects/monolith/frontend/visual/.gitignore`
- Create: `projects/monolith/frontend/visual/README.md`
- Create: `projects/monolith/frontend/visual/targets.json`

**Step 1: Create `package.json`** (standalone, pinned — the pinned `@playwright/test` version pins the chromium revision, which is half of determinism):

```json
{
  "name": "monolith-visual-regression",
  "private": true,
  "type": "module",
  "description": "Mock-data visual regression for monolith public pages. Run in CI's pinned Playwright container; smoke-runnable locally.",
  "scripts": {
    "capture": "node capture.mjs",
    "diff": "node diff.mjs",
    "test": "node --test"
  },
  "dependencies": {
    "@playwright/test": "1.49.1",
    "pixelmatch": "6.0.0",
    "pngjs": "7.0.0",
    "sirv": "3.0.0"
  }
}
```

**Step 2: Create `.gitignore`:**

```
node_modules/
out/
```

**Step 3: Create `targets.json`** — the SSOT for what gets screenshotted. `mask` is empty because we render maps (Task 4) rather than mask them; keep the field for future per-element exclusions. `waitFor` lets a page declare a settle selector.

```json
{
  "viewports": [
    { "name": "desktop", "width": 1280, "height": 800 },
    { "name": "mobile", "width": 390, "height": 844 }
  ],
  "pages": [
    { "id": "home", "path": "/" },
    { "id": "dr-jobs", "path": "/app/dr-jobs" },
    { "id": "hikes", "path": "/app/hikes", "map": true },
    { "id": "ships", "path": "/app/ships", "map": true },
    { "id": "stars", "path": "/app/stars", "map": true },
    { "id": "trips", "path": "/app/trips" },
    { "id": "trips-overview", "path": "/app/trips/demo-trip", "map": true },
    {
      "id": "trips-timeline",
      "path": "/app/trips/demo-trip/timeline",
      "map": true
    },
    { "id": "trips-day", "path": "/app/trips/demo-trip/day/1", "map": true },
    { "id": "notes", "path": "/app/notes", "static_initial": true },
    { "id": "chat", "path": "/chat", "static_initial": true },
    { "id": "docs", "path": "/docs" },
    { "id": "docs-slug", "path": "/docs/DOCS_SLUG_PLACEHOLDER" },
    { "id": "cv", "path": "/cv" },
    { "id": "engineering", "path": "/engineering" }
  ]
}
```

**Step 4: Create `README.md`** documenting: the `API_BASE` seam, "baselines are CI-only," how to smoke-run locally (`npm install && npx playwright install chromium && API_BASE=... node capture.mjs`), and the basemap-interception rationale.

**Step 5: Resolve `DOCS_SLUG_PLACEHOLDER`** — read `projects/monolith/frontend/src/lib/public/docs/docs-manifest.json`, pick the first stable slug, and replace the placeholder in `targets.json`.

**Step 6: Commit**

```bash
git add projects/monolith/frontend/visual/
git commit -m "feat(visual): scaffold public-pages visual regression tool"
```

---

## Task 2: Mock API server

**Files:**

- Create: `projects/monolith/frontend/visual/mock-server.mjs`
- Create: `projects/monolith/frontend/visual/fixtures/api/*.json` (one per endpoint)
- Create: `projects/monolith/frontend/visual/mock-server.test.mjs`

**Step 1: Write the failing test** (`mock-server.test.mjs`) — the route matcher maps a request path to a fixture file deterministically:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { resolveFixture } from "./mock-server.mjs";

test("maps known api path to fixture", () => {
  assert.equal(
    resolveFixture("/api/stars/sites"),
    "fixtures/api/stars_sites.json",
  );
});
test("maps parameterized trip path", () => {
  assert.equal(
    resolveFixture("/api/trips/trip/demo-trip"),
    "fixtures/api/trips_trip.json",
  );
});
test("returns null for unknown path", () => {
  assert.equal(resolveFixture("/api/does/not/exist"), null);
});
```

**Step 2: Run it, expect failure**

Run: `cd projects/monolith/frontend/visual && node --test mock-server.test.mjs`
Expected: FAIL (`resolveFixture` not exported).

**Step 3: Implement `mock-server.mjs`** — a tiny `http` server plus an exported pure `resolveFixture`. It serves committed JSON, sets permissive CORS + a stable `etag`/`last-modified` (so `setHeaders` in loads behaves), and 404s unknown paths so missing fixtures are loud:

```js
import http from "node:http";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));

// Map a request pathname to a committed fixture file (pure; unit-tested).
const ROUTES = [
  ["/api/home/observability/topology", "fixtures/api/home_topology.json"],
  ["/api/home/observability/stats", "fixtures/api/home_stats.json"],
  ["/api/dr-jobs/listings", "fixtures/api/dr_jobs_listings.json"],
  ["/api/hikes/walks", "fixtures/api/hikes_walks.json"],
  ["/api/ships/snapshot", "fixtures/api/ships_snapshot.json"],
  ["/api/stars/sites", "fixtures/api/stars_sites.json"],
  ["/api/trips/trips", "fixtures/api/trips_trips.json"],
  ["/api/knowledge/public/graph", "fixtures/api/knowledge_graph.json"],
];
const PREFIX = [["/api/trips/trip/", "fixtures/api/trips_trip.json"]];

export function resolveFixture(pathname) {
  for (const [p, f] of ROUTES) if (pathname === p) return f;
  for (const [p, f] of PREFIX) if (pathname.startsWith(p)) return f;
  return null;
}

export function startMock(port) {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://localhost");
    const fixture = resolveFixture(url.pathname);
    if (!fixture) {
      res.writeHead(404, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "no fixture", path: url.pathname }));
      return;
    }
    const body = readFileSync(join(HERE, fixture));
    res.writeHead(200, {
      "content-type": "application/json",
      "access-control-allow-origin": "*",
      etag: '"mock"',
      "last-modified": "Thu, 01 Jan 1970 00:00:00 GMT",
    });
    res.end(body);
  });
  return new Promise((resolve) =>
    server.listen(port, "127.0.0.1", () => resolve(server)),
  );
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const port = Number(process.env.MOCK_PORT || 8099);
  startMock(port).then(() => console.log(`mock api on :${port}`));
}
```

**Step 4: Run the test, expect pass**

Run: `node --test mock-server.test.mjs`
Expected: PASS (3/3).

**Step 5: Author fixtures.** For each endpoint, capture the _real_ response shape and hand-trim to a small, representative, deterministic body. Get the shapes from the backend response models / routers (e.g. `projects/monolith/app/.../stars`, `ships`, `trips`, `hikes`, `dr_jobs`, `home/observability`, `knowledge`). Fixtures must:

- Use fixed dates well in the past (so "X ago" text is stable once the clock is frozen in Task 3).
- Include enough rows to exercise layout (e.g. a few trip cards, a few dr-jobs rows, a handful of stars sites, some ship positions, a small knowledge graph).
- For `trips_trip.json`, use slug `demo-trip` with at least one day and GPS track so the maps and day page have data to overlay.

**Step 6: Smoke-run locally**

Run: `MOCK_PORT=8099 node mock-server.mjs &` then `curl -s localhost:8099/api/stars/sites | head -c 200`
Expected: the trimmed JSON body.

**Step 7: Commit**

```bash
git add projects/monolith/frontend/visual/mock-server.mjs projects/monolith/frontend/visual/mock-server.test.mjs projects/monolith/frontend/visual/fixtures/
git commit -m "feat(visual): mock api server with committed fixtures"
```

---

## Task 3: Build + boot orchestration and clock freezing

**Files:**

- Create: `projects/monolith/frontend/visual/serve.mjs` (boots mock + adapter-node server, waits for ready, returns a teardown)

**Step 1: Implement `serve.mjs`.** It (a) starts the mock on `MOCK_PORT`, (b) spawns the prebuilt adapter-node server with the right env, (c) polls until the app answers. The app bundle path is the Bazel output of `:build_public` (entry `index.js`):

```js
import { spawn } from "node:child_process";
import { startMock } from "./mock-server.mjs";

const APP_ENTRY = process.env.APP_ENTRY; // e.g. bazel-bin/projects/monolith/frontend/build_public/index.js
const APP_PORT = Number(process.env.APP_PORT || 3000);
const MOCK_PORT = Number(process.env.MOCK_PORT || 8099);

async function waitFor(url, ms = 30000) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    try {
      if ((await fetch(url)).ok) return;
    } catch {}
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`timeout waiting for ${url}`);
}

export async function serve() {
  const mock = await startMock(MOCK_PORT);
  const app = spawn("node", [APP_ENTRY], {
    env: {
      ...process.env,
      PORT: String(APP_PORT),
      API_BASE: `http://127.0.0.1:${MOCK_PORT}`,
      ORIGIN: `http://127.0.0.1:${APP_PORT}`,
    },
    stdio: "inherit",
  });
  await waitFor(`http://127.0.0.1:${APP_PORT}/cv`);
  return {
    base: `http://127.0.0.1:${APP_PORT}`,
    async stop() {
      app.kill("SIGTERM");
      mock.close();
    },
  };
}
```

> Note: clock freezing is done per-page in Playwright (Task 4) via `page.clock` / `addInitScript`, not here, because it must run in the browser context.

**Step 2: Smoke-run locally** after building the app:

```bash
bazel build //projects/monolith/frontend:build_public
APP_ENTRY=$(bazel info bazel-bin)/projects/monolith/frontend/build_public/index.js \
  node -e "import('./serve.mjs').then(async m => { const s = await m.serve(); console.log('up at', s.base); await s.stop(); })"
```

Expected: `up at http://127.0.0.1:3000` then clean exit. (On macOS this validates the bundle boots against the mock; pixels are not asserted here.)

**Step 3: Commit**

```bash
git add projects/monolith/frontend/visual/serve.mjs
git commit -m "feat(visual): boot adapter-node app against mock api"
```

---

## Task 4: Basemap interception fixture + capture script

**Files:**

- Create: `projects/monolith/frontend/visual/fixtures/basemap/blank-style.json`
- Create: `projects/monolith/frontend/visual/capture.mjs`

**Step 1: Create the committed blank basemap style** (Option A from the design — a solid background layer, no vector source, so maplibre makes zero tile/glyph/sprite requests and renders a flat backdrop under our real overlays):

```json
{
  "version": 8,
  "name": "blank",
  "sources": {},
  "layers": [
    {
      "id": "bg",
      "type": "background",
      "paint": { "background-color": "#e8e6df" }
    }
  ]
}
```

**Step 2: Implement `capture.mjs`.** Launch chromium with software GL so WebGL maps are deterministic; intercept every `tiles.openfreemap.org` request and fulfill the style request with the blank style (404 any other openfreemap sub-request so nothing hangs); freeze the clock; for each target × viewport, navigate, wait for network idle + a short settle, full-page screenshot to `out/<id>-<viewport>.png`.

```js
import { chromium } from "@playwright/test";
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { serve } from "./serve.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const targets = JSON.parse(readFileSync(join(HERE, "targets.json")));
const blankStyle = readFileSync(
  join(HERE, "fixtures/basemap/blank-style.json"),
  "utf8",
);
const FROZEN = Date.UTC(2026, 0, 1, 12, 0, 0); // deterministic wall clock

const outDir = join(HERE, "out");
mkdirSync(outDir, { recursive: true });

const app = await serve();
const browser = await chromium.launch({
  args: [
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
  ],
});
try {
  for (const vp of targets.viewports) {
    const ctx = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      deviceScaleFactor: 1,
      reducedMotion: "reduce",
    });
    // Intercept the basemap: serve our flat style, drop any other tile host call.
    await ctx.route("**/tiles.openfreemap.org/**", (route) => {
      const u = route.request().url();
      if (u.includes("/styles/")) {
        return route.fulfill({
          contentType: "application/json",
          body: blankStyle,
        });
      }
      return route.fulfill({ status: 404, body: "" });
    });
    for (const page of targets.pages) {
      const p = await ctx.newPage();
      await p.clock.setFixedTime(FROZEN);
      await p.goto(`${app.base}${page.path}`, {
        waitUntil: "networkidle",
        timeout: 30000,
      });
      // Maps: wait for the canvas to be present then a settle for the GL paint.
      if (page.map)
        await p
          .waitForSelector("canvas.maplibregl-canvas", { timeout: 10000 })
          .catch(() => {});
      await p.waitForTimeout(page.map ? 1200 : 400);
      await p.screenshot({
        path: join(outDir, `${page.id}-${vp.name}.png`),
        fullPage: true,
        animations: "disabled",
      });
      await p.close();
    }
    await ctx.close();
  }
  writeFileSync(
    join(outDir, "manifest.json"),
    JSON.stringify(
      targets.pages.flatMap((pg) =>
        targets.viewports.map((vp) => `${pg.id}-${vp.name}`),
      ),
      null,
      2,
    ),
  );
} finally {
  await browser.close();
  await app.stop();
}
```

**Step 3: Smoke-run locally** (after `npm install && npx playwright install chromium`):

```bash
bazel build //projects/monolith/frontend:build_public
cd projects/monolith/frontend/visual
APP_ENTRY=$(cd ../../../.. && bazel info bazel-bin)/projects/monolith/frontend/build_public/index.js node capture.mjs
ls out/*.png
```

Expected: a PNG per page×viewport, maps showing the flat basemap with real markers/routes overlaid. (Eyeball them; do not commit as baselines — these are macOS pixels.)

**Step 4: Commit**

```bash
git add projects/monolith/frontend/visual/fixtures/basemap projects/monolith/frontend/visual/capture.mjs
git commit -m "feat(visual): playwright capture with basemap interception"
```

---

## Task 5: Diff with signal threshold

**Files:**

- Create: `projects/monolith/frontend/visual/diff.mjs`
- Create: `projects/monolith/frontend/visual/diff.test.mjs`
- Create: `projects/monolith/frontend/visual/baseline/.gitkeep`

**Step 1: Write the failing test** for the pure decision function (the "high signal, low noise" rule):

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { isChanged } from "./diff.mjs";

test("sub-threshold noise is not a change", () => {
  assert.equal(isChanged({ mismatched: 30, total: 1_000_000 }), false); // 0.003%, < 50px floor
});
test("clear change is reported", () => {
  assert.equal(isChanged({ mismatched: 5000, total: 1_000_000 }), true); // 0.5%
});
test("missing baseline counts as added/changed", () => {
  assert.equal(isChanged({ added: true }), true);
});
```

**Step 2: Run it, expect failure.**

Run: `node --test diff.test.mjs` → FAIL.

**Step 3: Implement `diff.mjs`.** Export `isChanged` (floor of 50 mismatched px AND > 0.1% of pixels), and a main that pixelmatches every `out/<name>.png` against `baseline/<name>.png`, writes `out/diff/<name>.png` for changed pages, and emits `out/report.json` (`{changed: [...], added: [...], unchanged: n}`):

```js
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { PNG } from "pngjs";
import pixelmatch from "pixelmatch";

const HERE = dirname(fileURLToPath(import.meta.url));
const FLOOR = 50,
  RATIO = 0.001;

export function isChanged({ mismatched = 0, total = 1, added = false }) {
  if (added) return true;
  return mismatched > FLOOR && mismatched / total > RATIO;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const names = JSON.parse(readFileSync(join(HERE, "out/manifest.json")));
  mkdirSync(join(HERE, "out/diff"), { recursive: true });
  const report = { changed: [], added: [], unchanged: 0 };
  for (const name of names) {
    const curPath = join(HERE, `out/${name}.png`);
    const basePath = join(HERE, `baseline/${name}.png`);
    if (!existsSync(basePath)) {
      report.added.push(name);
      continue;
    }
    const cur = PNG.sync.read(readFileSync(curPath));
    const base = PNG.sync.read(readFileSync(basePath));
    if (cur.width !== base.width || cur.height !== base.height) {
      report.changed.push(name);
      continue;
    }
    const diff = new PNG({ width: cur.width, height: cur.height });
    const mismatched = pixelmatch(
      base.data,
      cur.data,
      diff.data,
      cur.width,
      cur.height,
      { threshold: 0.1 },
    );
    if (isChanged({ mismatched, total: cur.width * cur.height })) {
      writeFileSync(join(HERE, `out/diff/${name}.png`), PNG.sync.write(diff));
      report.changed.push(name);
    } else report.unchanged++;
  }
  writeFileSync(join(HERE, "out/report.json"), JSON.stringify(report, null, 2));
  console.log(
    `changed=${report.changed.length} added=${report.added.length} unchanged=${report.unchanged}`,
  );
}
```

**Step 4: Run the test, expect pass.** `node --test diff.test.mjs` → PASS (3/3).

**Step 5: Commit**

```bash
git add projects/monolith/frontend/visual/diff.mjs projects/monolith/frontend/visual/diff.test.mjs projects/monolith/frontend/visual/baseline/.gitkeep
git commit -m "feat(visual): pixelmatch diff with signal threshold"
```

---

## Task 6: Report to PR (release-asset upload + idempotent comment)

**Files:**

- Create: `projects/monolith/frontend/visual/report-to-pr.sh`

This reuses the exact `gh` mechanics from `bazel/helm/ci-diff-manifests.sh`: `GHCR_TOKEN`→`GH_TOKEN`, `BUILDBUDDY_PULL_REQUEST_NUMBER`, a `<!-- marker -->` for find-or-update. Images are hosted as GitHub release assets on a single long-lived prerelease tag `visual-snapshots`; the repo is public so `browser_download_url`s render inline.

**Step 1: Implement `report-to-pr.sh`:**

```bash
#!/usr/bin/env bash
# report-to-pr.sh — upload changed visual PNGs as release assets and post/update
# an inline before/after/diff PR comment. Mirrors bazel/helm/ci-diff-manifests.sh.
set -euo pipefail

MARKER="<!-- public-visual-regression -->"
TAG="visual-snapshots"
REPO_ROOT="$(git rev-parse --show-toplevel)"
VDIR="$REPO_ROOT/projects/monolith/frontend/visual"
PR_NUMBER="${BUILDBUDDY_PULL_REQUEST_NUMBER:-}"
SHA="$(git rev-parse --short HEAD)"

export GH_TOKEN="${GHCR_TOKEN:-}"
GH="$(command -v gh)"

report="$VDIR/out/report.json"
changed=$(jq -r '.changed[]' "$report")
added=$(jq -r '.added[]' "$report")

if [ -z "$changed$added" ]; then
  BODY="${MARKER}
## Public page visual diff
No visual changes across the public pages. ✅"
else
  # Ensure the hosting prerelease exists (idempotent).
  "$GH" release view "$TAG" >/dev/null 2>&1 || \
    "$GH" release create "$TAG" --prerelease --title "Visual snapshots" \
      --notes "Auto-managed asset host for public-page visual regression. Do not delete."

  upload() { # name -> echoes the public asset URL
    local name="$1" file="$2" asset="pr${PR_NUMBER}-${SHA}-${name}"
    "$GH" release upload "$TAG" "${file}#${asset}.png" --clobber >/dev/null
    echo "https://github.com/$(gh repo view --json nameWithOwner -q .nameWithOwner)/releases/download/${TAG}/${asset}.png"
  }

  rows=""
  for name in $changed; do
    before=$(upload "${name}-before" "$VDIR/baseline/${name}.png")
    after=$(upload "${name}-after" "$VDIR/out/${name}.png")
    diff=$(upload "${name}-diff" "$VDIR/out/diff/${name}.png")
    rows="${rows}
<details><summary><b>${name}</b> (changed)</summary>

| before | after | diff |
|---|---|---|
| <img src=\"${before}\" width=\"260\"> | <img src=\"${after}\" width=\"260\"> | <img src=\"${diff}\" width=\"260\"> |

</details>"
  done
  for name in $added; do
    after=$(upload "${name}-after" "$VDIR/out/${name}.png")
    rows="${rows}
<details><summary><b>${name}</b> (new — no baseline)</summary>

<img src=\"${after}\" width=\"320\">

</details>"
  done

  BODY="${MARKER}
## Public page visual diff
Changed: **$(echo "$changed" | grep -c . || true)** · New: **$(echo "$added" | grep -c . || true)** · commit \`${SHA}\`

To accept these as the new baseline, push a commit whose message contains \`[update-baselines]\`.
${rows}"
fi

if [ -z "$PR_NUMBER" ] || [ -z "$GH_TOKEN" ]; then
  echo "No PR number or token; printing report:"; echo "$BODY"; exit 0
fi

EXISTING=$("$GH" api "repos/{owner}/{repo}/issues/${PR_NUMBER}/comments" --paginate \
  --jq ".[] | select(.body | startswith(\"$MARKER\")) | .id" 2>/dev/null | head -1) || true
if [ -n "$EXISTING" ]; then
  "$GH" api "repos/{owner}/{repo}/issues/comments/${EXISTING}" --method PATCH --field body="$BODY" --silent
else
  "$GH" pr comment "$PR_NUMBER" --body "$BODY"
fi
echo "Posted visual diff comment."
```

**Step 2: `chmod +x report-to-pr.sh`** and shellcheck-clean it (the repo runs `shfmt`/format on shell).

**Step 3: Verification** is end-to-end in CI (Task 8). Locally you can dry-run the no-token branch:

Run: `cd projects/monolith/frontend/visual && echo '{"changed":[],"added":[]}' > out/report.json && ./report-to-pr.sh`
Expected: prints the "No visual changes" body and exits 0.

**Step 4: Commit**

```bash
git add projects/monolith/frontend/visual/report-to-pr.sh
git commit -m "feat(visual): upload diff images as release assets and post pr comment"
```

---

## Task 7: Baseline-update path

**Files:**

- Create: `projects/monolith/frontend/visual/update-baselines.sh`

**Step 1: Implement `update-baselines.sh`** — copies `out/*.png` over `baseline/*.png` and commits+pushes to the PR branch as a bot, mirroring the ci-format-bot pattern. Triggered only when the latest commit message contains `[update-baselines]`:

```bash
#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(git rev-parse --show-toplevel)"
VDIR="$REPO_ROOT/projects/monolith/frontend/visual"

cp "$VDIR"/out/*.png "$VDIR/baseline/"
cd "$REPO_ROOT"
if git diff --quiet -- "$VDIR/baseline"; then
  echo "Baselines already current."; exit 0
fi
git config user.name "visual-baseline-bot"
git config user.email "visual-baseline-bot@users.noreply.github.com"
git add "$VDIR/baseline"
git commit -m "chore(visual): update visual baselines [skip ci]"
git push origin "HEAD:${BUILDBUDDY_GIT_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
```

> Confirm the correct PR-branch env var BuildBuddy exposes (e.g. `BUILDBUDDY_GIT_BRANCH`); fall back to `git rev-parse --abbrev-ref HEAD`. The bot push needs `GH_TOKEN`/git credentials already configured by the runner (same as ci-format-bot).

**Step 2: Commit**

```bash
git add projects/monolith/frontend/visual/update-baselines.sh
git commit -m "feat(visual): opt-in baseline update via commit marker"
```

---

## Task 8: BuildBuddy `Visual regression` action

**Files:**

- Modify: `buildbuddy.yaml` (add one action; model on `Manifest diff`)

**Step 1: Add the action.** Pinned Playwright image gives chromium + fonts + deps as one deterministic unit. Triggered on PRs only (like `Manifest diff`), non-blocking/informational.

```yaml
# Visual regression — screenshots public pages against mock data, diffs vs
# committed baselines, posts inline before/after/diff to the PR. Standalone
# Node tool (NOT bazel test). Pinned Playwright image = deterministic fonts +
# chromium; baselines are only valid from this image.
- name: "Visual regression"
  container_image: "mcr.microsoft.com/playwright:v1.49.1-noble"
  max_retries: 1
  resource_requests:
    cpu: "4"
    memory: "8GB"
    disk: "20GB"
  triggers:
    pull_request:
      branches:
        - "*"
      merge_with_base: false
  steps:
    - run: |
        set -euo pipefail
        AUTHOR="$(git log -1 --format='%an')"
        [ "$AUTHOR" = "visual-baseline-bot" ] && { echo "skip bot commit"; exit 0; }

        bazel build //projects/monolith/frontend:build_public 2>&1 | tail -2
        APP_ENTRY="$(bazel info bazel-bin)/projects/monolith/frontend/build_public/index.js"

        cd projects/monolith/frontend/visual
        npm ci || npm install
        APP_ENTRY="$APP_ENTRY" node capture.mjs
        node diff.mjs

        cd "$(git rev-parse --show-toplevel)"
        if git log -1 --format='%s%n%b' | grep -q '\[update-baselines\]'; then
          ./projects/monolith/frontend/visual/update-baselines.sh
        else
          ./projects/monolith/frontend/visual/report-to-pr.sh
        fi
```

> Verify the Playwright image can run `bazel`/`bazelisk` (the repo aliases it to `bb`). If the BuildBuddy runner does not inject the bazel wrapper into arbitrary images, either (a) install bazelisk in the step, or (b) split into two actions: a default-image step that builds `:build_public` and stashes the artifact, and the Playwright-image step that consumes it. Resolve this during implementation by reading how other non-`ubuntu-24.04` actions (e.g. is there one?) get bazel, or test with a throwaway PR.

**Step 2: Verification:** push the branch, open a draft PR, watch the action.

```bash
gh pr checks <number> --watch
```

Read failures via `mcp__buildbuddy__get_invocation` (commitSha selector) → `get_log`. Expect the action to run capture, find every page "added" (no baselines yet), and post a comment with all pages as "new."

**Step 3: Commit**

```bash
git add buildbuddy.yaml
git commit -m "ci(visual): add public-pages visual regression action"
```

---

## Task 9: Bootstrap the baselines

**Step 1:** On the open PR, push a commit with `[update-baselines]` in the message (e.g. an empty commit):

```bash
git commit --allow-empty -m "chore(visual): seed baselines [update-baselines]"
git push
```

**Step 2:** The action runs in the Playwright container, captures all pages, and `visual-baseline-bot` commits `baseline/*.png` to the PR branch. Pull and inspect them:

```bash
git pull
ls projects/monolith/frontend/visual/baseline/
```

Expected: one PNG per page×viewport (~30 files), Linux-rendered.

**Step 3:** Push a trivial, visible change to one public page (e.g. tweak a heading) on a follow-up commit (no marker) and confirm the action posts an inline before/after/diff for _only_ that page, and reports the rest unchanged. This is the real acceptance test of the whole loop.

**Step 4:** Revert the trivial change. Final state: baselines committed, loop proven.

---

## Task 10: Docs

**Files:**

- Modify: `projects/monolith/CLAUDE.md` (add a short "Visual regression" subsection: what it is, the `API_BASE` mock seam, how to add a new public page to `targets.json` + a fixture, and the `[update-baselines]` workflow)
- Consider: a one-line pointer in root `CLAUDE.md` Key Patterns if warranted.

**Commit**

```bash
git add projects/monolith/CLAUDE.md
git commit -m "docs(visual): document the public-pages visual regression loop"
```

---

## Open items resolved by this plan

- **Container base:** pinned `mcr.microsoft.com/playwright:v1.49.1-noble` (not apko — this is a CI runner, not a shipped image). Determinism comes from pinning this image + the `@playwright/test` version together.
- **Release tag / pruning:** single long-lived `visual-snapshots` prerelease; assets keyed `pr<N>-<sha>-<name>`, `--clobber` so a PR's latest run overwrites its own assets. Assets are tens of KB; accept slow accumulation, prune manually/periodically if ever needed (a follow-up cron could delete assets for closed PRs).
- **"Which pages changed":** `isChanged` in `diff.mjs` (floor 50 px AND > 0.1% of pixels); only changed/added pages are uploaded and embedded.
- **Maps:** intercepted, not masked — committed flat `blank-style.json` served for the basemap; real overlay layers render on top; software-GL chromium keeps WebGL deterministic.

## Risks / things to watch

- **bazel-in-Playwright-image** (Task 8 Step 1 note) is the likeliest snag; have the two-action fallback ready.
- **Fixture drift:** if a backend response shape changes, a fixture can go stale and a page renders an error state. That is actually _desirable signal_ (the diff shows it), but document that fixtures are hand-maintained, not generated.
- **`networkidle` + SSE pages** (`/app/notes`, `/chat`): an open SSE stream can prevent `networkidle`. For `static_initial` pages, prefer `waitUntil: "domcontentloaded"` + a fixed settle, or block the SSE endpoints via `ctx.route` so they never hold the connection. Decide per-page during Task 4 hardening.
- **`deviceScaleFactor: 1`** keeps PNGs small and stable; do not change it without regenerating all baselines.

```

```
