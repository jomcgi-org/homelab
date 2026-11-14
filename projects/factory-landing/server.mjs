// Static files and a fixed, credential-free connector status proxy.
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";

const types = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".woff2": "font/woff2",
};
export const headers = {
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "Cache-Control": "no-store",
  "Cloudflare-CDN-Cache-Control": "no-store",
  // The web app imports a built Astryx theme as external CSS; csp.test.tsx
  // guards against introducing runtime style injection.
  "Content-Security-Policy":
    "default-src 'none'; script-src 'self'; style-src 'self'; " +
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; " +
    "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
};

export const STATUS_TIMEOUT_MS = 2000;

export function createLandingServer({
  root = resolve("build/static"),
  statusURL = process.env.CONNECTOR_STATUS_URL,
  fetchStatus = fetch,
  timeout = STATUS_TIMEOUT_MS,
} = {}) {
  return createServer(async (request, response) => {
    try {
      const pathname = decodeURIComponent(
        new URL(request.url, "http://static").pathname,
      );
      if (pathname === "/api/status") {
        if (request.method !== "GET") {
          response.writeHead(405, { ...headers, Allow: "GET" }).end();
          return;
        }
        try {
          if (!statusURL) throw new Error("Status endpoint not configured");
          const upstream = await fetchStatus(statusURL, {
            signal: AbortSignal.timeout(timeout),
            redirect: "error",
            headers: { Accept: "application/json" },
          });
          if (!upstream.ok) {
            await upstream.body?.cancel();
            throw new Error("Status unavailable");
          }
          // Bound the body as well as the request duration. No visitor headers
          // or cookies are sent upstream, and no upstream headers are relayed.
          const chunks = [];
          let bytes = 0;
          for await (const chunk of upstream.body) {
            bytes += chunk.length;
            if (bytes > 1024 * 1024) throw new Error("Status too large");
            chunks.push(chunk);
          }
          const body = Buffer.concat(chunks).toString("utf8");
          JSON.parse(body);
          response.writeHead(200, {
            ...headers,
            "Content-Type": "application/json; charset=utf-8",
          }).end(body);
        } catch {
          response.writeHead(503, {
            ...headers,
            "Content-Type": "application/json; charset=utf-8",
          }).end(JSON.stringify({ error: "Status unavailable" }));
        }
        return;
      }
      if (request.method !== "GET" && request.method !== "HEAD") {
        response.writeHead(405, { ...headers, Allow: "GET, HEAD" }).end();
        return;
      }
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
  });
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  createLandingServer().listen(8080, "0.0.0.0");
}
