// The private keypad: every tool on private.jomcgi.dev, plus the three
// outside services Joe logs into, as one row of keys on the dashboard.
//
// Internal hrefs are browser paths on the private host. External keys open
// in a new tab. The public /app/* apps live on the apex and are not here.
// ArgoCD has no hub UI route and Longhorn is retired (#7051); SigNoz left
// with its chart (#5362) and telemetry is Honeycomb.
//
// `conductor` carries a static fallback; the dashboard swaps in the board's
// own composed link (model and opening prompt) when the factory answers.
export const launcher = [
  { label: "factory", href: "/factory" },
  { label: "decisions", href: "/factory/escalations", count: "waiting" },
  { label: "work", href: "/factory/execution" },
  {
    label: "conductor",
    href: "/factory/execution?compose=1",
    dynamic: "conductor",
  },
  { label: "ask the cluster", href: "#ask" },
  { label: "knowledge", href: "/chat" },
  { label: "review", href: "/review" },
  { label: "notes", href: "/notes" },
  { label: "updates", href: "/updates" },
  { label: "qwen", href: "/qwen" },
  { label: "grimoire", href: "/grimoire" },
  {
    label: "honeycomb",
    href: "https://ui.honeycomb.io/jomcgi-75/environments/homelab",
    external: true,
  },
  { label: "buildbuddy", href: "https://jomcgi.buildbuddy.io", external: true },
  {
    label: "github",
    href: "https://github.com/jomcgi-org/homelab",
    external: true,
  },
];
