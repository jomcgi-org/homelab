// Offline browser shared by diagram generation and built-site acceptance tests.
import chromium, { inflate } from "@sparticuz/chromium";
import puppeteer from "puppeteer-core";
import { once } from "node:events";
import { readFile, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

export async function withSiteBrowser(
  { renderer, csp = true, scripts = true, assets = {} },
  check,
) {
  const headers = await readFile("public/_headers", "utf8");
  const policy = headers.match(/Content-Security-Policy: (.+)/)[1];
  // A route with its own policy (the map pages) is served with that policy,
  // as the host does. Maps load on arrival, so the site policy would block
  // them here but not in production.
  const routePolicies = new Map(
    [
      ...headers.matchAll(
        /^(\/[a-z-]+)\n(?:  .*\n)*?  Content-Security-Policy: (.+)$/gm,
      ),
    ].map(([, route, value]) => [route, value]),
  );
  const policyFor = (path) =>
    routePolicies.get("/" + path.split("/")[1].replace(/\.html$/, "")) ??
    policy;
  const server = createServer(async (req, res) => {
    try {
      const path = new URL(req.url, "http://localhost").pathname;
      const isRenderer = renderer && path.startsWith("/_renderer/");
      const root = resolve(isRenderer ? renderer : "dist");
      let relative = isRenderer ? path.slice(10) : path;
      if (relative === "/") relative = "/index.html";
      else if (!extname(relative)) relative += ".html";
      const file = assets[path] ?? resolve(root, "." + relative);
      if (!assets[path] && !file.startsWith(root + "/"))
        throw new Error("invalid asset path");
      res.setHeader(
        "Content-Type",
        { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" }[
          extname(file)
        ] ?? "application/octet-stream",
      );
      if (csp) res.setHeader("Content-Security-Policy", policyFor(path));
      res.end(await readFile(file));
    } catch {
      res.writeHead(404).end();
    }
  });
  let browser;
  try {
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    const origin = `http://127.0.0.1:${server.address().port}`;
    const executablePath =
      process.env.CHROMIUM_PATH ?? (await chromium.executablePath());
    const env = { ...process.env };
    if (!process.env.CHROMIUM_PATH) {
      // Supply the browser's pinned native libraries and fonts even on a
      // minimal Linux build worker. No system browser or download is needed.
      await inflate(
        fileURLToPath(
          new URL(
            "../bin/al2023.tar.br",
            import.meta.resolve("@sparticuz/chromium"),
          ),
        ),
      );
      env.LD_LIBRARY_PATH = [join(tmpdir(), "al2023/lib"), env.LD_LIBRARY_PATH]
        .filter(Boolean)
        .join(":");
    }
    // The packaged fonts.conf hard-codes /tmp/fonts. Bazel sets TMPDIR,
    // so point fontconfig at the actual extraction directory instead.
    const fonts = await inflate(
      fileURLToPath(
        new URL(
          "../bin/fonts.tar.br",
          import.meta.resolve("@sparticuz/chromium"),
        ),
      ),
    );
    const xml = (value) =>
      value.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
    env.FONTCONFIG_FILE = join(tmpdir(), "docs-fonts.conf");
    await writeFile(
      env.FONTCONFIG_FILE,
      `<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig><dir>${xml(fonts)}</dir><cachedir>${xml(join(tmpdir(), "fonts-cache"))}</cachedir></fontconfig>`,
    );
    browser = await puppeteer.launch({
      executablePath,
      env,
      args: ["--no-sandbox", "--disable-dev-shm-usage"],
      headless: "shell",
    });
    const page = await browser.newPage();
    const unexpected = [];
    // Only a map page may reach the tile host or start a blob worker. On any
    // other page those requests stay unexpected, as its policy forbids them.
    const mapPage = () =>
      /^\/(stars|hikes|trips)(\.html|\/|$)/.test(new URL(page.url()).pathname);
    await page.setRequestInterception(true);
    page.on("request", (request) => {
      const url = request.url();
      if (url === "https://static.cloudflareinsights.com/beacon.min.js") {
        void request.respond({ contentType: "text/javascript", body: "" });
      } else if (
        mapPage() &&
        url.startsWith("https://tiles.openfreemap.org/")
      ) {
        // The run is offline. On a map page the tile host answers "not
        // found", and the page must stay usable without a base map.
        void request.respond({ status: 404, body: "" });
      } else if (
        !url.startsWith(origin + "/") &&
        !url.startsWith("data:") &&
        !(mapPage() && url.startsWith("blob:"))
      ) {
        unexpected.push(url);
        void request.abort();
      } else if (
        !scripts &&
        request.resourceType() === "script" &&
        !url.startsWith(origin + "/_renderer/")
      ) {
        void request.respond({
          status: 200,
          contentType: "text/javascript",
          body: "",
        });
      } else void request.continue();
    });
    await check(page, origin);
    if (unexpected.length)
      throw new Error(`Non-local browser requests: ${unexpected.join(", ")}`);
  } finally {
    await browser?.close();
    await new Promise((resolve) => server.close(resolve));
  }
}
