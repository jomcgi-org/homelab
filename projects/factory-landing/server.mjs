// Static files only. No API, credentials, proxying or directory listings.
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, resolve, sep } from "node:path";

const root = resolve("build/static");
const types = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".woff2": "font/woff2",
};
const headers = {
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "Cache-Control": "no-store",
  "Cloudflare-CDN-Cache-Control": "no-store",
  // The web app imports a built Astryx theme as external CSS; csp.test.tsx
  // guards against introducing runtime style injection.
  "Content-Security-Policy":
    "default-src 'none'; script-src 'self'; style-src 'self'; " +
    "img-src 'self' data:; font-src 'self'; connect-src 'none'; " +
    "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
};

createServer(async (request, response) => {
  if (request.method !== "GET" && request.method !== "HEAD") {
    response.writeHead(405, { ...headers, Allow: "GET, HEAD" }).end();
    return;
  }
  try {
    const pathname = decodeURIComponent(
      new URL(request.url, "http://static").pathname,
    );
    const file = resolve(
      root,
      `.${pathname === "/" ? "/index.html" : pathname}`,
    );
    if (!file.startsWith(root + sep) || !types[extname(file)]) {
      response.writeHead(404, headers).end();
      return;
    }
    const body = await readFile(file);
    response.writeHead(200, {
      ...headers,
      "Content-Type": types[extname(file)],
    });
    response.end(request.method === "HEAD" ? undefined : body);
  } catch {
    response.writeHead(404, headers).end();
  }
}).listen(8080, "0.0.0.0");
