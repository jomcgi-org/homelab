#!/usr/bin/env node
import { randomUUID } from "node:crypto";
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
import {
  validateProbeEndpoints,
  probeUrl,
  endpointCacheGroups,
} from "../src/lib/public/performance/endpoint-probes.js";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { installPerformanceObservers } from "../src/lib/public/performance/browser-observers.js";
import {
  interactionResourceReady,
  cls,
  median,
  navigationHeaderMetrics,
  recordTransferBytes,
  percentile,
  summarize,
} from "../src/lib/public/performance/metrics.js";

const { values } = parseArgs({
  options: {
    origin: { type: "string", default: "https://jomcgi.dev" },
    route: { type: "string", multiple: true },
    "page-cache-key": { type: "string" },
    endpoint: { type: "string", multiple: true },
    "cache-miss-probes": { type: "boolean", default: false },
    runs: { type: "string", default: "3" },
    samples: { type: "string", default: "20" },
    "settle-ms": { type: "string", default: "15000" },
    output: { type: "string", default: "public-app-performance" },
    "playwright-module": { type: "string" },
    executable: { type: "string" },
    proxy: { type: "string" },
    "interaction-ready-url": { type: "string", multiple: true },
    interaction: { type: "string", multiple: true },
    "content-selector": { type: "string", multiple: true },
    help: { type: "boolean", default: false },
  },
});
if (values.help) {
  console.log(`Usage: node scripts/public-app-performance.mjs [options]
  --route /app/hikes             Repeat for any public page (default: seven main apps)
  --page-cache-key TOKEN         Opt into one keyed page cohort, repeated across all runs (default: canonical)
  --endpoint /app/stars/map      Repeat for read-only public data endpoints
  --content-selector '/app/hikes::.map-snapshot .snapshot-row'
                                 Record an explicitly selected visible content milestone
  --interaction '/app/hikes::button.filter-toggle'
                                 Click an explicit read-only control after the load sample
  --cache-miss-probes            Add a separate unique-query series for allowlisted endpoints (max 20 each)
  --interaction-ready-url '/app/stars::/app/stars/history-map'
                                 Observe natural response completion for up to 20s after load cutoff
  --origin https://jomcgi.dev    Origin to measure
  --runs 3 --samples 20          Page runs and endpoint samples
  --settle-ms 15000              Observation time after DOMContentLoaded
  --output public-app-performance
  --playwright-module /absolute/path/to/playwright/index.mjs
  --executable /path/to/chromium --proxy http://proxy:port

Fresh browser context per run; CDN cache is observed, never purged.
150 ms minimum network latency, 1.6 Mbps down, 750 Kbps up, 4x CPU, 360x800 mobile viewport.
INP remains null: optional clicks produce a separate lab Event Timing sample.
GET endpoints only. Results contain URLs and metrics, never response bodies.`);
  process.exit(0);
}
const number = (key, minimum) => {
  const n = Number(values[key]);
  if (!Number.isInteger(n) || n < minimum) throw new Error(`Invalid --${key}`);
  return n;
};
const runs = number("runs", 1);
const samples = number("samples", 1);
const settleMs = number("settle-ms", 0);
if (values["cache-miss-probes"])
  validateProbeEndpoints(values.endpoint ?? [], samples);
