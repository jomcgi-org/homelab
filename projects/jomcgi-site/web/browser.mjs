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
      if (csp) res.setHeader("Content-Security-Policy", policy);
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
    await page.setRequestInterception(true);
    page.on("request", (request) => {
      const url = request.url();
      if (url === "https://static.cloudflareinsights.com/beacon.min.js") {
        void request.respond({ contentType: "text/javascript", body: "" });
      } else if (!url.startsWith(origin + "/") && !url.startsWith("data:")) {
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
