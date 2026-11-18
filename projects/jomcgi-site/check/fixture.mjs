import { createServer } from "node:http";
import { once } from "node:events";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { gzipSync } from "node:zlib";

const headerFile = readFileSync(
  new URL("../web/public/_headers", import.meta.url),
  "utf8",
);
// Apply only the rules that match this request. Flattening the file would
// put the catalog cache policy on hashed assets and slop's robots on all pages.
const headerRules = [];
for (const line of headerFile.split("\n")) {
  if (!line.trim() || line.trimStart().startsWith("#")) continue;
  if (!line.startsWith(" ")) {
    const expression = line
      .split(/(\*|:[a-zA-Z]\w*)/)
      .map((token) =>
        token === "*"
          ? ".*"
          : token.startsWith(":")
            ? "[^/]+"
            : token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"),
      )
      .join("");
    headerRules.push({
      host: line.startsWith("https://"),
      match: new RegExp(`^${expression}$`),
      detach: [],
      headers: {},
    });
  } else {
    // `! Name` detaches a header that an earlier matching rule set, as the
    // Workers asset server does (for example the /hikes map policy).
    const detach = line.match(/^\s+!\s*([\w-]+)\s*$/);
    if (detach) {
      headerRules.at(-1).detach.push(detach[1].toLowerCase());
      continue;
    }
    const [, name, value] = line.match(/^\s+([\w-]+): (.+)$/);
    headerRules.at(-1).headers[name.toLowerCase()] = value;
  }
}

export function headersFor(path, preview = true) {
  const origin = preview ? "https://next.jomcgi.dev" : "https://jomcgi.dev";
  const url = new URL(path, origin);
  const applied = {};
  for (const rule of headerRules) {
    if (!rule.match.test(rule.host ? `${url.origin}${url.pathname}` : url.pathname))
      continue;
    for (const name of rule.detach) delete applied[name];
    Object.assign(applied, rule.headers);
  }
  return applied;
}

export const headers = headersFor("/");

export const html = `<!doctype html><html lang="en"><head>
<title>Site check fixture</title><meta name="viewport" content="width=device-width">
<link rel="stylesheet" href="/assets/style.css"></head>
<body><main><h1>Readable page</h1><p>Text needs contrast.</p><a href="/">Home</a></main>
<script src="/assets/app.js"></script></body></html>`;
export const css =
  "body { background: white; color: black; } a { color: navy; }";

export function isolateBrowserCache(t) {
  // Chromium treats an existing output as fully unpacked. Separate Node/Bazel
  // tests must not observe another process's partially extracted browser.
  const previous = process.env.TMPDIR;
  const root = mkdtempSync(
    join(process.env.TEST_TMPDIR ?? tmpdir(), "site-check-"),
  );
  process.env.TMPDIR = root;
  t.after(() => {
    if (previous === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = previous;
    rmSync(root, { recursive: true, force: true });
  });
}

export async function serve(handler) {
  const server = createServer(handler);
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return {
    origin: `http://127.0.0.1:${server.address().port}`,
    async close() {
      server.closeAllConnections();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

export function respond(
  res,
  body,
  type = "text/html",
  extra = {},
  status = 200,
  preview = true,
) {
  res.writeHead(status, {
    ...headersFor(res.req.url, preview),
    "content-type": type,
    ...extra,
  });
  res.end(body);
}

export function compressed(res, body, type, status = 200, preview = true) {
  // No Content-Length, so the client must measure the actual encoded stream.
  respond(
    res,
    gzipSync(body),
    type,
    { "content-encoding": "gzip" },
    status,
    preview,
  );
}
