#!/usr/bin/env node
import {
  observeContextRequests,
  ensureRequestInventory,
  withinDeadline,
} from "../src/lib/public/performance/request-inventory.js";
import { createNavigationTracker } from "../src/lib/public/performance/navigation.js";
import {
  pageCohort,
  pageRequest,
  pageDestination,
} from "../src/lib/public/performance/page-cohort.js";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import {
  navigationHeaderMetrics,
  summarizeTimingCalibration,
} from "../src/lib/public/performance/metrics.js";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { installPerformanceObservers } from "../src/lib/public/performance/browser-observers.js";

const { values } = parseArgs({
  options: {
    "playwright-module": { type: "string" },
    output: { type: "string", default: "public-app-performance" },
  },
});
const { chromium } = await import(
  values["playwright-module"]
    ? pathToFileURL(path.resolve(values["playwright-module"])).href
    : "playwright"
);
const browser = await chromium.launch({ headless: true });
const results = [];
const navigationResults = [];
const pageCohortResults = [];
const calibration = {
  browser: browser.version(),
  method:
    "Three fresh-context pairs against one local HTTP document, first 0 ms then 150 ms latency. No redirect. Page-target calibration only; worker throttling remains unverified.",
  profile: {
    downloadThroughput: 200000,
    uploadThroughput: 93750,
    cpuSlowdown: 4,
  },
  samples: [],
  passed: false,
};
async function calibrateTiming() {
  const body =
    "<!doctype html><title>Calibration</title><p>Local timing control</p>";
  const server = createServer((_request, response) => {
    response.writeHead(200, {
      "content-type": "text/html",
      "cache-control": "no-store",
      "content-length": Buffer.byteLength(body),
    });
    response.end(body);
  });
  try {
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    const url = `http://127.0.0.1:${server.address().port}/`;
    for (let pair = 0; pair < 3; pair++) {
      for (const latencyMs of [0, 150]) {
        const context = await browser.newContext({
          viewport: { width: 360, height: 800 },
          deviceScaleFactor: 2,
          isMobile: true,
          hasTouch: true,
          serviceWorkers: "block",
        });
        try {
          const page = await context.newPage();
          const cdp = await context.newCDPSession(page);
          await cdp.send("Network.enable");
          await cdp.send("Network.emulateNetworkConditions", {
            offline: false,
            latency: latencyMs,
            downloadThroughput: calibration.profile.downloadThroughput,
            uploadThroughput: calibration.profile.uploadThroughput,
            connectionType: "cellular4g",
          });
          await cdp.send("Emulation.setCPUThrottlingRate", {
            rate: calibration.profile.cpuSlowdown,
          });
          let start;
          let requestId;
          const requests = [];
          cdp.on("Network.requestWillBeSent", (event) => {
            if (start === undefined && event.type === "Document") {
              start = event.timestamp;
              requestId = event.requestId;
            }
          });
          cdp.on("Network.responseReceived", (event) => {
            if (event.requestId === requestId)
              requests.push({
                mainNavigation: true,
                responseReceivedMs: (event.timestamp - start) * 1000,
                responseTiming: event.response.timing,
              });
          });
          const response = await page.goto(url, {
            waitUntil: "domcontentloaded",
            timeout: 10000,
          });
          const navigation = await page.evaluate(() =>
            performance.getEntriesByType("navigation")[0]?.toJSON(),
          );
          const timing = navigationHeaderMetrics(requests, navigation);
          calibration.samples.push({
            pair,
            latencyMs,
            status: response?.status(),
            headerArrivalMs: timing.ttfbMs,
            rawNavigationTtfbMs: timing.rawNavigationTtfbMs,
            navigation,
            requests,
          });
        } finally {
          await context.close();
        }
      }
    }
    Object.assign(calibration, summarizeTimingCalibration(calibration.samples));
    assert.equal(
      calibration.passed,
      true,
      "local CDP header timing must include configured latency",
    );
  } catch (error) {
    calibration.error = error.message;
    throw error;
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

async function navigationFixture() {
  const server = createServer((request, response) => {
    if (request.url === "/missing.js") {
      response.writeHead(404, {
        "content-type": "application/javascript",
        "content-length": 0,
      });
      response.end();
      return;
    }
    if (request.url === "/redirect") {
      response.writeHead(302, { location: "/settled", "content-length": 0 });
      response.end();
      return;
    }
    const body =
      request.url === "/reload"
        ? '<!doctype html><title>Before reload</title><iframe src="/child"></iframe><script>setTimeout(() => location.replace("/settled"), 300)</script>'
        : "<!doctype html><title>Settled</title><p>Local navigation control</p>";
    response.writeHead(200, {
      "content-type": "text/html",
      "cache-control": "no-store",
      "content-length": Buffer.byteLength(body),
    });
    response.end(body);
  });
  try {
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    const origin = `http://127.0.0.1:${server.address().port}`;
    for (const route of ["/redirect", "/reload", "/interrupted"]) {
      const context = await browser.newContext();
      const contextRequests = observeContextRequests(context);
      const pageRequests = [];
      const byId = new Map();
      try {
        const page = await context.newPage();
        const cdp = await context.newCDPSession(page);
        await cdp.send("Network.enable");
        const { frameTree } = await cdp.send("Page.getFrameTree");
        const tracker = createNavigationTracker(frameTree.frame.id);
        cdp.on("Network.requestWillBeSent", (event) => {
          tracker.request(event);
          const item = {
            url: event.request.url,
            type: event.type,
            complete: false,
          };
          pageRequests.push(item);
          byId.set(event.requestId, item);
        });
        cdp.on("Network.responseReceived", (event) => {
          tracker.response(event);
          const item = byId.get(event.requestId);
          if (item) item.status = event.response.status;
        });
        await page.goto(`${origin}${route}`, {
          waitUntil: "domcontentloaded",
          timeout: 10000,
        });
        if (route === "/interrupted") {
          const result = {};
          try {
            await withinDeadline(
              page.evaluate(async () => {
                await import("/missing.js").catch(() => null);
                location.replace("/settled");
                return new Promise(() => {});
              }),
              10000,
              "fixture observation was not interrupted",
            );
          } catch (error) {
            result.error = error.message;
          } finally {
            await ensureRequestInventory(
              result,
              contextRequests.values(),
              pageRequests,
              [],
              "error",
            );
            Object.assign(result, tracker.snapshot());
          }
          assert.match(result.error ?? "", /context.*destroyed|navigation/i);
          assert.ok(
            result.requests.some(
              (request) =>
                request.url === `${origin}/missing.js` &&
                request.status === 404,
            ),
          );
          assert.ok(
            result.pageTargetRequests.some(
              (request) =>
                request.url === `${origin}/missing.js` &&
                request.status === 404,
            ),
          );
          assert.equal(result.httpErrorRequests, 1);
          assert.equal(result.inventoryPhase, "error");
          navigationResults.push({ route, passed: true, ...result });
          continue;
        }
        await page.waitForURL(`${origin}/settled`, {
          waitUntil: "domcontentloaded",
          timeout: 10000,
        });
        const state = tracker.snapshot();
        const clock = await page.evaluate(() => ({
          timeOriginMs: performance.timeOrigin,
          navigation: performance.getEntriesByType("navigation")[0]?.toJSON(),
        }));
        assert.equal(state.navigationCount, route === "/redirect" ? 1 : 2);
        assert.equal(state.comparisonEligible, route === "/redirect");
        assert.equal(state.navigations.at(-1).status, 200);
        if (route === "/redirect")
          assert.equal(state.navigations[0].urls.length, 2);
        navigationResults.push({ route, passed: true, ...state, ...clock });
      } finally {
        await withinDeadline(
          context.close(),
          5000,
          "fixture context cleanup timed out",
        );
      }
    }
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

// Real HTTP navigations prove query identity and repeated request URLs without
// asserting anything about Cloudflare. The local server never contacts an app.
async function pageCohortFixture() {
  const received = [];
  const server = createServer((request, response) => {
    received.push(request.url);
    const url = new URL(request.url, "http://fixture.test");
    if (url.pathname === "/redirect") {
      response.writeHead(302, { location: "/settled", "content-length": 0 });
      response.end();
      return;
    }
    const body =
      url.pathname === "/recover" && !url.searchParams.has("_fresh")
        ? '<!doctype html><script>setTimeout(() => { const u = new URL(location.href); u.searchParams.set("_fresh", "recovery"); location.replace(u); }, 50)</script>'
        : '<!doctype html><p class="data">Real fixture content</p><script>window.dataReady = fetch("/natural-data").then(response => response.text())</script>';
    response.writeHead(200, {
      "content-type": "text/html",
      "cache-control": "no-store",
    });
    response.end(url.pathname === "/natural-data" ? "ready" : body);
  });
  try {
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    const origin = `http://127.0.0.1:${server.address().port}`;
    const cohort = pageCohort("hosted-fixture");
    for (const route of ["/page", "/page", "/page", "/redirect", "/recover"]) {
      const visit = pageRequest(origin, route, cohort);
      const context = await browser.newContext();
      try {
        const page = await context.newPage();
        const cdp = await context.newCDPSession(page);
        await cdp.send("Network.enable");
        const { frameTree } = await cdp.send("Page.getFrameTree");
        const tracker = createNavigationTracker(
          frameTree.frame.id,
          visit.requestedUrl,
        );
        cdp.on("Network.requestWillBeSent", (event) => tracker.request(event));
        cdp.on("Network.responseReceived", (event) => tracker.response(event));
        await page.goto(visit.requestedUrl, {
          waitUntil: "domcontentloaded",
          timeout: 10000,
        });
        if (route === "/recover")
          await page.waitForURL((url) => url.searchParams.has("_fresh"), {
            waitUntil: "domcontentloaded",
            timeout: 10000,
          });
        assert.equal(await page.evaluate(() => window.dataReady), "ready");
        assert.equal(
          await page.locator(".data").textContent(),
          "Real fixture content",
        );
        const state = tracker.snapshot();
        const destination = pageDestination(page.url(), cohort);
        assert.equal(state.requestedUrlMatched, true);
        assert.equal(state.comparisonEligible, route !== "/recover");
        assert.equal(state.navigationCount, route === "/recover" ? 2 : 1);
        assert.equal(
          destination.finalPageCacheKeyRetained,
          route !== "/redirect",
        );
        pageCohortResults.push({
          ...visit,
          ...destination,
          ...state,
          passed: true,
        });
      } finally {
        await withinDeadline(
          context.close(),
          5000,
          "cohort fixture context cleanup timed out",
        );
      }
    }
    assert.deepEqual(
      received.filter((url) => url.startsWith("/page?")),
      Array(3).fill("/page?__public_perf_page=hosted-fixture"),
    );
    assert.equal(received.filter((url) => url === "/natural-data").length, 5);
    assert.ok(!received.some((url) => url.startsWith("/natural-data?")));
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

async function fixture(name, css, expected) {
  const context = await browser.newContext({
    viewport: { width: 360, height: 800 },
  });
  try {
    const page = await context.newPage();
    await page.addInitScript(installPerformanceObservers, {
      contentSelector: ".data",
      contentTimeoutMs: 2000,
    });
    await page.goto(
      `data:text/html,${encodeURIComponent(`<!doctype html><style>
      body { margin: 0 } .clip { position: relative; width: 200px; height: 40px; overflow: hidden }
      .data { position: absolute; width: 150px; height: 30px; margin: 0 }
      ${css} .data { animation-play-state: paused }</style><div class="clip"><p class="data">Real content</p></div>`)}`,
    );
    await page.waitForTimeout(100);
    assert.equal(
      await page.evaluate(() => window.__publicAppPerf.visibleContentMs),
      null,
      `${name}: initially hidden`,
    );
    if (expected) {
      const animationStartedAt = await page.evaluate(() => {
        window.__fixtureMutationCount = 0;
        new MutationObserver(() => window.__fixtureMutationCount++).observe(
          document,
          {
            subtree: true,
            attributes: true,
            childList: true,
          },
        );
        // CSSOM changes do not mutate the DOM. Subsequent animation frames also
        // produce no mutation, resize or load event for the milestone observer.
        const sheet = document.styleSheets[0];
        sheet.insertRule(
          ".data { animation-play-state: running }",
          sheet.cssRules.length,
        );
        return performance.now();
      });
      await page.waitForFunction(
        () => window.__publicAppPerf.visibleContentMs !== null,
        null,
        { timeout: 3000 },
      );
      const result = await page.evaluate(() => window.__publicAppPerf);
      assert.ok(
        result.visibleContentMs >= animationStartedAt + 300,
        `${name}: did not record before CSS animation`,
      );
      assert.equal(result.contentObservationStopReason, "matched");
      assert.equal(await page.evaluate(() => window.__fixtureMutationCount), 0);
      results.push({
        name,
        passed: true,
        visibleContentMs: result.visibleContentMs,
      });
    } else {
      await page.waitForTimeout(500);
      const result = await page.evaluate(() => {
        window.__stopPublicAppContentObservation();
        return {
          visibleContentMs: window.__publicAppPerf.visibleContentMs,
          stopReason: window.__publicAppPerf.contentObservationStopReason,
          targetRect: document
            .querySelector(".data")
            .getBoundingClientRect()
            .toJSON(),
        };
      });
      assert.ok(
        result.targetRect.top > 0 && result.targetRect.bottom < 800,
        "clipped target's own rect is inside viewport",
      );
      assert.equal(result.visibleContentMs, null);
      assert.equal(result.stopReason, "cutoff");
      results.push({ name, passed: true, ...result });
    }
  } finally {
    await context.close();
  }
}
try {
  await fixture(
    "ancestor clips a viewport-local target",
    ".data { top: 60px }",
    false,
  );
  await fixture(
    "CSS-only opacity animation",
    ".data { opacity: 0; animation: appear 200ms linear 400ms forwards } @keyframes appear { to { opacity: 1 } }",
    true,
  );
  await fixture(
    "CSS-only position animation",
    ".data { top: 60px; animation: enter 200ms linear 400ms forwards } @keyframes enter { to { top: 0 } }",
    true,
  );
  await fixture(
    "CSS-only entry from exactly the clipping edge",
    ".data { top: 40px; animation: enter 200ms linear 400ms forwards } @keyframes enter { to { top: 0 } }",
    true,
  );
  await calibrateTiming();
  await navigationFixture();
  await pageCohortFixture();
  console.log("Content observer and local timing Chromium fixtures passed");
} catch (error) {
  results.push({ passed: false, error: error.message });
  throw error;
} finally {
  await browser.close();
  await mkdir(path.resolve(values.output), { recursive: true });
  await writeFile(
    path.join(values.output, "page-cohort-fixture.json"),
    JSON.stringify({ results: pageCohortResults }, null, 2) + "\n",
  );
  await writeFile(
    path.join(values.output, "navigation-fixture.json"),
    JSON.stringify({ results: navigationResults }, null, 2) + "\n",
  );
  await writeFile(
    path.join(values.output, "calibration.json"),
    JSON.stringify(calibration, null, 2) + "\n",
  );
  await writeFile(
    path.join(values.output, "observer-fixture.json"),
    JSON.stringify({ results }, null, 2) + "\n",
  );
}
