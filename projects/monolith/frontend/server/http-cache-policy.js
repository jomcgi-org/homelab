import { requiresNoStore } from "./response-cache-policy.js";

const policyHeaders = new Set([
  "cache-control",
  "cloudflare-cdn-cache-control",
  "cdn-cache-control",
  "etag",
]);

function entries(headers) {
  if (!Array.isArray(headers)) return Object.entries(headers || {});
  const pairs = [];
  for (let i = 0; i < headers.length; i += 2)
    pairs.push([headers[i], headers[i + 1]]);
  return pairs;
}

function webHeaders(pairs) {
  const headers = new Headers();
  for (const [name, value] of pairs) {
    for (const part of Array.isArray(value) ? value : [value]) {
      if (part !== undefined) headers.append(name, String(part));
    }
  }
  return headers;
}

export function protectResponseCache(request, response) {
  const writeHead = response.writeHead;
  response.writeHead = function (...args) {
    const index = typeof args[1] === "string" ? 2 : 1;
    const supplied = args[index];
    // writeHead's explicit headers override preceding setHeader calls. Inspect
    // both sources conservatively so a restrictive policy cannot be overwritten.
    const headers = webHeaders([
      ...Object.entries(this.getHeaders()),
      ...entries(supplied),
    ]);
    let noStore = true;
    try {
      const origin = `http://${request.headers.host || "localhost"}`;
      const { pathname } = new URL(request.url || "/", origin);
      noStore = requiresNoStore(
        {
          route: {
            id: /^\/(?:public|private|friends)(?:\/|$)/.test(pathname)
              ? pathname
              : `/public${pathname}`,
          },
          request: {
            method: request.method,
            // An absolute-form request target cannot hide a private Host.
            url: origin,
            headers: webHeaders(Object.entries(request.headers)),
          },
        },
        { status: args[0], headers },
      );
    } catch {
      // Malformed request metadata must not make an early error cacheable.
    }
    if (noStore) {
      for (const name of policyHeaders) this.removeHeader(name);
      this.setHeader("cache-control", "no-store");
      this.setHeader("cloudflare-cdn-cache-control", "no-store");
      if (headers.has("cdn-cache-control"))
        this.setHeader("cdn-cache-control", "no-store");
      if (supplied) {
        const safe = entries(supplied).filter(
          ([name]) => !policyHeaders.has(name.toLowerCase()),
        );
        args[index] = Array.isArray(supplied)
          ? safe.flat()
          : Object.fromEntries(safe);
      }
    }
    return writeHead.apply(this, args);
  };
}
