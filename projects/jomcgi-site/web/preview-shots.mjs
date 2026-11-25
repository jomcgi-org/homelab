// Screenshots of a built site with the invented factory fixtures (HOME-877).
// Usage: node preview-shots.mjs --dist <dir> --fixtures <dir> --out <dir> [/page ...]
// Exit 0: every page loaded clean. 1: a page failed. 2: bad arguments.
// projects/jomcgi-site/preview/preview.sh builds the site and calls this.
import { existsSync, mkdirSync, mkdtempSync, readdirSync } from "node:fs";
import { readFileSync, realpathSync, rmSync, statSync } from "node:fs";
import { writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, extname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

export const DEFAULT_PAGES = [
  "/",
  "/blog",
  "/factory",
  "/factory/goals/reading-list",
  "/factory/issues/DEMO-101",
  "/factory/sessions/demo-build",
];
const VIEWPORTS = [
  { name: "phone", width: 390, height: 844 },
  { name: "desktop", width: 1280, height: 900 },
];
const SCHEMES = ["light", "dark"];
const BEACON = "https://static.cloudflareinsights.com/beacon.min.js";
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".webp": "image/webp",
  ".woff2": "font/woff2",
  ".txt": "text/plain",
  ".xml": "application/xml",
  ".webmanifest": "application/manifest+json",
};

class UsageError extends Error {}

