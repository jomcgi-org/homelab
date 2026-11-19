import assert from "node:assert/strict";
import { test } from "node:test";
import {
  checkPage,
  isEdgeBeacon,
  isEdgeBeaconConsole,
  isEdgeBeaconCsp,
  launchBrowser,
  tier2,
} from "./browser.mjs";
import { css, html, isolateBrowserCache, respond, serve } from "./fixture.mjs";

test(
  "tier 2 fails a rendered stylesheet colour-contrast violation",
  { timeout: 120_000 },
  async (t) => {
    isolateBrowserCache(t);
    let stylesheet = css;
    const server = await serve((req, res) => {
      if (req.url === "/assets/style.css") {
        respond(res, stylesheet, "text/css");
      } else if (req.url === "/assets/app.js") {
        respond(res, "", "text/javascript");
      } else {
        respond(res, html);
      }
    });
    try {
      const url = `${server.origin}/`;
      // This runs the same tier-2 entry point as site-check: both colour
      // modes and viewport sizes must pass before changing the stylesheet.
      assert.deepEqual(await tier2([{ url, path: "/" }]), Array(4).fill(url));
      stylesheet += " p { color: lightgray; }";
      await assert.rejects(tier2([{ url, path: "/" }]), (error) => {
        // Require the contrast rule itself, with no incidental console,
        // HTTP or CSP failures that could make this a false positive.
        assert.equal(error.actual?.length, 1);
        assert.match(error.actual[0], /^axe color-contrast: p:/);
        console.log(`Expected tier-2 rejection: ${error.actual[0]}`);
        return true;
      });
    } finally {
      await server.close();
    }
  },
);

test(
  "real browser catches rendered CSS contrast, console errors and enforced/report-only CSP",
  { timeout: 120_000 },
  async (t) => {
    isolateBrowserCache(t);
    const browser = await launchBrowser();
    try {
      for (const mode of [
        "pass",
        "contrast",
        "console",
        "exception",
        "csp",
        "report-only",
        "404",
        "missing-factory",
        "off-origin",
        "edge-beacon",
        "shipped-beacon",
        "third-party-script",
        "beacon-in-query",
        "beacon-console",
        "beacon-console-prefix",
        "beacon-console-exact",
      ]) {
        await t.test(mode, async () => {
          const server = await serve((req, res) => {
            if (req.url === "/data/factory/manifest.json") {
              respond(res, "{}", "application/json", {}, 404);
            } else if (req.url === "/assets/style.css") {
              respond(res, css, "text/css");
            } else if (req.url === "/assets/app.js") {
              const script =
                {
                  // A colour keyword in JS is invisible to static CSS scans.
                  contrast:
                    'document.querySelector("p").style.color = "lightgray";',
                  console: 'console.error("fixture console error")',
                  exception: 'throw new Error("fixture exception")',
                  "missing-factory": 'fetch("/data/factory/manifest.json")',
                  "off-origin": 'fetch("https://outside.invalid/forbidden")',
                  // The beacon URL inside another URL or an app message must
                  // not hide a real failure.
                  "beacon-in-query":
                    'fetch("https://outside.invalid/preview?url=https://static.cloudflareinsights.com/beacon.min.js")',
                  "beacon-console":
                    'console.error("app failed near https://static.cloudflareinsights.com/beacon.min.js/x")',
                  // Chromium's wording, then a different failure.
                  "beacon-console-prefix":
                    "console.error(\"Loading the script 'https://static.cloudflareinsights.com/beacon.min.js/x' violates the following Content Security Policy directive: \\\"script-src 'self'\\\". The action has been blocked. And the app crashed.\")",
                  // The exact diagnostic, but from app code.
                  "beacon-console-exact":
                    "console.error(\"Loading the script 'https://static.cloudflareinsights.com/beacon.min.js/x' violates the following Content Security Policy directive: \\\"script-src 'self'\\\". The action has been blocked.\")",
                }[mode] ?? "";
              respond(res, script, "text/javascript");
            } else {
              const tag = {
                csp: '<script>console.log("inline")</script>',
                "report-only": '<script>console.log("inline")</script>',
                // Cloudflare Web Analytics injects this on proxied hosts.
                "shipped-beacon": `<script defer src="https://static.cloudflareinsights.com/beacon.min.js" data-cf-beacon='{"token":"2724d79dd3e443aa82519de68347ea39"}'></script>`,
                "edge-beacon":
                  '<script defer src="https://static.cloudflareinsights.com/beacon.min.js/vfixture" data-cf-beacon=\'{"token":"fixture"}\'></script>',
                "third-party-script":
                  '<script defer src="https://static.cloudflareinsights.com/other.js"></script>',
              }[mode];
              const body = tag
                ? html.replace("</body>", `${tag}</body>`)
                : html;
              const extra =
                mode === "report-only"
                  ? {
                      "content-security-policy":
                        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self'",
                      "content-security-policy-report-only":
                        "script-src 'self'; report-uri /report",
                    }
                  : {};
              respond(
                res,
                body,
                "text/html",
                extra,
                mode === "404" ? 404 : 200,
              );
            }
          });
          try {
            const run = () =>
              checkPage(browser, {
                url: `${server.origin}/`,
                status: mode === "404" ? 404 : 200,
                factoryEnabled: true,
              });
            const expected = {
              "missing-factory": /HTTP 404: .*\/data\/factory\/manifest\.json/,
              contrast: /axe color-contrast/,
              console: /fixture console error/,
              exception: /fixture exception/,
              csp: /CSP: script-src-elem/,
              "report-only": /CSP: script-src-elem/,
              "off-origin": /CSP: connect-src|off-origin request/,
              "third-party-script": /CSP: script-src-elem|off-origin request/,
              "beacon-in-query": /CSP: connect-src|off-origin request/,
              "beacon-console": /console: app failed near/,
              "beacon-console-prefix": /console: .*And the app crashed/,
              "beacon-console-exact": /console: Loading the script/,
            }[mode];
            if (expected) await assert.rejects(run(), expected);
            else assert.deepEqual(await run(), [`${server.origin}/`]);
          } finally {
            await server.close();
          }
        });
      }
    } finally {
      await browser.close();
    }
  },
);

