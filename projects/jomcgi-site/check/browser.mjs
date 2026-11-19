import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { tmpdir } from "node:os";
import chrome, { inflate } from "@sparticuz/chromium";
import axe from "axe-core";
import { chromium } from "playwright-core";

// Sections staged on the site before their publisher lands: /factory
// (HOME-224) reads /data/factory/ before HOME-213 publishes it. Only these
// sections' /data files may answer 404, so a misspelled data path still
// fails. browser.test.mjs fails once data/sources.json publishes one of them,
// so its entry goes when the publisher lands.
export const PENDING_DATA = ["factory"];

export function pendingData(url, pending = PENDING_DATA) {
  const section = /^\/data\/([^/]+)\//.exec(new URL(url).pathname)?.[1];
  return section !== undefined && pending.includes(section);
}

const NOT_FOUND =
  /^Failed to load resource: the server responded with a status of 404 \(.*\)$/;

export async function launchBrowser() {
  let executablePath = process.env.SITE_CHECK_BROWSER;
  let env = { ...process.env };
  if (!executablePath) {
    // The npm archive contains chrome-headless-shell, fonts and its shared
    // libraries. No browser install or network download occurs in the Job.
    const require = createRequire(import.meta.url);
    const bin = join(dirname(require.resolve("@sparticuz/chromium")), "../bin");
    await inflate(join(bin, "al2023.tar.br"));
    executablePath = await chrome.executablePath();
    // The bundled fontconfig uses /tmp literally; Bazel sets another TMPDIR.
    const xml = (value) =>
      value.replaceAll("&", "&amp;").replaceAll("<", "&lt;");
    const fontconfig = join(tmpdir(), "site-check-fonts.conf");
    await writeFile(
      fontconfig,
      `<fontconfig><dir>${xml(join(tmpdir(), "fonts"))}</dir><cachedir>${xml(join(tmpdir(), "fonts-cache"))}</cachedir></fontconfig>`,
    );
    env = {
      ...env,
      FONTCONFIG_PATH: join(tmpdir(), "fonts"),
      FONTCONFIG_FILE: fontconfig,
      LD_LIBRARY_PATH: [join(tmpdir(), "al2023/lib"), env.LD_LIBRARY_PATH]
        .filter(Boolean)
        .join(":"),
    };
  }
  // Do not use chrome.args: its serverless defaults disable web security.
  // Playwright's defaults keep CSP and normal origin checks enabled.
  return chromium.launch({
    executablePath,
    env,
    headless: true,
    timeout: 30_000,
  });
}

// Block both the shipped beacon and the versioned script injected at the edge.
const BEACON_ORIGIN = "https://static.cloudflareinsights.com";
export function isEdgeBeacon(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    return false;
  }
  return (
    url.origin === BEACON_ORIGIN &&
    (url.pathname === "/beacon.min.js" ||
      url.pathname.startsWith("/beacon.min.js/"))
  );
}
// Chromium's whole diagnostic, start to end, so an app error that starts with
// the same words and goes on to describe another failure still counts.
const BEACON_CSP_CONSOLE =
  /^Loading the script '([^']+)' violates the following Content Security Policy directive: "script-src[^"]*"\.(?: Note that 'script-src-elem' was not explicitly set, so 'script-src' is used as a fallback\.)? The action has been blocked\.$/;
export function isEdgeBeaconConsole(text) {
  const match = BEACON_CSP_CONSOLE.exec(text);
  return Boolean(match && isEdgeBeacon(match[1]));
}
export function isEdgeBeaconCsp(entry) {
  const [directive, ...rest] = entry.split(": ");
  return directive === "script-src-elem" && isEdgeBeacon(rest.join(": "));
}