const origin = new URL(values.origin);
const pageCache = pageCohort(values["page-cache-key"]);
const routes = values.route ?? [
  "/app/hikes",
  "/app/ships",
  "/app/stars",
  "/app/trips",
  "/scotland",
  "/app/llm-leaderboard",
  "/app/notes",
];
const pageRequests = new Map(
  routes.map((route) => [route, pageRequest(origin, route, pageCache)]),
);
function selectors(option) {
  const entries = new Map(
    (values[option] ?? []).map((entry) => {
      const separator = entry.indexOf("::");
      if (separator < 1 || !entry.slice(separator + 2).trim())
        throw new Error(`--${option} requires /path::selector`);
      return [entry.slice(0, separator), entry.slice(separator + 2)];
    }),
  );
  for (const route of entries.keys()) {
    if (!routes.includes(route))
      throw new Error(`${option} route ${route} is not selected by --route`);
  }
  return entries;
}
const interactions = selectors("interaction");
const contentSelectors = selectors("content-selector");
const interactionReadyUrls = selectors("interaction-ready-url");
for (const route of interactionReadyUrls.keys()) {
  if (!interactions.has(route))
    throw new Error("Interaction readiness requires a matching --interaction");
  urlFor(interactionReadyUrls.get(route));
}
function urlFor(route) {
  const url = new URL(route, origin);
  if (url.origin !== origin.origin)
    throw new Error("Routes must share --origin");
  return url.href;
}
const headerNames = [
  "cache-control",
  "cloudflare-cdn-cache-control",
  "cf-cache-status",
  "age",
  "vary",
  "content-encoding",
];
function headersFor(headers) {
  return {
    ...Object.fromEntries(
      headerNames
        .filter((name) => headers[name] !== undefined)
        .map((name) => [name, headers[name]]),
    ),
    setsCookie: Boolean(headers["set-cookie"]),
  };
}
const moduleName = values["playwright-module"]
  ? pathToFileURL(path.resolve(values["playwright-module"])).href
  : "playwright";
const { chromium } = await import(moduleName);
const output = path.resolve(values.output);
await mkdir(output, { recursive: true });
const profile = {
  latency: 150,
  downloadThroughput: 1_600_000 / 8,
  uploadThroughput: 750_000 / 8,
  cpuSlowdown: 4,
  viewport: { width: 360, height: 800 },
  deviceScaleFactor: 2,
};
const proxy = values.proxy ? { server: values.proxy } : undefined;
const browser = await chromium.launch({
  headless: true,
  ...(values.executable ? { executablePath: values.executable } : {}),
  ...(proxy ? { proxy } : {}),
});
const report = {
  schemaVersion: 3,
  startedAt: new Date().toISOString(),
  origin: origin.origin,
  pageCache,
  profile,
  settleMs,
  notes: [
    "Cold browser context; observed edge state, no CDN purge.",
    "Page cohorts are canonical by default. An explicit page cache key repeats across runs and changes only the requested page URL. Actual edge states and redirect destinations remain observed; a key guarantees neither MISS nor the deployed generation. Never pool canonical and keyed cohorts or attribute timing differences solely to code.",
    "TTFB is the final main-document CDP responseReceived timestamp relative to its first requestWillBeSent, including redirect time. Raw NavigationTiming and complete navigation timing are saved separately; Chromium 151 NavigationTiming omits emulated latency. Calibration is required before claiming a mobile TTFB pass.",
    "An optional visibleContentMs records the first observed visible selector match with positive clipped intersection. Visibility is checked on intersection changes and at a nominal 100 ms interval until the cutoff; it is a DOM milestone, not a paint or map-readiness guarantee.",
    "Context-wide request events cover page, worker and child-frame requests. Request.sizes() supplies encoded body plus response-header sizes. Full transfer totals are unavailable when any request remains unfinished, failed or unsized at the observation cutoff; completed-response lower bounds remain in JSON.",
    "Worker bootstrap responses with zero reported body size and no explicit Content-Length:0, or a body size inconsistent with Content-Length, remain unsized. Header-byte lower bounds and reported size components are retained.",
    "Redirects without Content-Length have unknown body size: Playwright's fallback zero is not a measurement. Such a redirect keeps the whole-page transfer total unavailable.",
    "Detailed CDP initiators cover the page target only. A worker or child frame makes the complete initiator inventory unavailable; page-target partial observations remain in JSON.",
    "The separate page-target CDP byte estimate includes response bytes received plus body chunks. Finished-request totals replace the running estimate; this partial inventory is never used as a whole-page total.",
    "CPU/network throttling applies to the page target. Runs with workers or child frames have profileComplete=false and cannot certify the full mobile target profile.",
    "Optional interaction readiness waits only for an already natural successful response to finish, after freezing load metrics, then allows two animation frames. It does not fetch the URL or prove map readiness; unready clicks retain raw Event Timing but are excluded from interaction medians.",
    "LCP and CLS are lab observations through that window. inpMs is null; interactionLatencyMs is an optional synthetic Event Timing sample.",
    "Interrupted or failed page observations preserve context-wide and page-target request inventories, including received HTTP errors, with inventoryPhase=error. Size collection and context cleanup each have a 5 second bound.",
    "Multiple top-level document requests make a sample ineligible for medians: a reload resets NavigationTiming/LCP clocks while the request inventory retains both documents. Raw observations and navigation chains remain in JSON.",
    "Optional cache-miss probes append unique ignored query keys to allowlisted read-only endpoints. Actual edge status is recorded per sample; this does not guarantee a CDN miss or a cold backend.",
    "Endpoint timings are unthrottled full-response durations, separate from page navigation TTFB.",
  ],
  pages: [],
  endpoints: [],
  cacheMissProbes: [],
};
async function save() {
  await writeFile(
    path.join(output, "results.json"),
    JSON.stringify(report, null, 2) + "\n",
  );
}

