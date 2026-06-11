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
};

// The bare apex (jomcgi.dev, no subdomain) serves the public tier: it is the
// primary public hostname. Everything on it is rewritten under /public, which
// also keeps /private unreachable from the apex (a request to
// jomcgi.dev/private/x reroutes to /public/private/x and 404s). www and any
// other alias should 301 to the apex at Cloudflare, so only the bare apex is
// handled here.
const APEX_HOST = "jomcgi.dev";
const APEX_PREFIX = "/public";

// Top-level routes that intentionally live outside /public and /private.
// The browser OTEL exporter posts to same-origin /otel/v1/traces and the
// handler proxies to the cluster-internal SigNoz collector, so it must
// not be swept under a subdomain prefix.
const PASSTHROUGH_PREFIXES = ["/otel/"];

/** @type {import('@sveltejs/kit').Reroute} */
export function reroute({ url }) {
  if (PASSTHROUGH_PREFIXES.some((p) => url.pathname.startsWith(p))) {
    return;
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
    url.hostname === APEX_HOST &&
    !url.pathname.startsWith(`${APEX_PREFIX}/`)
  ) {
    return `${APEX_PREFIX}${url.pathname}`;
  }
}