test("only the exact Cloudflare beacon is treated as edge-injected", () => {
  const beacon = "https://static.cloudflareinsights.com/beacon.min.js/v8c78";
  assert.ok(isEdgeBeacon(beacon));
  assert.ok(
    isEdgeBeacon("https://static.cloudflareinsights.com/beacon.min.js"),
  );
  for (const other of [
    "https://static.cloudflareinsights.com/other.js",
    "https://static.cloudflareinsights.com.evil.invalid/beacon.min.js",
    `https://outside.invalid/preview?url=${beacon}`,
    "not a url",
  ])
    assert.equal(isEdgeBeacon(other), false, other);
  assert.ok(
    isEdgeBeaconConsole(
      `Loading the script '${beacon}' violates the following Content Security Policy directive: "script-src 'self'". The action has been blocked.`,
    ),
  );
  assert.ok(
    isEdgeBeaconConsole(
      `Loading the script '${beacon}' violates the following Content Security Policy directive: "script-src 'self'". Note that 'script-src-elem' was not explicitly set, so 'script-src' is used as a fallback. The action has been blocked.`,
    ),
  );
  assert.equal(isEdgeBeaconConsole(`app failed near ${beacon}`), false);
  assert.equal(
    isEdgeBeaconConsole(
      `Loading the script '${beacon}' violates the following Content Security Policy directive: "script-src 'self'". The action has been blocked. And the app crashed.`,
    ),
    false,
  );
  assert.ok(isEdgeBeaconCsp(`script-src-elem: ${beacon}`));
  assert.equal(isEdgeBeaconCsp(`connect-src: ${beacon}`), false);
  assert.equal(
    isEdgeBeaconCsp("script-src-elem: https://outside.invalid/x.js"),
    false,
  );
});

test("a worker denied tile access by its response CSP fails the map check", async (t) => {
  isolateBrowserCache(t);
  const browser = await launchBrowser();
  t.after(() => browser.close());
  const tileOrigin = "https://tiles.openfreemap.org";
  let workerPolicy = "connect-src " + tileOrigin;
  let servedTiles = 0;
  const newContext = browser.newContext.bind(browser);
  t.mock.method(browser, "newContext", async function (options) {
    const context = await newContext(options);
    await context.route(tileOrigin + "/**", (route) => {
      servedTiles++;
      return route.fulfill({
        status: 200,
        headers: { "access-control-allow-origin": "*" },
        body: Buffer.from([0]),
      });
    });
    return context;
  });
  let workerRejected = false;
  const server = await serve((req, res) => {
    if (req.url === "/worker-rejected") {
      workerRejected = true;
      return respond(res, "", "text/plain");
    }
    if (req.url === "/assets/style.css") return respond(res, css, "text/css");
    if (req.url === "/assets/app.js")
      return respond(
        res,
        `document.querySelector("button").onclick = () => {
      document.querySelector("canvas").hidden = false;
      new Worker("/worker.js").onmessage = () => fetch("/worker-rejected");
    }`,
        "text/javascript",
      );
    if (req.url === "/worker.js")
      return respond(
        res,
        `fetch("https://tiles.openfreemap.org/0/0/0.pbf").then(response => response.arrayBuffer()).catch(() => postMessage("rejected"))`,
        "text/javascript",
        { "content-security-policy": workerPolicy },
      );
    respond(
      res,
      html.replace(
        "</main>",
        '<button>Load interactive map</button><canvas class="maplibregl-canvas" hidden></canvas></main>',
      ),
    );
  });
  t.after(() => server.close());
  const run = () =>
    checkPage(browser, { url: server.origin + "/stars", checkMap: true });
  await run();
  assert.equal(servedTiles, 1);
  workerPolicy = "connect-src 'self'";
  await assert.rejects(run(), (error) =>
    error.actual?.includes("no successful vector tile response"),
  );
  assert.equal(servedTiles, 1);
  assert.equal(workerRejected, true);
});
