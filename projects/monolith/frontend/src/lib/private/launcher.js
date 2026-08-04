// Private launcher registry for the dashboard's launcher strip.
//
// Scope: internal cluster-ops tools and repos Joe logs into and uses, not the
// public /app/* apps (those live on the apex and are reachable at jomcgi.dev).
//
// Internal hrefs are browser paths on private.jomcgi.dev. The gateway-proxied
// UIs (argocd/signoz/longhorn) live under /app/<name> (Envoy strips the prefix
// and forwards to the real pod); /perf is a first-party route served by the
// monolith itself. External tools open in a new tab.
export const launcher = [
  { label: "ArgoCD", desc: "GitOps deploys", href: "/app/argocd" },
  { label: "SigNoz", desc: "logs, traces, metrics", href: "/app/signoz" },
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