async function measure(route, run) {
  const pageVisit = pageRequests.get(route);
  const startedAt = new Date().toISOString();
  const context = await browser.newContext({
    viewport: profile.viewport,
    deviceScaleFactor: profile.deviceScaleFactor,
    isMobile: true,
    hasTouch: true,
    serviceWorkers: "block",
    userAgent: `Mozilla/5.0 (Linux; Android 11; moto g power) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${browser.version()} Mobile Safari/537.36`,
  });
  const contextRequests = observeContextRequests(context);
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  const requests = [];
  const active = new Map();
  const errors = [];
  const otherTargets = [];
  // Playwright exposes dedicated workers, including nested worker sessions.
  // Child frames can use separate renderer targets, so treat them conservatively.
  page.on("worker", (worker) =>
    otherTargets.push({ type: "worker", url: worker.url() }),
  );
  page.on("frameattached", (frame) =>
    otherTargets.push({ type: "frame", url: frame.url() }),
  );
  page.on("pageerror", (error) => errors.push(error.message));
  await cdp.send("Network.enable");
  await cdp.send("Network.emulateNetworkConditions", {
    offline: false,
    latency: profile.latency,
    downloadThroughput: profile.downloadThroughput,
    uploadThroughput: profile.uploadThroughput,
    connectionType: "cellular4g",
  });
  await cdp.send("Emulation.setCPUThrottlingRate", {
    rate: profile.cpuSlowdown,
  });
  const { frameTree } = await cdp.send("Page.getFrameTree");
  const navigationTracker = createNavigationTracker(
    frameTree.frame.id,
    pageVisit.requestedUrl,
  );
  cdp.on("Network.requestWillBeSent", (event) => {
    if (event.redirectResponse && active.has(event.requestId)) {
      const previous = active.get(event.requestId);
      previous.status = event.redirectResponse.status;
      recordTransferBytes(
        previous,
        "finished",
        event.redirectResponse.encodedDataLength,
      );
      previous.complete = true;
    }
    navigationTracker.request(event);
    const stack = event.initiator?.stack?.callFrames ?? [];
    const item = {
      id: `${event.requestId}:${requests.length}`,
      url: event.request.url,
      type: event.type,
      mainNavigation: navigationTracker.isInitialDocument(event),
      startMs:
        (event.timestamp - (navigationTracker.start ?? event.timestamp)) * 1000,
      initiator: event.initiator?.type,
      initiatorUrl:
        event.initiator?.url ?? stack.find((frame) => frame.url)?.url ?? null,
      transferBytes: null,
      complete: false,
    };
    requests.push(item);
    active.set(event.requestId, item);
  });
  cdp.on("Network.responseReceived", (event) => {
    navigationTracker.response(
      event,
      headersFor(
        Object.fromEntries(
          Object.entries(event.response.headers ?? {}).map(([name, value]) => [
            name.toLowerCase(),
            value,
          ]),
        ),
      ),
    );
    const item = active.get(event.requestId);
    if (item) {
      item.responseReceivedMs =
        (event.timestamp - navigationTracker.start) * 1000;
      item.responseTiming = event.response.timing ?? null;
      item.status = event.response.status;
      item.mimeType = event.response.mimeType;
      item.fromDiskCache = event.response.fromDiskCache ?? false;
      recordTransferBytes(item, "response", event.response.encodedDataLength);
    }
  });
  cdp.on("Network.dataReceived", (event) => {
    const item = active.get(event.requestId);
    if (item) recordTransferBytes(item, "data", event.encodedDataLength);
  });
  cdp.on("Network.loadingFinished", (event) => {
    const item = active.get(event.requestId);
    if (item) {
      recordTransferBytes(item, "finished", event.encodedDataLength);
      item.endMs = (event.timestamp - navigationTracker.start) * 1000;
      item.complete = true;
    }
  });
  cdp.on("Network.loadingFailed", (event) => {
    const item = active.get(event.requestId);
    if (item) {
      item.failed = event.errorText;
      item.complete = true;
    }
  });
  await page.addInitScript(installPerformanceObservers, {
    contentSelector: contentSelectors.get(route) ?? null,
    contentTimeoutMs: 60_000 + settleMs,
  });
  const result = { ...pageVisit, run, startedAt, inpMs: null };
  try {
    const response = await page.goto(pageVisit.requestedUrl, {
      waitUntil: "domcontentloaded",
      timeout: 60_000,
    });
    result.status = response?.status() ?? null;
    result.finalUrl = page.url();
    result.headers = headersFor(response ? await response.allHeaders() : {});
    await page.waitForTimeout(settleMs);
    const observation = await page.evaluate(() => {
      window.__stopPublicAppContentObservation();
      return {
        ...window.__publicAppPerf,
        navigation: performance.getEntriesByType("navigation")[0]?.toJSON(),
        observedUntilMs: performance.now(),
        timeOriginMs: performance.timeOrigin,
        observedAt: new Date().toISOString(),
      };
    });
    const loadCutoff = performance.now();
    result.finalUrl = page.url();
    result.observedAt = observation.observedAt;
    result.timeOriginMs = observation.timeOriginMs;
    result.observedUntilMs = observation.observedUntilMs;
    const loadNavigation = navigationTracker.snapshot();
    result.navigationsDuringLoad = loadNavigation.navigations;
    Object.assign(result, loadNavigation);
    const finalDocument = loadNavigation.navigations.at(-1);
    if (finalDocument?.status) result.status = finalDocument.status;
    if (finalDocument?.headers) result.headers = finalDocument.headers;
    Object.assign(
      result,
      navigationHeaderMetrics(requests, observation.navigation),
    );
    result.navigationTiming = observation.navigation ?? null;
    result.visibleContentMs = observation.visibleContentMs;
    result.contentSelector = observation.contentSelector;
    result.contentSelectorError = observation.contentSelectorError;
    result.contentObservationIntervalMs =
      observation.contentObservationIntervalMs;
    result.contentObservationStopReason =
      observation.contentObservationStopReason;
    result.lcpCandidates = observation.lcp;
    const lcp = observation.lcp.at(-1);
    result.lcpMs = lcp?.startTime ?? null;
    result.lcpElement = lcp?.element ?? null;
    result.lcpSelector = lcp?.selector ?? null;
    result.lcpUrl = lcp?.url ?? null;
    result.cls = observation.supported.includes("layout-shift")
      ? cls(observation.shifts)
      : null;
    result.otherTargetsDuringLoad = structuredClone(otherTargets);
    result.unobservedTargets = otherTargets.length > 0;
    result.profileComplete = !result.unobservedTargets;
    // Freeze both inventories at the same cutoff. Only requests already finished
    // here may contribute final sizes; later completion does not move the cutoff.
    const contextSnapshot = [...contextRequests.values()].map((item) => ({
      ...item,
    }));
    await ensureRequestInventory(
      result,
      contextSnapshot,
      requests,
      otherTargets,
      "load-cutoff",
    );
    result.pageTargetInitiatorsBeforeLcp = result.pageTargetRequests
      .filter((r) => result.lcpMs !== null && r.startMs <= result.lcpMs)
      .map(({ url, type, initiator, initiatorUrl, startMs, endMs }) => ({
        url,
        type,
        initiator,
        initiatorUrl,
        startMs,
        endMs,
      }));
    result.initiatorsBeforeLcp =
      result.unobservedTargets || !result.comparisonEligible
        ? null
        : result.pageTargetInitiatorsBeforeLcp;
    result.interactionLatencyMs = null;
    if (result.contentSelectorError) result.error = result.contentSelectorError;
    if (interactions.has(route) && result.comparisonEligible) {
      result.interactionSelector = interactions.get(route);
      try {
        if (interactionReadyUrls.has(route)) {
          const readyUrl = urlFor(interactionReadyUrls.get(route));
          const start = performance.now();
          const completedBeforeLoadCutoff = interactionResourceReady(
            contextSnapshot,
            readyUrl,
          );
          while (
            !interactionResourceReady(
              [...contextRequests.values()],
              readyUrl,
            ) &&
            performance.now() - loadCutoff < 20000
          )
            await page.waitForTimeout(100);
          const responseFinished = interactionResourceReady(
            [...contextRequests.values()],
            readyUrl,
          );
          if (responseFinished)
            await page.evaluate(
              () =>
                new Promise((resolve) =>
                  requestAnimationFrame(() => requestAnimationFrame(resolve)),
                ),
            );
          result.interactionReadiness = {
            url: readyUrl,
            completedBeforeLoadCutoff,
            responseFinished,
            waitedMs: performance.now() - start,
            loadCutoffElapsedMs: performance.now() - loadCutoff,
            maximumWaitMs: 20000,
            animationFramesAfterCompletion: responseFinished ? 2 : 0,
          };
          result.interactionComparable = responseFinished;
        }
        const since = await page.evaluate(() => performance.now());
        await page
          .locator(result.interactionSelector)
          .first()
          .click({ timeout: 10_000 });
        await page.waitForTimeout(1000);
        const events = await page.evaluate(
          (start) =>
            window.__publicAppPerf.events.filter(
              (e) => e.startTime >= start && e.interactionId > 0,
            ),
          since,
        );
        result.interactionLatencyMs = events.length
          ? Math.max(...events.map((e) => e.duration))
          : null;
        result.interactionEvents = events;
      } catch (error) {
        result.interactionError = error.message;
      }
    }
  } catch (error) {
    result.error = error.message;
  } finally {
    Object.assign(result, pageDestination(page.url(), pageCache));
    await ensureRequestInventory(
      result,
      contextRequests.values(),
      requests,
      otherTargets,
      "error",
    );
    Object.assign(result, navigationTracker.snapshot());
    result.finishedAt = new Date().toISOString();
    result.pageErrors = errors;
    result.otherTargets = structuredClone(otherTargets);
    // An interaction can create a worker after the load observation.
    result.profileComplete = otherTargets.length === 0 && !result.error;
    try {
      await withinDeadline(
        context.close(),
        5000,
        "browser context cleanup timed out",
      );
    } catch (error) {
      result.cleanupError = error.message;
      result.error ??= error.message;
      result.profileComplete = false;
    }
  }
  return result;
}

