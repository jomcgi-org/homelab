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
    page.on("requestfailed", (request) =>
      errors.push(
        `request failed: ${request.url()} ${request.failure()?.errorText}`,
      ),
    );
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
      ...(await page.evaluate(() =>
        globalThis.siteCheckCsp.map((value) => `CSP: ${value}`),
      )),
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

export async function tier2(pages) {
  const browser = await launchBrowser();
  const links = [];
  try {
    for (const page of pages) {
      for (const colorScheme of ["light", "dark"]) {
        for (const width of [390, 1280]) {
          links.push(
            ...(await checkPage(browser, { ...page, colorScheme, width })),
          );
          console.log(`tier 2 ${page.path}: ${colorScheme}, ${width}px passed`);
        }
      }
    }
  } finally {
    await browser.close();
  }
  return links;
}