export function parseArgs(argv) {
  const options = { pages: [] };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (["--dist", "--fixtures", "--out"].includes(arg)) {
      if (i + 1 >= argv.length) throw new UsageError(`${arg} needs a value`);
      options[arg.slice(2)] = argv[++i];
    } else if (arg.startsWith("-")) {
      throw new UsageError(`unknown option ${arg}`);
    } else {
      // A site path only: no host, no "..", no spaces or control characters.
      if (!/^\/(?!\/)[^\s\\]*$/.test(arg) || arg.split(/[/?#]/).includes(".."))
        throw new UsageError(`page must be a site path like /blog: ${arg}`);
      options.pages.push(arg);
    }
  }
  for (const name of ["dist", "fixtures", "out"])
    if (!options[name]) throw new UsageError(`--${name} is required`);
  if (!options.pages.length) options.pages = DEFAULT_PAGES;
  return options;
}

// Cloudflare's _headers: path blocks, "Name: value" sets, "! Name" detaches.
function headerRules(dist) {
  const file = join(dist, "_headers");
  if (!existsSync(file)) return [];
  const rules = [];
  for (const line of readFileSync(file, "utf8").split("\n")) {
    if (!line.trim() || line.trimStart().startsWith("#")) continue;
    if (!/^\s/.test(line)) rules.push({ pattern: line.trim(), lines: [] });
    else rules.at(-1)?.lines.push(line.trim());
  }
  return rules.filter((rule) => rule.pattern.startsWith("/"));
}

function matches(pattern, path) {
  const source = pattern
    .split(/(\*|:[a-z]+)/)
    .map((part) =>
      part === "*"
        ? ".*"
        : part.startsWith(":")
          ? "[^/]+"
          : part.replace(/[.+?^${}()|[\]\\]/g, "\\$&"),
    )
    .join("");
  return new RegExp(`^${source}$`).test(path);
}

function headersFor(rules, path) {
  const headers = {};
  for (const rule of rules.filter((rule) => matches(rule.pattern, path))) {
    for (const line of rule.lines) {
      if (line.startsWith("!")) delete headers[line.slice(1).trim()];
      else {
        const [name, ...value] = line.split(":");
        headers[name] = headers[name]
          ? `${headers[name]}, ${value.join(":").trim()}`
          : value.join(":").trim();
      }
    }
  }
  return headers;
}

// Cloudflare's _redirects: "from to status"; 200 rewrites, 3xx redirects.
function redirectRules(dist) {
  const file = join(dist, "_redirects");
  if (!existsSync(file)) return [];
  return readFileSync(file, "utf8")
    .split("\n")
    .map((line) => line.trim().split(/\s+/))
    .filter(([from]) => from?.startsWith("/"));
}

// Serves dist as the Worker's assets do (auto-trailing-slash, 404 page), and
// the fixtures under /data/factory/ as Vite does in development.
export function siteServer(dist, fixtures) {
  const headerList = headerRules(dist);
  const redirects = redirectRules(dist);
  const inside = (root, path) => {
    const file = resolve(root, "." + path);
    return file.startsWith(resolve(root) + sep) ? file : undefined;
  };
  const isFile = (file) => file && existsSync(file) && statSync(file).isFile();
  return createServer((request, response) => {
    let path;
    try {
      path = decodeURIComponent(new URL(request.url, "http://x").pathname);
    } catch {
      return response.writeHead(400).end();
    }
    for (const header of Object.entries(headersFor(headerList, path)))
      response.setHeader(...header);
    if (path.startsWith("/data/factory/")) {
      const name = path.slice("/data/factory/".length);
      const file = /^(manifest\.json|objects\/[a-f0-9]{64}\.json)$/.test(name)
        ? join(fixtures, name)
        : undefined;
      if (!isFile(file)) return response.writeHead(404).end();
      response.setHeader("Content-Type", TYPES[".json"]);
      return response.end(readFileSync(file));
    }
    for (const [from, to, status = "302"] of redirects) {
      if (!matches(from, path)) continue;
      const splat = from.endsWith("*") ? path.slice(from.length - 1) : "";
      const target = to.replace(":splat", splat);
      if (status === "200") path = target;
      else
        return response.writeHead(Number(status), { Location: target }).end();
      break;
    }
    const candidates = path.endsWith("/")
      ? [`${path}index.html`]
      : extname(path)
        ? [path]
        : [`${path}.html`, `${path}/index.html`];
    const hidden = path.split("/").some((part) => part.startsWith("."));
    const file = hidden
      ? undefined
      : candidates.map((c) => inside(dist, c)).find(isFile);
    if (!file) {
      const page = join(dist, "404.html");
      response.writeHead(404, { "Content-Type": TYPES[".html"] });
      return response.end(existsSync(page) ? readFileSync(page) : "");
    }
    response.setHeader(
      "Content-Type",
      TYPES[extname(file)] ?? "application/octet-stream",
    );
    response.end(readFileSync(file));
  });
}

// Chromium from @sparticuz/chromium, as in home-browser.mjs.
async function chromiumPath(scratch) {
  if (process.env.SITE_CHROMIUM_EXECUTABLE)
    return process.env.SITE_CHROMIUM_EXECUTABLE;
  process.env.TMPDIR = scratch;
  const { default: chrome, inflate } = await import("@sparticuz/chromium");
  const pkg = createRequire(import.meta.url).resolve("@sparticuz/chromium");
  await inflate(join(dirname(pkg), "../bin/al2023.tar.br"));
  const executablePath = await chrome.executablePath();
  const xml = (path) => path.replaceAll("&", "&amp;").replaceAll("<", "&lt;");
  const conf = `<fontconfig><dir>${xml(join(scratch, "fonts"))}</dir><cachedir>${xml(join(scratch, "cache"))}</cachedir></fontconfig>`;
  process.env.FONTCONFIG_FILE = join(scratch, "fonts.conf");
  writeFileSync(process.env.FONTCONFIG_FILE, conf);
  const libs = [join(scratch, "al2023/lib"), process.env.LD_LIBRARY_PATH];
  process.env.LD_LIBRARY_PATH = libs.filter(Boolean).join(":");
  return executablePath;
}

const slug = (page) =>
  page
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "") || "home";

// One page in one viewport and scheme. Returns its failures and notes.
async function shoot(browser, origin, page, viewport, colorScheme, file) {
  const failures = [];
  const notes = new Set();
  // Section data is not in a local build: those sections show their
  // unavailable states. The factory fixtures must all load.
  const missingData = (url) =>
    url.startsWith(`${origin}/data/`) &&
    !url.startsWith(`${origin}/data/factory/`);
  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
    colorScheme,
  });
  const tab = await context.newPage();
  // Stay offline: only loopback requests reach a server.
  await tab.route(
    (url) => url.origin !== origin,
    (route) => {
      const url = route.request().url();
      if (url === BEACON)
        return route.fulfill({ contentType: "text/javascript", body: "" });
      if (!url.startsWith("data:") && !url.startsWith("blob:"))
        notes.add(`offline: ${new URL(url).host}`);
      return route.fulfill({ status: 404, body: "" });
    },
  );
  tab.on("pageerror", (error) => failures.push(`page error: ${error.message}`));
  tab.on("console", (message) => {
    // The response and requestfailed handlers judge failed loads.
    if (message.type() !== "error") return;
    if (/^Failed to load resource/.test(message.text())) return;
    failures.push(`console error: ${message.text()}`);
  });
  const answered = new Set();
  tab.on("response", (response) => {
    const url = response.url();
    answered.add(url);
    if (!url.startsWith(`${origin}/`) || response.status() < 400) return;
    if (missingData(url)) notes.add(`no local data: ${new URL(url).pathname}`);
    else failures.push(`HTTP ${response.status()}: ${new URL(url).pathname}`);
  });
  tab.on("requestfailed", (request) => {
    // A response already judged can still fail while its body loads.
    if (request.url().startsWith(`${origin}/`) && !answered.has(request.url()))
      failures.push(`request failed: ${new URL(request.url()).pathname}`);
  });
  try {
    const response = await tab.goto(origin + page, {
      waitUntil: "networkidle",
      timeout: 30_000,
    });
    if (!response) failures.push("no response");
    await tab.evaluate(async () => {
      await document.fonts.ready;
      await new Promise(requestAnimationFrame);
    });
    await tab.screenshot({ path: file, fullPage: true });
  } catch (error) {
    failures.push(`load failed: ${error.message.split("\n")[0]}`);
  } finally {
    await context.close();
  }
  return { failures: [...new Set(failures)], notes: [...notes] };
}

