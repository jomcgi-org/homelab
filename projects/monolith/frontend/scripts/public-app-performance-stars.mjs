#!/usr/bin/env node
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import {
  pageCohort,
  pageRequest,
  pageDestination,
  sameDocumentUrl,
} from "../src/lib/public/performance/page-cohort.js";

const { values } = parseArgs({
  options: {
    "playwright-module": { type: "string" },
    "page-cache-key": { type: "string" },
    output: {
      type: "string",
      default: "public-app-performance/stars-diagnostic",
    },
    help: { type: "boolean", default: false },
  },
});
if (values.help) {
  console.log(`Usage: node scripts/public-app-performance-stars.mjs [options]
  --playwright-module /absolute/path/to/playwright/index.mjs
  --page-cache-key TOKEN         Opt into a stable keyed page cohort (default: canonical)
  --output public-app-performance/stars-diagnostic

Profiles three warm Historical clicks on https://jomcgi.dev/app/stars.
Each run uses a fresh anonymous context and waits for the page's own
/app/stars/history-map request to finish, followed by two animation frames.
No additional application fetches are made. CPU profiles cover the page main
thread only; workers remain unprofiled and their CPU throttling is unverified.
Profiler overhead makes these diagnostic timings separate from benchmark medians.`);
  process.exit(0);
}

const PAGE_URL = "https://jomcgi.dev/app/stars";
const pageVisit = pageRequest(
  "https://jomcgi.dev",
  "/app/stars",
  pageCohort(values["page-cache-key"]),
);
const HISTORY_URL = "https://jomcgi.dev/app/stars/history-map";
const SELECTOR = '[aria-label="Map mode"] button:has-text("Historical")';
const profile = {
  latency: 150,
  downloadThroughput: 1_600_000 / 8,
  uploadThroughput: 750_000 / 8,
  cpuSlowdown: 4,
  viewport: { width: 360, height: 800 },
  deviceScaleFactor: 2,
};
const output = path.resolve(values.output);
await mkdir(output, { recursive: true });
const report = {
  schemaVersion: 1,
  purpose: "Stars warm Historical click, page-main-thread CPU diagnosis",
  url: PAGE_URL,
  ...pageVisit,
  historyUrl: HISTORY_URL,
  startedAt: new Date().toISOString(),
  harnessSha: process.env.HARNESS_SHA ?? null,
  deployedSha: null,
  profile,
  plannedRuns: 3,
  samplingIntervalUs: 1000,
  workerCpuProfiled: false,
  workerCpuThrottleVerified: false,
  notes: [
    "Each run starts with a fresh anonymous browser context. Only the public Stars page is opened; readiness observes its natural history-map request without issuing a fetch.",
    "After the same 15-second post-DOMContentLoaded settle as the benchmark, readiness has a 45-second bound and two animation frames. Response completion does not itself prove map paint or successful application parsing.",
    "Profiler covers the page main thread from just before the click through two animation frames and a one-second observer-delivery window. Raw profiles include trailing idle/rendering work. CDP profile timestamps are monotonic microseconds; event and frame timestamps are document-relative milliseconds. No exact cross-clock sample alignment is asserted.",
    "These diagnostic samples include profiler overhead and never replace normal benchmark medians or field INP. Event Timing duration is browser-rounded, with a 16ms observation threshold; missing entries remain unavailable.",
    "Page CDP network and CPU throttling match the benchmark. Workers are observed but neither CPU-profiled nor certified under this throttle; Chromium 151 rejects worker CPU throttling with AssertPage.",
    "The harness revision identifies the diagnostic script, not the deployed application. Public script URLs identify the observed assets; rollout verification is separate.",
    "The optional page key repeats across all three diagnostic runs and may match a prior benchmark cohort. Actual edge states are recorded; no CDN purge or guaranteed cache miss/generation is implied. Canonical and keyed samples remain separate cohorts, not an isolated causal comparison.",
  ],
  runs: [],
};
let writes = Promise.resolve();
function save() {
  const json = JSON.stringify(report, null, 2) + "\n";
  writes = writes.then(() =>
    writeFile(path.join(output, "diagnostic.json"), json),
  );
  return writes;
}
await save();
const moduleName = values["playwright-module"]
  ? pathToFileURL(path.resolve(values["playwright-module"])).href
  : "playwright";
