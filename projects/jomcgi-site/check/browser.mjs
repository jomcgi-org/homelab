import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { tmpdir } from "node:os";
import chrome, { inflate } from "@sparticuz/chromium";
import axe from "axe-core";
import { chromium } from "playwright-core";

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
    args: ["--enable-unsafe-swiftshader"],
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
  { url, status = 200, colorScheme = "light", width = 1280, checkMap = false },
) {
  const origin = new URL(url).origin;
  const context = await browser.newContext({
    colorScheme,
    viewport: { width, height: 900 },
    serviceWorkers: "block",
    bypassCSP: false,
  });
  const errors = [];
  const tileOrigin = "https://tiles.openfreemap.org";
  function isVectorTile(response) {
    const target = new URL(response.url());
    return (
      target.origin === tileOrigin &&
      /\/\d+\/\d+\/\d+\.pbf$/.test(target.pathname) &&
      response.status() === 200
    );
  }
  let mapActive = false;
  let tiles = 0;
  try {
    await context.route("**/*", (route) => {
      const requestOrigin = new URL(route.request().url()).origin;
      if (requestOrigin !== origin) {
        if (mapActive && requestOrigin === tileOrigin) return route.fallback();
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
        status === 404 &&
        message.location().url === url
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
      if (mapActive && isVectorTile(response)) tiles++;
      if (
        response.status() >= 400 &&
        !(response.url() === url && response.status() === status)
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
    const links = await page
      .locator("a[href]")
      .evaluateAll((anchors) =>
        anchors
          .map((anchor) => anchor.href)
          .filter((href) => new URL(href).origin === location.origin),
      );
    const path = new URL(url).pathname;
    if (checkMap && ["/stars", "/hikes"].includes(path)) {
      const button = (name) => page.getByRole("button", { name, exact: true });
      if (path === "/hikes") await button("Explore walks").click();
      mapActive = true;
      const firstTile = page
        .waitForEvent("response", {
          predicate: isVectorTile,
          timeout: 15000,
        })
        .catch(() => undefined);
      await button(
        path === "/stars" ? "Load interactive map" : "Show map",
      ).click();
      await page
        .locator("canvas.maplibregl-canvas")
        .waitFor({ state: "visible" });
      // The canvas can appear before the worker requests its first tile.
      await firstTile;
      await page.waitForLoadState("networkidle");
      console.log(`tier 2 ${path}: ${tiles} vector tile responses (HTTP 200)`);
      if (!tiles) errors.push("no successful vector tile response");
      if (
        /the map could not load|couldn’t load the map/i.test(
          await page.locator("body").innerText(),
        )
      )
        errors.push("map error text displayed");
    }
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
    return links;
  } finally {
    await context.close();
  }
}

// Each check opens its own browser context, so two run side by side in one
// browser. Four crashed a renderer inside the Job's 1536 MiB limit (axe on the
// heavier pages); two keeps memory in bounds and each stage near 2 minutes.
export const TIER2_CONCURRENCY = 2;

export async function tier2(
  pages,
  { concurrency = TIER2_CONCURRENCY, checkMaps = true } = {},
) {
  const browser = await launchBrowser();
  const checks = pages.flatMap((page) =>
    ["light", "dark"].flatMap((colorScheme) =>
      [390, 1280].map((width) => ({
        ...page,
        colorScheme,
        width,
        checkMap: checkMaps && colorScheme === "light" && width === 1280,
      })),
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
