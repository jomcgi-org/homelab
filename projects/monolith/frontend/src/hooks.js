/**
 * Maps subdomain prefixes to SvelteKit route prefixes.
 * Requests to `<subdomain>.jomcgi.dev/foo` are rerouted internally
 * to `/<prefix>/foo` so SvelteKit's file-based router resolves them
 * from `src/routes/<prefix>/`.
 *
 * This replaces gateway-level ReplacePrefixMatch rewrites which have
 * inconsistent slash-joining behaviour across implementations.
 */
const DOMAIN_PREFIX_MAP = {
  "public.": "/public",
  "private.": "/private",
  "friends.": "/friends",
};

// The bare apex (jomcgi.dev, no subdomain) serves the public tier: it is the
// primary public hostname. Everything on it is rewritten under /public, which
// also keeps /private unreachable from the apex (a request to
// jomcgi.dev/private/x reroutes to /public/private/x and 404s). www and any
// other alias should 301 to the apex at Cloudflare, so only the bare apex is
// handled here.
const APEX_HOST = "jomcgi.dev";
const APEX_PREFIX = "/public";

// Local previews (adapter-node build or vite dev on a workstation) serve the
// public tier: without this every path 404s locally because nothing maps the
// un-prefixed browser path into /public. Prod never serves on these hosts.
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1"]);

// Top-level routes that intentionally live outside /public and /private.
// The browser OTEL exporter posts to same-origin /otel/v1/traces and the
// handler proxies to the cluster-internal telemetry collector, so it must
// not be swept under a subdomain prefix.
const PASSTHROUGH_PREFIXES = ["/otel/"];

// Browser chat paths the public app POSTs to same-origin. They live under
// /public/chat/* in the route tree, so map the short browser path the SvelteKit
// app uses (/chat/share, alongside /chat/session and /chat/message which the
// apex/public prefix already covers) explicitly. This keeps the rewrite obvious
// next to the others even though the apex/public prefixing below would also
// catch it on the apex host.
const CHAT_PREFIX_MAP = {
  "/chat/session": "/public/chat/session",
  "/chat/message": "/public/chat/message",
  "/chat/share": "/public/chat/share",
  "/chat/fork": "/public/chat/fork",
};

/** @type {import('@sveltejs/kit').Reroute} */
export function reroute({ url }) {
  if (PASSTHROUGH_PREFIXES.some((p) => url.pathname.startsWith(p))) {
    return;
  }
  // Same-origin chat BFF paths the public app POSTs to. Mapped explicitly (and
  // host-independently) so each browser path is obvious next to the others; the
  // apex/public prefix rule below would also catch these on the apex host, but
  // the explicit map keeps /chat/share co-located with /chat/session and
  // /chat/message.
  if (CHAT_PREFIX_MAP[url.pathname]) {
    return CHAT_PREFIX_MAP[url.pathname];
  }
  for (const [domain, prefix] of Object.entries(DOMAIN_PREFIX_MAP)) {
    if (
      url.hostname.startsWith(domain) &&
      !url.pathname.startsWith(`${prefix}/`)
    ) {
      return `${prefix}${url.pathname}`;
    }
  }
  if (
    (url.hostname === APEX_HOST || LOCAL_HOSTS.has(url.hostname)) &&
    !url.pathname.startsWith(`${APEX_PREFIX}/`)
  ) {
    return `${APEX_PREFIX}${url.pathname}`;
  }
}