const { chromium } = await import(moduleName);
const browser = await chromium.launch({ headless: true });
report.browserVersion = browser.version();
process.once("SIGTERM", () => {
  report.interrupted = true;
  report.finishedAt = new Date().toISOString();
  void save().finally(async () => {
    await browser.close().catch(() => {});
    process.exit(124);
  });
});

async function twoFrames(page) {
  return page.evaluate(
    () =>
      new Promise((resolve) =>
        requestAnimationFrame(() =>
          requestAnimationFrame(() => resolve(performance.now())),
        ),
      ),
  );
}

// Serialized into the public page. No imports, application state or source reads.
function installClickObservers() {
  const events = [];
  const longTasks = [];
  const inputEvents = [];
  const observers = [];
  const supported = PerformanceObserver.supportedEntryTypes;
  const isHistorical = (target) =>
    target?.closest?.('[aria-label="Map mode"] button')?.textContent.trim() ===
    "Historical";
  const consumeEvents = (entries) => {
    for (const entry of entries) {
      events.push({
        name: entry.name,
        startTime: entry.startTime,
        duration: entry.duration,
        processingStart: entry.processingStart,
        processingEnd: entry.processingEnd,
        interactionId: entry.interactionId,
        cancelable: entry.cancelable,
        historicalTarget: isHistorical(entry.target),
      });
    }
  };
  const consumeTasks = (entries) => {
    for (const entry of entries) {
      longTasks.push({
        name: entry.name,
        startTime: entry.startTime,
        duration: entry.duration,
        attribution: [...entry.attribution].map((item) => ({
          name: item.name,
          containerType: item.containerType,
        })),
      });
    }
  };
  for (const [type, consume, options] of [
    ["event", consumeEvents, { durationThreshold: 16 }],
    ["longtask", consumeTasks, {}],
  ]) {
    if (!supported.includes(type)) continue;
    const observer = new PerformanceObserver((list) =>
      consume(list.getEntries()),
    );
    observer.observe({ type, ...options });
    observers.push({ observer, consume });
  }
  for (const type of ["pointerdown", "pointerup", "click"]) {
    document.addEventListener(
      type,
      (event) => {
        if (!isHistorical(event.target)) return;
        inputEvents.push({
          name: type,
          timeStamp: event.timeStamp,
          observedAt: performance.now(),
          trusted: event.isTrusted,
        });
      },
      { capture: true, passive: true },
    );
  }
  window.__readStarsClickDiagnostic = () => {
    for (const { observer, consume } of observers) {
      consume(observer.takeRecords());
      observer.disconnect();
    }
    return {
      supported,
      timeOriginMs: performance.timeOrigin,
      capturedUntilMs: performance.now(),
      events,
      longTasks,
      inputEvents,
    };
  };
}

