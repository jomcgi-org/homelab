import { createHash } from "node:crypto";
import {
  chmodSync,
  cpSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { extname, join } from "node:path";
import { compressed, serve } from "./fixture.mjs";
import { scopeFor } from "./scope.mjs";
import { snapshot } from "../data/snapshot.mjs";
import { publishHikesSummary } from "../data/hikes-page.mjs";

const sources = JSON.parse(
  readFileSync(new URL("../data/sources.json", import.meta.url)),
);

// The Worker's asset server applies dist/_redirects before it serves a file:
// a 200 rule rewrites to its target, any other status redirects. A rule's
// trailing `/*` matches the prefix. The first matching rule wins, once.
export function redirectRules(dist) {
  let text;
  try {
    text = readFileSync(join(dist, "_redirects"), "utf8");
  } catch {
    return [];
  }
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"))
    .map((line) => {
      const [from, to, status = "302"] = line.split(/\s+/);
      return { from, to, status: Number(status) };
    });
}

export function matchRedirect(rules, path) {
  return rules.find((rule) =>
    rule.from.endsWith("/*")
      ? path.startsWith(rule.from.slice(0, -1))
      : path === rule.from,
  );
}

// One walk in the real snapshot format, so the hikes summary publishes.
const FIXTURE_WALKS = JSON.stringify({
  schema_version: 2,
  fetched_at: "2020-11-17T12:00:00Z",
  walks: [
    {
      id: "ben",
      name: "Ben Vorlich from Ardlui",
      url: "https://www.walkhighlands.co.uk/lochlomond/ben-vorlich-ardlui.shtml",
      duration_h: 5.5,
      forecast_fetched_at: "2020-11-17T12:00:00Z",
      distance_km: 11,
      ascent_m: 920,
      hours: [],
    },
  ],
});

const fixtureTrip = {
  slug: "fixture-trip",
  title: "Fixture trip",
  tz: "UTC",
  days: {},
  stats: {},
  highlights: [],
};
const objects = new Map([["walks.json", FIXTURE_WALKS]]);
function tripObject(name, value) {
  const body = JSON.stringify(value);
  const key = `trips/v/${name}-${createHash("sha256").update(body).digest("hex")}.json`;
  objects.set(key, body);
  return key;
}
objects.set(
  "trips/export.json",
  JSON.stringify({
    asOf: "2020-11-17T12:00:00.000Z",
    refreshSeconds: 86400,
    photoMode: "snapshot",
    photoBase: "/data/trips/photos",
    photoCount: 0,
    photoAssetCount: 0,
    photoBytes: 0,
    photoMaxBytes: 0,
    images: [],
    files: {
      index: tripObject("index", { count: 1, trips: [fixtureTrip] }),
      trips: {
        [fixtureTrip.slug]: tripObject("trip/" + fixtureTrip.slug, {
          trip: fixtureTrip,
          points: [
            {
              id: "1",
              taken_at: "2020-11-17T12:00:00Z",
              lat: 58,
              lng: -125,
              elevation: 100,
              tags: [],
            },
          ],
        }),
      },
    },
  }),
);
export async function fixtureObject(bucket, key) {
  const body = objects.get(key);
  if (!body && key.startsWith("trips/"))
    throw new Error(`Missing fixture object: ${bucket}/${key}`);
  return {
    body: Buffer.from(body ?? "{}"),
    lastModified: "Tue, 17 Nov 2020 12:00:00 GMT",
  };
}

// Exercise the built release, including the production hub/Worker boundary.
export async function serveRelease(dist, stage, { contrast = false } = {}) {
  const scope = scopeFor(stage);
  const rules = redirectRules(dist);
  // Like run.sh: copy the release, then snapshot its data into the copy, which
  // also stamps the stars summary into stars.html (HOME-333).
  const dataRoot = mkdtempSync(join(tmpdir(), "site-check-site-"));
  try {
    cpSync(dist, dataRoot, { recursive: true, dereference: true });
    // Bazel outputs are read-only; the snapshot writes into the copy.
    chmodSync(dataRoot, 0o755);
    for (const entry of readdirSync(dataRoot, { recursive: true })) {
      const path = join(dataRoot, entry);
      chmodSync(path, statSync(path).isDirectory() ? 0o755 : 0o644);
    }
    // Use the real snapshot format with fixed R2 objects, without credentials.
    await snapshot(sources, fixtureObject, join(dataRoot, "data"));
    // Like data/main.mjs: stamp the hikes summary after the snapshot.
    await publishHikesSummary(join(dataRoot, "data"));
    const server = await serve((req, res) => {
      const url = new URL(req.url, "http://fixture");
      let path = url.pathname;
      if (!scope.owns(path + url.search)) {
        res.writeHead(200, { "content-type": "text/html" });
        res.end("<h1>Existing hub page</h1>");
        return;
      }
      const rule = matchRedirect(rules, path);
      if (rule && rule.status !== 200) {
        res.writeHead(rule.status, { location: rule.to });
        res.end();
        return;
      }
      if (rule) path = rule.to;
      let body;
      let type;
      try {
        const file = path.endsWith("/")
          ? `${path}index.html`
          : extname(path)
            ? path
            : `${path}.html`;
        body = readFileSync(join(dataRoot, file));
        type =
          {
            ".html": "text/html",
            ".js": "text/javascript",
            ".css": "text/css",
            ".json": "application/json",
          }[extname(file)] ?? "application/octet-stream";
        if (contrast && extname(file) === ".css") {
          body = Buffer.concat([
            body,
            Buffer.from(
              "\n* { color: lightgray !important; background: white !important; }",
            ),
          ]);
        }
      } catch {
        body = readFileSync(join(dataRoot, "404.html"));
        type = "text/html";
        res.statusCode = 404;
      }
      compressed(res, body, type, res.statusCode, stage === "dev");
    });
    return {
      ...server,
      async close() {
        try {
          await server.close();
        } finally {
          rmSync(dataRoot, { recursive: true, force: true });
        }
      },
    };
  } catch (error) {
    rmSync(dataRoot, { recursive: true, force: true });
    throw error;
  }
}
