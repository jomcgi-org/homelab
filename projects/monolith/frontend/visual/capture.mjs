// Playwright capture for the monolith public pages.
//
// MUST run in CI's pinned Linux Playwright container. It cannot be fully
// exercised on a workstation: it boots the Bazel `:build_public` adapter-node
// app (see serve.mjs / APP_ENTRY), and the resulting macOS pixels are NOT a
// valid baseline (font hinting + SwiftShader rasterization differ from Linux).
//
// Determinism seams:
//   - `**/tiles.openfreemap.org/**` is intercepted: the style request is
//     fulfilled with a committed flat background style and every other tile/
//     glyph/sprite sub-request is 404'd, so maplibre paints a flat backdrop
//     under our real overlay data and makes zero non-deterministic calls.
//   - `**/img/**` (same-origin imgproxy) is fulfilled with a committed
//     placeholder PNG. Trip pages render imgproxy-signed URLs that resolve to
//     `/img/unsafe/...` (or `/img/<sig>/...`) when IMGPROXY keys are unset at
//     mock boot; the adapter-node app does not serve `/img/**`, so without this
//     those requests 404 and add noise / can stall networkidle.
//   - Chat SSE endpoints (`/chat/message`, `/chat/session`) are aborted so the
//     open stream never prevents `waitUntil: "networkidle"` from resolving;
//     pages flagged `static_initial` are additionally loaded with
//     `domcontentloaded` + a fixed settle so we capture their static initial
//     state rather than hanging on a live stream.
// @playwright/test is imported DYNAMICALLY below, after PLAYWRIGHT_BROWSERS_PATH
// is absolutized: Playwright reads that env when its module is first evaluated,
// and a static import would be hoisted above the env fix.
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import { serve } from "./serve.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const targets = JSON.parse(readFileSync(join(HERE, "targets.json")));
const blankStyle = readFileSync(
  join(HERE, "fixtures/basemap/blank-style.json"),
  "utf8",
);
const placeholderPng = readFileSync(
  join(HERE, "fixtures/basemap/placeholder.png"),
);
const FROZEN = Date.UTC(2026, 0, 1, 12, 0, 0); // deterministic wall clock
// The public tier lives under /public in the route tree; the apex reroute that
// strips that prefix is hostname-gated (jomcgi.dev) and does not fire on
// 127.0.0.1, so we navigate the real /public/* routes directly. The render is
// identical to production (only the URL bar differs, which screenshots ignore).
const ROUTE_PREFIX = process.env.ROUTE_PREFIX ?? "/public";

// Under Bazel (js_run_binary) the tool's own dir is read-only runfiles, so write
// captures to the declared out_dir. The action runs with cwd under
// <execroot>/bazel-out/...; BAZEL_BINDIR + CAPTURE_OUT_SUBDIR are execroot-
// relative, so resolve them to an absolute path via the execroot (cwd cut at
// /bazel-out/). Fall back to HERE/out for a non-Bazel local smoke run.
const outDir = process.env.BAZEL_BINDIR
  ? join(
      process.cwd().split("/bazel-out/")[0],
      process.env.BAZEL_BINDIR,
      process.env.CAPTURE_OUT_SUBDIR ?? "projects/monolith/frontend/visual/out",
    )
  : join(HERE, "out");
mkdirSync(outDir, { recursive: true });

// The BUILD sets PLAYWRIGHT_BROWSERS_PATH to an execroot-relative $(execpath)
// (this js_run_binary runs from the execroot, so a runfiles-relative $(rootpath)
// would not resolve). cwd is under <execroot>/bazel-out/..., so make it absolute
// via the execroot before Playwright reads it at launch.
if (
  process.env.PLAYWRIGHT_BROWSERS_PATH &&
  process.cwd().includes("/bazel-out/")
) {
  process.env.PLAYWRIGHT_BROWSERS_PATH = resolve(
    process.cwd().split("/bazel-out/")[0],
    process.env.PLAYWRIGHT_BROWSERS_PATH,
  );
}

// Dynamic import AFTER the env fix so Playwright's registry picks up the
// absolutized PLAYWRIGHT_BROWSERS_PATH.
const { chromium } = await import("@playwright/test");

const app = await serve();
const browser = await chromium.launch({
  args: [
    // swiftshader gives deterministic software WebGL for the map pages...
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    // ...and these let chromium run inside the apko exec image on RBE (no user
    // namespaces for the sandbox; small /dev/shm). NOT --disable-gpu: that would
    // disable the swiftshader GL path the maps need.
    "--no-sandbox",
    "--disable-dev-shm-usage",
  ],
});
try {
  for (const vp of targets.viewports) {
    const ctx = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      deviceScaleFactor: 1,
      reducedMotion: "reduce",
    });
    // Basemap: serve our flat style, drop any other tile-host sub-request.
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
    // Same-origin imgproxy: serve a deterministic placeholder so trip
    // thumbnails render instead of 404ing (the app does not serve /img/**).
    await ctx.route("**/img/**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "image/png",
        body: placeholderPng,
      }),
    );
    // External flag CDN (wc2026 group table): serve the same placeholder so the
    // flags render deterministically instead of hitting the network (which the
    // CI container cannot reach, which would stall networkidle).
    await ctx.route("**/flagcdn.com/**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "image/png",
        body: placeholderPng,
      }),
    );
    // Chat SSE: abort so the open stream never blocks networkidle.
    await ctx.route("**/chat/message", (route) => route.abort());
    await ctx.route("**/chat/session", (route) => route.abort());

    for (const page of targets.pages) {
      const p = await ctx.newPage();
      await p.clock.setFixedTime(FROZEN);
      // static_initial pages keep open SSE streams; domcontentloaded + a fixed
      // settle captures their initial state without waiting on the stream.
      const waitUntil = page.static_initial
        ? "domcontentloaded"
        : "networkidle";
      // "/" maps to the public index at /public (no trailing slash); other
      // paths get the prefix prepended.
      const rel = page.path === "/" ? "" : page.path;
      await p.goto(`${app.base}${ROUTE_PREFIX}${rel}`, {
        waitUntil,
        timeout: 30000,
      });
      if (page.map) {
        await p
          .waitForSelector("canvas.maplibregl-canvas", { timeout: 10000 })
          .catch(() => {});
      }
      // Maps need a longer settle for the GL paint; static_initial pages need a
      // settle for their initial client render after domcontentloaded.
      await p.waitForTimeout(page.map || page.static_initial ? 1200 : 400);
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
