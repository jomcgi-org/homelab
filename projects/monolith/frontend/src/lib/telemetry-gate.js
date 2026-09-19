// Browser OTLP export is same-origin: the exporter posts to
// <origin>/otel/v1/traces and the SvelteKit route there proxies to the
// cluster-internal collector. That route is served publicly on the public
// tiers, but private.jomcgi.dev sits behind the Cloudflare Access application
// "private" (see docs/security.md), which answers every unauthenticated
// request on the host at the edge with a redirect to the Access login. A
// same-origin export from that tier is therefore bounced before it can reach
// the app and can never carry a span; measured over the 24h to
// 2026-09-19T00:40Z, private.jomcgi.dev/otel/v1/traces drew 963 requests, every
// one edgeResponseStatus 302 with originResponseStatus 0.
//
// So the exporter runs only where the edge serves /otel to the app. An empty or
// missing hostname fails closed: nothing is exported unless the tier is known.
const EDGE_GATED_HOST_PREFIXES = ["private."];

export function browserTelemetryEnabled(hostname) {
  const host = String(hostname ?? "").toLowerCase();
  if (!host) return false;
  return !EDGE_GATED_HOST_PREFIXES.some((prefix) => host.startsWith(prefix));
}