const cell = (text) => String(text).replace(/[|\\]/g, "\\$&");

export function indexMarkdown(results) {
  const pages = [...new Set(results.map((r) => r.page))];
  const failed = pages.filter((page) =>
    results.some((r) => r.page === page && r.failures.length),
  );
  const lines = [
    "# jomcgi.dev preview",
    "",
    `Pages: ${pages.length}. Screenshots: ${results.length}. Pages that failed: ${failed.length}.`,
    "",
    "## Pages",
    "",
    "| Page | Result | Details |",
    "| --- | --- | --- |",
  ];
  for (const page of pages) {
    const runs = results.filter((r) => r.page === page);
    const failures = [...new Set(runs.flatMap((r) => r.failures))];
    const notes = [...new Set(runs.flatMap((r) => r.notes))];
    lines.push(
      `| \`${cell(page)}\` | ${failures.length ? "FAIL" : "ok"} | ${cell([...failures, ...notes].join("; "))} |`,
    );
  }
  lines.push(
    "",
    "## Screenshots",
    "",
    "| Page | Width | Scheme | File |",
    "| --- | --- | --- | --- |",
  );
  for (const r of results)
    lines.push(
      `| \`${cell(r.page)}\` | ${r.width} | ${r.scheme} | ${r.file} |`,
    );
  return lines.join("\n") + "\n";
}

async function main(argv) {
  let options;
  try {
    options = parseArgs(argv);
  } catch (error) {
    if (!(error instanceof UsageError)) throw error;
    console.error(`preview-shots: ${error.message}`);
    return 2;
  }
  const dist = resolve(options.dist);
  if (!existsSync(join(dist, "index.html"))) {
    console.error(`preview-shots: no built site in ${dist}`);
    return 2;
  }
  const out = resolve(options.out);
  mkdirSync(out, { recursive: true });
  // Remove only this tool's earlier files from the output directory.
  for (const name of readdirSync(out))
    if (name.endsWith(".png") || name === "index.md") rmSync(join(out, name));

  const server = siteServer(dist, resolve(options.fixtures));
  await new Promise((done) => server.listen(0, "127.0.0.1", done));
  const origin = `http://127.0.0.1:${server.address().port}`;
  console.log(`preview-shots: serving ${origin}`);
  const scratch = mkdtempSync(join(tmpdir(), "preview-shots-"));
  const { chromium } = await import("playwright");
  let browser;
  // Stop the browser and the server once, on every exit path. A signal
  // exits with its code after the stop.
  let stopping;
  const stop = () =>
    (stopping ??= (async () => {
      await browser?.close().catch(() => {});
      server.closeAllConnections();
      await new Promise((done) => server.close(done));
      rmSync(scratch, { recursive: true, force: true });
    })());
  for (const [signal, code] of [
    ["SIGINT", 130],
    ["SIGTERM", 143],
    ["SIGHUP", 129],
  ])
    process.once(signal, () => stop().finally(() => process.exit(code)));
  const results = [];
  try {
    browser = await chromium.launch({
      executablePath: await chromiumPath(scratch),
      args: ["--no-sandbox", "--disable-dev-shm-usage"],
      handleSIGINT: false,
      handleSIGTERM: false,
      handleSIGHUP: false,
    });
    for (const page of options.pages)
      for (const viewport of VIEWPORTS)
        for (const scheme of SCHEMES) {
          const file = `${slug(page)}-${viewport.name}-${scheme}.png`;
          const result = await shoot(
            browser,
            origin,
            page,
            viewport,
            scheme,
            join(out, file),
          );
          results.push({
            page,
            width: viewport.width,
            scheme,
            file,
            ...result,
          });
          const status = result.failures.length ? "FAIL" : "ok";
          console.log(`${status} ${page} ${viewport.width} ${scheme}`);
          for (const failure of result.failures) console.log(`  ${failure}`);
        }
  } finally {
    await stop();
  }
  writeFileSync(join(out, "index.md"), indexMarkdown(results));
  console.log(`preview-shots: ${join(out, "index.md")}`);
  return results.some((r) => r.failures.length) ? 1 : 0;
}

const entry = process.argv[1] && realpathSync(process.argv[1]);
if (entry === realpathSync(fileURLToPath(import.meta.url)))
  process.exitCode = await main(process.argv.slice(2));
