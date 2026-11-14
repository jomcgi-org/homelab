#!/usr/bin/env node
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import {
  cls,
  median,
  recordTransferBytes,
  responseTransferTotals,
  percentile,
  summarize,
  transferTotals,
} from "../src/lib/public/performance/metrics.js";

const { values } = parseArgs({
  options: {
    origin: { type: "string", default: "https://jomcgi.dev" },
    route: { type: "string", multiple: true },
    endpoint: { type: "string", multiple: true },
    runs: { type: "string", default: "3" },
    samples: { type: "string", default: "20" },
    "settle-ms": { type: "string", default: "15000" },
    output: { type: "string", default: "public-app-performance" },
    "playwright-module": { type: "string" },
    executable: { type: "string" },
    proxy: { type: "string" },
    interaction: { type: "string", multiple: true },
    help: { type: "boolean", default: false },
  },
});
if (values.help) {
  console.log(`Usage: node scripts/public-app-performance.mjs [options]
  --route /app/hikes             Repeat for any public page (default: seven main apps)
  --endpoint /app/stars/map      Repeat for read-only public data endpoints
  --interaction '/app/hikes::button.filter-toggle'
                                 Click an explicit read-only control after the load sample
  --origin https://jomcgi.dev    Origin to measure
  --runs 3 --samples 20          Page runs and endpoint samples
  --settle-ms 15000              Observation time after DOMContentLoaded
  --output public-app-performance
  --playwright-module /absolute/path/to/playwright/index.mjs
  --executable /path/to/chromium --proxy http://proxy:port

Fresh browser context per run; CDN cache is observed, never purged.
150 ms RTT, 1.6 Mbps down, 750 Kbps up, 4x CPU, 360x800 mobile viewport.
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
const origin = new URL(values.origin);
const routes = values.route ?? [
  "/app/hikes",
  "/app/ships",
  "/app/stars",
  "/app/trips",
  "/scotland",
  "/app/llm-leaderboard",
  "/app/notes",
];
const interactions = new Map(
  (values.interaction ?? []).map((entry) => {
    const separator = entry.indexOf("::");
    if (separator < 1)
      throw new Error("--interaction requires /path::selector");
    return [entry.slice(0, separator), entry.slice(separator + 2)];
  }),
);
for (const route of interactions.keys()) {
  if (!routes.includes(route))
    throw new Error(`Interaction route ${route} is not selected by --route`);
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
  startedAt: new Date().toISOString(),
  origin: origin.origin,
  profile,
  settleMs,
  notes: [
    "Cold browser context; observed edge state, no CDN purge.",
    "TTFB is navigation responseStart minus startTime and includes connection setup.",
    "Context-wide request events cover page, worker and child-frame requests. Request.sizes() supplies encoded body plus response-header sizes. Full transfer totals are unavailable when any request remains unfinished, failed or unsized at the observation cutoff; completed-response lower bounds remain in JSON.",
    "Detailed CDP initiators cover the page target only. A worker or child frame makes the complete initiator inventory unavailable; page-target partial observations remain in JSON.",
    "The separate page-target CDP byte estimate includes response bytes received plus body chunks. Finished-request totals replace the running estimate; this partial inventory is never used as a whole-page total.",
    "CPU/network throttling applies to the page target. Runs with workers or child frames have profileComplete=false and cannot certify the full mobile target profile.",
    "LCP and CLS are lab observations through that window. inpMs is null; interactionLatencyMs is an optional synthetic Event Timing sample.",
    "Endpoint timings are unthrottled full-response durations, separate from page navigation TTFB.",
  ],
  pages: [],
  endpoints: [],
};
async function save() {
  await writeFile(
    path.join(output, "results.json"),
    JSON.stringify(report, null, 2) + "\n",
  );
}

async function measure(route, run) {
  const context = await browser.newContext({
    viewport: profile.viewport,
    deviceScaleFactor: profile.deviceScaleFactor,
    isMobile: true,
    hasTouch: true,
    serviceWorkers: "block",
    userAgent: `Mozilla/5.0 (Linux; Android 11; moto g power) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${browser.version()} Mobile Safari/537.36`,
  });
  const contextRequests = new Map();
  context.on("request", (request) => {
    const resourceType = request.resourceType();
    contextRequests.set(request, {
      url: request.url(),
      type: resourceType[0].toUpperCase() + resourceType.slice(1),
      complete: false,
      transferBytes: null,
    });
  });
  context.on("requestfinished", (request) => {
    const item = contextRequests.get(request);
    if (!item) return;
    item.complete = true;
    item.sizing = request
      .sizes()
      .then((sizes) => ({
        transferBytes: sizes.responseBodySize + sizes.responseHeadersSize,
      }))
      .catch((error) => ({ sizeError: error.message }));
  });
  context.on("requestfailed", (request) => {
    const item = contextRequests.get(request);
    if (item) {
      item.complete = true;
      item.failed = request.failure()?.errorText ?? "request failed";
    }
  });
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
  let navigationStart;
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
    if (navigationStart === undefined && event.type === "Document")
      navigationStart = event.timestamp;
    const stack = event.initiator?.stack?.callFrames ?? [];
    const item = {
      id: `${event.requestId}:${requests.length}`,
      url: event.request.url,
      type: event.type,
      startMs: (event.timestamp - (navigationStart ?? event.timestamp)) * 1000,
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
    const item = active.get(event.requestId);
    if (item) {
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
      item.endMs = (event.timestamp - navigationStart) * 1000;
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
  await page.addInitScript(() => {
    window.__publicAppPerf = {
      lcp: [],
      shifts: [],
      events: [],
      supported: PerformanceObserver.supportedEntryTypes,
    };
    const observe = (type, callback, options = {}) => {
      if (PerformanceObserver.supportedEntryTypes.includes(type))
        new PerformanceObserver((list) => callback(list.getEntries())).observe({
          type,
          buffered: true,
          ...options,
        });
    };
    observe("largest-contentful-paint", (entries) =>
      window.__publicAppPerf.lcp.push(
        ...entries.map((e) => ({
          startTime: e.startTime,
          size: e.size,
          url: e.url,
          element: e.element?.tagName ?? null,
        })),
      ),
    );
    observe("layout-shift", (entries) =>
      window.__publicAppPerf.shifts.push(
        ...entries.map((e) => ({
          startTime: e.startTime,
          value: e.value,
          hadRecentInput: e.hadRecentInput,
        })),
      ),
    );
    const record = (entries) =>
      window.__publicAppPerf.events.push(
        ...entries.map((e) => ({
          startTime: e.startTime,
          duration: e.duration,
          interactionId: e.interactionId,
          name: e.name,
        })),
      );
    observe("event", record, { durationThreshold: 16 });
    observe("first-input", record);
  });
  const result = { route, run, inpMs: null };
  try {
    const response = await page.goto(urlFor(route), {
      waitUntil: "domcontentloaded",
      timeout: 60_000,
    });
    result.status = response?.status() ?? null;
    result.finalUrl = page.url();
    result.headers = headersFor(response ? await response.allHeaders() : {});
    await page.waitForTimeout(settleMs);
    const observation = await page.evaluate(() => ({
      ...window.__publicAppPerf,
      navigation: performance.getEntriesByType("navigation")[0]?.toJSON(),
      observedUntilMs: performance.now(),
    }));
    result.observedUntilMs = observation.observedUntilMs;
    result.ttfbMs = observation.navigation
      ? observation.navigation.responseStart - observation.navigation.startTime
      : null;
    const lcp = observation.lcp.at(-1);
    result.lcpMs = lcp?.startTime ?? null;
    result.lcpElement = lcp?.element ?? null;
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
    result.pageTargetTotals = transferTotals(requests);
    result.pageTargetRequests = structuredClone(requests);
    result.requests = await Promise.all(
      contextSnapshot.map(async ({ sizing, ...item }) => {
        if (!sizing) return item;
        let timer;
        try {
          const size = await Promise.race([
            sizing,
            new Promise((resolve) => {
              timer = setTimeout(
                () => resolve({ sizeError: "request.sizes timed out" }),
                5000,
              );
            }),
          ]);
          return { ...item, ...size };
        } finally {
          clearTimeout(timer);
        }
      }),
    );
    Object.assign(result, responseTransferTotals(result.requests));
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
    result.initiatorsBeforeLcp = result.unobservedTargets
      ? null
      : result.pageTargetInitiatorsBeforeLcp;
    result.interactionLatencyMs = null;
    if (interactions.has(route)) {
      result.interactionSelector = interactions.get(route);
      const since = await page.evaluate(() => performance.now());
      try {
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
    result.pageErrors = errors;
    result.otherTargets = structuredClone(otherTargets);
    // An interaction can create a worker after the load observation.
    result.profileComplete = otherTargets.length === 0 && !result.error;
    await context.close();
  }
  return result;
}

try {
  for (const route of routes) {
    const entry = { route, runs: [] };
    report.pages.push(entry);
    for (let run = 1; run <= runs; run++) {
      entry.runs.push(await measure(route, run));
      entry.median = summarize(entry.runs);
      await save();
      console.log(JSON.stringify({ route, run, ...entry.median }));
    }
  }
  // Separate contexts prevent an endpoint's Set-Cookie from affecting later reads.
  for (const endpoint of values.endpoint ?? []) {
    const entry = { endpoint, samples: [] };
    report.endpoints.push(entry);
    const context = await browser.newContext();
    try {
      for (let sample = 1; sample <= samples; sample++) {
        await context.clearCookies();
        const start = performance.now();
        try {
          const response = await context.request.get(urlFor(endpoint), {
            timeout: 60_000,
          });
          const body = await response.body();
          entry.samples.push({
            sample,
            status: response.status(),
            totalMs: performance.now() - start,
            decodedBytes: body.length,
            headers: headersFor(response.headers()),
          });
          await response.dispose();
        } catch (error) {
          entry.samples.push({ sample, error: error.message });
        }
      }
    } finally {
      await context.close();
    }
    const times = entry.samples
      .filter((r) => !r.error && r.status >= 200 && r.status < 400)
      .map((r) => r.totalMs);
    entry.p50Ms = median(times);
    entry.p95Ms = percentile(times, 0.95);
    entry.successfulSamples = times.length;
    await save();
  }
} finally {
  await browser.close();
  report.finishedAt = new Date().toISOString();
  await save();
}
const cell = (value, digits = 0) =>
  Number.isFinite(value) ? value.toFixed(digits) : "unavailable";
const table = [
  "| Route | Runs | Complete profile | TTFB ms | LCP ms | CLS | Lab interaction ms | INP | Transfer KiB | JS KiB | Requests | Edge |",
  "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | --- |",
];
for (const page of report.pages) {
  const m = page.median;
  table.push(
    `| ${page.route} | ${m.successfulRuns}/${m.runs} | ${m.completeProfileRuns}/${m.runs} | ${cell(m.ttfbMs)} | ${cell(m.lcpMs)} | ${cell(m.cls, 3)} | ${cell(m.interactionLatencyMs)} | unavailable | ${m.transferBytes === null ? "unavailable" : cell(m.transferBytes / 1024, 1)} | ${m.javascriptBytes === null ? "unavailable" : cell(m.javascriptBytes / 1024, 1)} | ${cell(m.requestCount)} | ${m.cacheStatus.join(", ")} |`,
  );
}
if (report.endpoints.length) {
  table.push(
    "",
    "| Public endpoint | Successful samples | p50 full response ms | p95 full response ms |",
    "| --- | ---: | ---: | ---: |",
  );
  for (const e of report.endpoints)
    table.push(
      `| ${e.endpoint} | ${e.successfulSamples}/${e.samples.length} | ${cell(e.p50Ms)} | ${cell(e.p95Ms)} |`,
    );
}
await writeFile(path.join(output, "table.md"), table.join("\n") + "\n");
console.log(table.join("\n"));
if (
  report.pages.some((p) => p.median.successfulRuns !== runs) ||
  report.endpoints.some((e) => e.successfulSamples !== samples)
)
  process.exitCode = 1;