function summarizeEndpoint(rows) {
  const times = rows
    .filter((r) => !r.error && r.status >= 200 && r.status < 400)
    .map((r) => r.totalMs);
  return {
    p50Ms: median(times),
    p95Ms: percentile(times, 0.95),
    successfulSamples: times.length,
  };
}
async function measureEndpoint(endpoint, uniqueQuery) {
  const entry = {
    endpoint,
    mode: uniqueQuery ? "unique-query" : "normal-key",
    samples: [],
  };
  const nonce = randomUUID();
  // No previous endpoint or response cookie may personalize a subsequent read.
  const context = await browser.newContext();
  try {
    for (let sample = 1; sample <= samples; sample++) {
      await context.clearCookies();
      const url = uniqueQuery
        ? probeUrl(origin, endpoint, nonce, sample)
        : urlFor(endpoint);
      const startedAt = new Date().toISOString();
      const start = performance.now();
      try {
        const response = await context.request.get(url, { timeout: 60_000 });
        try {
          const body = await response.body();
          entry.samples.push({
            sample,
            url,
            startedAt,
            status: response.status(),
            totalMs: performance.now() - start,
            decodedBytes: body.length,
            headers: headersFor(response.headers()),
          });
        } finally {
          await response.dispose();
        }
      } catch (error) {
        entry.samples.push({ sample, url, startedAt, error: error.message });
      }
    }
  } finally {
    await context.close();
  }
  Object.assign(entry, summarizeEndpoint(entry.samples));
  entry.byCacheStatus = endpointCacheGroups(entry.samples, summarizeEndpoint);
  return entry;
}

