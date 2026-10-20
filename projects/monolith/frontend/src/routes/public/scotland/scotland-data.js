import { PUBLIC_BASE } from "$lib/public/seo.js";

// The connector itself is a separate service (projects/scotland-connector)
// that owns /scotland/mcp and /scotland/v1/*; this page is the human front
// door at /scotland and only links to it.
export const BASE = `${PUBLIC_BASE}/scotland`;
export const MCP_URL = `${BASE}/mcp`;
export const SPEC_URL = `${BASE}/v1/openapi.json`;
export const METHOD_URL = `${BASE}/v1/method`;

export const facts = [
  { k: "MCP", v: MCP_URL, code: true },
  { k: "Tools", v: "dark_sky_windows, walk_windows", code: true },
  { k: "Covers", v: "Scotland, mainland and islands" },
  { k: "Forecast", v: "MET Norway, refreshed every two to three hours" },
  { k: "Per call", v: "up to 10 results, up to 7 days ahead" },
  { k: "Cost", v: "free, no sign-up or key" },
];

// Directory listings and plugins (#7255). A null href renders as "soon";
// set it when the listing goes live.
export const listings = [
  { label: "Claude directory", href: null },
  { label: "Claude Code plugin", href: null },
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
    lang: "sh",
    code: `claude mcp add --transport http scotland ${MCP_URL}`,
  },
  {
    id: "codex",
    label: "Codex",
    how: "In ~/.codex/config.toml:",
    lang: "toml",
    code: `[mcp_servers.scotland]\nurl = "${MCP_URL}"`,
  },
];

export const curl = `curl '${BASE}/v1/dark-sky-windows?lat=57.19&lon=-3.83&nights=3'`;

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
    use: "walks, linked to their route pages",
    licence: "linked, not reproduced",
  },
];
