// Private launcher registry for the dashboard's launcher strip.
//
// Internal hrefs are browser paths on private.jomcgi.dev (hooks.js reroutes
// them under /private internally, so no /private prefix here). Note that
// /app/* on the private host is only gateway-routed for signoz/argocd/
// longhorn; the public tier's /app/* apps are NOT served there (the reroute
// would send /app/grimoire to /private/app/grimoire, which 404s), so the
// public apps are linked absolute to the apex and open in a new tab.
import { apps } from "$lib/public/apps.js";

export const launcher = [
  { label: "Notes", desc: "knowledge graph", href: "/notes" },
  { label: "Review", desc: "knowledge review queue", href: "/review" },
  { label: "Chat", desc: "knowledge graph explorer", href: "/chat" },
  { label: "SigNoz", desc: "logs, traces, metrics", href: "/app/signoz" },
  { label: "ArgoCD", desc: "GitOps deploys", href: "/app/argocd" },
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
  {
    label: "Docs",
    desc: "runbooks + ADRs",
    href: "https://jomcgi.dev/docs",
    external: true,
  },
  ...apps.map((a) => ({
    label: a.label,
    desc: a.desc,
    href: `https://jomcgi.dev${a.href}`,
    external: true,
  })),
];