try {
  for (const route of routes) {
    const entry = { ...pageRequests.get(route), runs: [] };
    report.pages.push(entry);
    for (let run = 1; run <= runs; run++) {
      entry.runs.push(await measure(route, run));
      entry.median = summarize(entry.runs);
      await save();
      console.log(JSON.stringify({ route, run, ...entry.median }));
    }
  }
  // Keep normal-key and unique-query observations in separate series.
  for (const endpoint of values.endpoint ?? []) {
    const entry = await measureEndpoint(endpoint, false);
    report.endpoints.push(entry);
    await save();
  }
  if (values["cache-miss-probes"]) {
    for (const endpoint of values.endpoint) {
      const entry = await measureEndpoint(endpoint, true);
      report.cacheMissProbes.push(entry);
      await save();
    }
  }
} finally {
  await browser.close();
  report.finishedAt = new Date().toISOString();
  await save();
}
const cell = (value, digits = 0) =>
  Number.isFinite(value) ? value.toFixed(digits) : "unavailable";
const table = [
  pageCache.mode === "keyed"
    ? `Page cohort: keyed (${pageCache.parameter}=${pageCache.key}), reused across runs; actual edge states below. Do not combine with canonical cohorts. Redirect destinations that drop the key are marked key-not-retained in JSON and are not a fresh-key destination observation.`
    : "Page cohort: canonical URLs; actual edge states below.",
  "",
  "| Route | Runs | Complete profile | Header arrival ms | Raw nav TTFB ms | Visible content ms | LCP ms | CLS | Lab interaction ms | Lab samples | INP | Transfer KiB | JS KiB | Requests | Edge |",
  "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | --- |",
];
for (const page of report.pages) {
  const m = page.median;
  table.push(
    `| ${page.route} | ${m.successfulRuns}/${m.runs} | ${m.completeProfileRuns}/${m.runs} | ${cell(m.ttfbMs)} | ${cell(m.rawNavigationTtfbMs)} | ${cell(m.visibleContentMs)} | ${cell(m.lcpMs)} | ${cell(m.cls, 3)} | ${cell(m.interactionLatencyMs)} | ${m.comparableInteractionRuns}/${m.runs} | unavailable | ${m.transferBytes === null ? "unavailable" : cell(m.transferBytes / 1024, 1)} | ${m.javascriptBytes === null ? "unavailable" : cell(m.javascriptBytes / 1024, 1)} | ${cell(m.requestCount)} | ${m.cacheStatus.join(", ")} |`,
  );
}
for (const [title, entries] of [
  ["Normal-key endpoints", report.endpoints],
  ["Unique-query probes (observed edge state)", report.cacheMissProbes],
]) {
  if (!entries.length) continue;
  table.push(
    "",
    title,
    "",
    "| Public endpoint | Successful samples | p50 full response ms | p95 full response ms | Edge samples |",
    "| --- | ---: | ---: | ---: | --- |",
  );
  for (const e of entries)
    table.push(
      `| ${e.endpoint} | ${e.successfulSamples}/${e.samples.length} | ${cell(e.p50Ms)} | ${cell(e.p95Ms)} | ${Object.entries(
        e.byCacheStatus,
      )
        .map(([status, group]) => `${status}: ${group.samples}`)
        .join(", ")} |`,
    );
}
await writeFile(path.join(output, "table.md"), table.join("\n") + "\n");
console.log(table.join("\n"));
if (
  report.pages.some((p) => p.median.successfulRuns !== runs) ||
  [...report.endpoints, ...report.cacheMissProbes].some(
    (e) => e.successfulSamples !== samples,
  )
)
  process.exitCode = 1;
