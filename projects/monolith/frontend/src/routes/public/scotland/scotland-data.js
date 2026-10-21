import { PUBLIC_BASE } from "$lib/public/seo.js";

// The connector itself is a separate service (projects/scotland-connector)
// that owns /scotland/mcp and /scotland/v1/*; this page is the human front
// door at /scotland and only links to it.
export const BASE = `${PUBLIC_BASE}/scotland`;
export const MCP_URL = `${BASE}/mcp`;
export const SPEC_URL = `${BASE}/v1/openapi.json`;
export const METHOD_URL = `${BASE}/v1/method`;
export const MARKETPLACE_URL = `${BASE}/marketplace.json`;

// The Claude Code plugin lives in the homelab monorepo. Served as a file at
// MARKETPLACE_URL, the catalog lets `/plugin marketplace add` fetch it alone,
// and the entry's git-subdir source checks out only the plugin folder.
export const marketplace = {
  name: "jomcgi",
  owner: {
    name: "Joe McGinley",
    email: "joe@jomcgi.dev",
    url: PUBLIC_BASE,
  },
  description: "Plugins from jomcgi.dev.",
  plugins: [
    {
      name: "scotland",
      source: {
        source: "git-subdir",
        url: "https://github.com/jomcgi-org/homelab.git",
        path: "projects/scotland-connector/plugin",
        ref: "main",
      },
      description:
        "Plan and check Scottish walks and stargazing with forecast verdicts.",
      category: "travel",
    },
  ],
};

export const facts = [
  { k: "MCP", v: MCP_URL, code: true },
  { k: "Tools", v: "plan, check", code: true },
  { k: "Covers", v: "Scotland, mainland and islands" },
  { k: "Forecast", v: "MET Norway, refreshed every two to three hours" },
  {
    k: "Per call",
    v: "a plan of up to 7 days, or a check of up to 10 walks and nights",
  },
  { k: "Cost", v: "free, no sign-up or key" },
];

// Directory listings and plugins (#7255). A null href renders as "soon";
// set it when the listing goes live.
export const listings = [
  { label: "Claude directory", href: null },
  { label: "Codex plugin", href: null },
];

export const setups = [
  {
    id: "claude",
    label: "Claude",
    how: "Settings, Connectors, Add custom connector, then paste the MCP URL. On Team and Enterprise plans an owner adds it first, in the organisation's connector settings.",
  },
  {
    id: "claude-code",
    label: "Claude Code",
    how: "Install the plugin: the server plus a skill on when to use plan and check. Or add the server alone with the last line.",
    lang: "sh",
    code: `claude plugin marketplace add ${MARKETPLACE_URL}\nclaude plugin install scotland@jomcgi\n\nclaude mcp add --transport http scotland ${MCP_URL}`,
  },
  {
    id: "codex",
    label: "Codex",
    how: "In ~/.codex/config.toml:",
    lang: "toml",
    code: `[mcp_servers.scotland]\nurl = "${MCP_URL}"`,
  },
];

export const curl = `curl '${BASE}/v1/plan?lat=57.19&lon=-3.83'`;

export const sources = [
  { name: "MET Norway", use: "cloud forecast", licence: "CC BY 4.0" },
  {
    name: "Copernicus CERRA",
    use: "clear-sky odds by month",
    licence: "Copernicus licence",
  },
  { name: "OpenStreetMap", use: "road access", licence: "ODbL" },
  { name: "Natural Earth", use: "coastline", licence: "public domain" },
  {
    name: "Walkhighlands",
    use: "walk listings",
    licence: "routes stay on Walkhighlands; each walk links to its page",
  },
];