async function measure(run) {
  const result = {
    ...pageVisit,
    run,
    startedAt: new Date().toISOString(),
    phase: "navigation",
    workers: [],
    documentRequests: 0,
    documentUrls: [],
    publicCodeUrls: [],
    errors: [],
  };
  report.runs.push(result);
  await save();
  const context = await browser.newContext({
    viewport: profile.viewport,
    deviceScaleFactor: profile.deviceScaleFactor,
    isMobile: true,
    hasTouch: true,
    serviceWorkers: "block",
    userAgent: `Mozilla/5.0 (Linux; Android 11; moto g power) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${browser.version()} Mobile Safari/537.36`,
  });
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  let profiling = false;
  let historyResponse = null;
  const codeUrls = new Set();
  page.on("worker", (worker) => result.workers.push(worker.url()));
  page.on("pageerror", (error) => result.errors.push(error.message));
  page.on("request", (request) => {
    if (request.isNavigationRequest() && request.frame() === page.mainFrame()) {
      result.documentRequests++;
      result.documentUrls.push(request.url());
    }
  });
  context.on("requestfinished", async (request) => {
    const url = request.url();
    if (
      url.startsWith("https://jomcgi.dev/_app/") &&
      /\.m?js(?:\?|$)/.test(url)
    )
      codeUrls.add(url);
    if (url !== HISTORY_URL) return;
    const response = await request.response().catch(() => null);
    historyResponse = {
      url,
      status: response?.status() ?? null,
      finishedAt: new Date().toISOString(),
      successful: response?.ok() ?? false,
    };
  });
  try {
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
    const response = await page.goto(pageVisit.requestedUrl, {
      waitUntil: "domcontentloaded",
      timeout: 60_000,
    });
    result.status = response?.status() ?? null;
    Object.assign(result, pageDestination(page.url(), pageVisit.pageCache));
    const headers = response ? await response.allHeaders() : {};
    result.headers = Object.fromEntries(
      ["cache-control", "cf-cache-status", "age", "vary"]
        .filter((name) => headers[name] !== undefined)
        .map((name) => [name, headers[name]]),
    );
    if (
      !response?.ok() ||
      !sameDocumentUrl(result.finalUrl, pageVisit.requestedUrl) ||
      !sameDocumentUrl(result.documentUrls[0], pageVisit.requestedUrl)
    )
      throw new Error("The fixed public Stars page did not load successfully");
    await page.waitForTimeout(15_000);
    result.phase = "history-readiness";
    await save();
    const waitingSince = performance.now();
    while (!historyResponse && performance.now() - waitingSince < 45_000)
      await page.waitForTimeout(100);
    result.historyResponse = historyResponse;
    result.historyWaitMs = performance.now() - waitingSince;
    if (!historyResponse?.successful)
      throw new Error(
        "Natural history-map response did not finish successfully",
      );
    result.readyAfterFramesMs = await twoFrames(page);
    if (result.documentRequests !== 1)
      throw new Error(
        "A document reload invalidated this fresh-context sample",
      );
    const button = page.locator(SELECTOR);
    if ((await button.getAttribute("aria-pressed")) !== "false")
      throw new Error("Historical mode was already selected before the click");
    await page.evaluate(installClickObservers);
    await cdp.send("Profiler.enable");
    await cdp.send("Profiler.setSamplingInterval", { interval: 1000 });
    result.phase = "profiling";
    await save();
    await cdp.send("Profiler.start");
    profiling = true;
    result.clickWindowStartMs = await page.evaluate(() => performance.now());
    await button.click({ timeout: 10_000 });
    result.clickResolvedMs = await page.evaluate(() => performance.now());
    result.afterClickFramesMs = await twoFrames(page);
    await page.waitForTimeout(1000);
    result.observation = await page.evaluate(() =>
      window.__readStarsClickDiagnostic(),
    );
    const { profile: cpuProfile } = await cdp.send("Profiler.stop");
    profiling = false;
    result.cpuProfile = `historical-${run}.cpuprofile`;
    await writeFile(
      path.join(output, result.cpuProfile),
      JSON.stringify(cpuProfile),
    );
    result.historicalSelected =
      (await button.getAttribute("aria-pressed")) === "true";
    result.historyUnavailable = await page
      .getByText("Historical data is unavailable right now.", { exact: false })
      .isVisible();
    result.historyLoading = await page
      .getByText("Loading history…", { exact: true })
      .isVisible();
    if (
      !result.historicalSelected ||
      result.historyUnavailable ||
      result.historyLoading
    )
      throw new Error("The click did not finish in loaded Historical mode");
    result.eventTimingAvailable = result.observation.events.some(
      (entry) => entry.interactionId > 0 && entry.historicalTarget,
    );
    if (
      !result.observation.inputEvents.some(
        (entry) => entry.name === "click" && entry.trusted,
      )
    )
      throw new Error("No trusted Historical click was observed");
    if (result.documentRequests !== 1)
      throw new Error("A document reload invalidated the click profile");
    result.phase = "complete";
  } catch (error) {
    result.error = error.message;
    result.failedPhase = result.phase;
    result.phase = "failed";
    if (profiling) {
      const stopped = await cdp.send("Profiler.stop").catch(() => null);
      if (stopped) {
        result.cpuProfile = `historical-${run}-partial.cpuprofile`;
        await writeFile(
          path.join(output, result.cpuProfile),
          JSON.stringify(stopped.profile),
        );
      }
    }
  } finally {
    Object.assign(result, pageDestination(page.url(), pageVisit.pageCache));
    result.publicCodeUrls = [...codeUrls].sort();
    result.finishedAt = new Date().toISOString();
    await save();
    await context.close();
  }
}

try {
  for (let run = 1; run <= 3; run++) await measure(run);
} finally {
  report.finishedAt = new Date().toISOString();
  report.completedRuns = report.runs.filter(
    (run) => run.phase === "complete",
  ).length;
  await save();
  await browser.close();
}
console.log(
  `Stars diagnostic: ${report.completedRuns}/3 complete; ${path.join(output, "diagnostic.json")}`,
);
if (report.completedRuns !== 3) process.exitCode = 1;