export async function checkPage(
  browser,
  { url, status = 200, colorScheme = "light", width = 1280 },
) {
  const origin = new URL(url).origin;
  const context = await browser.newContext({
    colorScheme,
    viewport: { width, height: 900 },
    serviceWorkers: "block",
    bypassCSP: false,
  });
  const errors = [];
  try {
    await context.route("**/*", (route) => {
      if (new URL(route.request().url()).origin !== origin) {
        if (isEdgeBeacon(route.request().url())) return route.abort();
        errors.push(`off-origin request: ${route.request().url()}`);
        return route.abort();
      }
      return route.continue();
    });
    await context.routeWebSocket("**/*", (socket) => {
      errors.push(`unexpected WebSocket: ${socket.url()}`);
      socket.close();
    });
    await context.addInitScript(() => {
      globalThis.siteCheckCsp = [];
      document.addEventListener("securitypolicyviolation", (event) => {
        globalThis.siteCheckCsp.push(
          `${event.effectiveDirective}: ${event.blockedURI}`,
        );
      });
    });
    const page = await context.newPage();
    page.setDefaultTimeout(15_000);
    page.on("console", (message) => {
      if (message.type() !== "error") return;
      if (
        message.text().startsWith("Failed to load resource: ") &&
        isEdgeBeacon(message.location().url)
      )
        return;
      // The edge-injected versioned beacon keeps this older exception
      // (decision: https://factory-plan.jomcgi.dev/factory/issue/HOME-382#comment-4115).
      // Chromium attributes its CSP diagnostic to the document; the site's own
      // scripts are external files (the CSP forbids inline script), so an app
      // console call that copies the text reports its script URL instead.
      if (isEdgeBeaconConsole(message.text()) && message.location().url === url)
        return;
      // Chromium reports the deliberately missing document as a console error.
      // Suppress only that exact network message; every app error still fails.
      if (
        NOT_FOUND.test(message.text()) &&
        ((status === 404 && message.location().url === url) ||
          pendingData(message.location().url))
      )
        return;
      errors.push(`console: ${message.text()}`);
    });
    page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
    page.on("crash", () => errors.push("page crashed"));
    page.on("requestfailed", (request) => {
      if (isEdgeBeacon(request.url())) return;
      errors.push(
        `request failed: ${request.url()} ${request.failure()?.errorText}`,
      );
    });
    page.on("response", (response) => {
      if (
        response.status() >= 400 &&
        !(response.url() === url && response.status() === status) &&
        !(response.status() === 404 && pendingData(response.url()))
      ) {
        errors.push(`HTTP ${response.status()}: ${response.url()}`);
      }
    });
    const response = await page.goto(url, {
      waitUntil: "networkidle",
      timeout: 30_000,
    });
    assert.equal(response?.status(), status, `${url}: browser HTTP status`);
    await page.locator("main h1").waitFor({ state: "visible" });
    await page.evaluate(() => document.fonts.ready);
    // DevTools evaluation installs the auditor without adding an inline script
    // tag, which the site's real CSP correctly forbids. CSP stays enabled.
    await page.evaluate(axe.source);
    const results = await page.evaluate(async () =>
      globalThis.axe.run(document, {
        rules: { "color-contrast": { enabled: true } },
      }),
    );
    assert.ok(
      results.passes.some(({ id }) => id === "color-contrast") ||
        results.violations.some(({ id }) => id === "color-contrast"),
      `${url}: colour contrast did not run to a result`,
    );
    errors.push(
      ...(await page.evaluate(() => globalThis.siteCheckCsp))
        .filter((value) => !isEdgeBeaconCsp(value))
        .map((value) => `CSP: ${value}`),
    );
    for (const violation of results.violations) {
      errors.push(
        `axe ${violation.id}: ${violation.nodes.map((node) => `${node.target.join(" ")}: ${node.failureSummary}`).join("; ")}`,
      );
    }
    assert.deepEqual(errors, [], `${url} (${colorScheme}, ${width}px)`);
    return await page
      .locator("a[href]")
      .evaluateAll((anchors) =>
        anchors
          .map((anchor) => anchor.href)
          .filter((href) => new URL(href).origin === location.origin),
      );
  } finally {
    await context.close();
  }
}

// Each check opens its own browser context, so two run side by side in one
// browser. Four crashed a renderer inside the Job's 1536 MiB limit (axe on the
// heavier pages); two keeps memory in bounds and each stage near 2 minutes.
export const TIER2_CONCURRENCY = 2;

export async function tier2(pages, concurrency = TIER2_CONCURRENCY) {
  const browser = await launchBrowser();
  const checks = pages.flatMap((page) =>
    ["light", "dark"].flatMap((colorScheme) =>
      [390, 1280].map((width) => ({ ...page, colorScheme, width })),
    ),
  );
  const links = [];
  let next = 0;
  const worker = async () => {
    while (next < checks.length) {
      const check = checks[next++];
      links.push(...(await checkPage(browser, check)));
      console.log(
        `tier 2 ${check.path}: ${check.colorScheme}, ${check.width}px passed`,
      );
    }
  };
  try {
    await Promise.all(
      Array.from({ length: Math.min(concurrency, checks.length) }, worker),
    );
  } finally {
    await browser.close();
  }
  return links;
}
