// Private launcher registry for the dashboard's launcher strip.
//
// Scope: internal cluster-ops tools and repos Joe logs into and uses, not the
// public /app/* apps (those live on the apex and are reachable at jomcgi.dev).
//
// Internal hrefs are browser paths on private.jomcgi.dev. The gateway-proxied
// UIs (argocd/longhorn) live under /app/<name> (Envoy strips the prefix
// and forwards to the real pod); /perf is a first-party route served by the
// monolith itself. External tools open in a new tab.
//
// Telemetry now lives in Honeycomb, which is external and so opens in a new
// tab rather than sitting under /app/. The SigNoz tile was removed with SigNoz
// itself (#5362); its /app/signoz HTTPRoute went with the chart, so the tile
// was left pointing at a route that no longer resolves.
export const launcher = [
  { label: "ArgoCD", desc: "GitOps deploys", href: "/app/argocd" },
  {
    label: "Honeycomb",
    desc: "traces and probes",
    href: "https://ui.honeycomb.io/jomcgi-75/environments/homelab",
    external: true,
  },
  { label: "Longhorn", desc: "cluster storage", href: "/app/longhorn" },
  { label: "Perf", desc: "semgrep scan perf", href: "/perf" },
  { label: "Agents", desc: "agent sessions", href: "/agents" },
  {
    label: "BuildBuddy",
    desc: "CI",
    href: "https://jomcgi.buildbuddy.io",
    external: true,
  },
  {
    label: "GitHub",
    desc: "jomcgi/homelab",
    href: "https://github.com/jomcgi/homelab",
    external: true,
  },
];
