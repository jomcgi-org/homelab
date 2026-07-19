import http from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));

// Map a request pathname to a committed fixture file (pure; unit-tested).
const ROUTES = [
  ["/api/home/observability/stats", "fixtures/api/home_stats.json"],
  ["/api/dr-jobs/listings", "fixtures/api/dr_jobs_listings.json"],
  ["/api/wc2026/summary", "fixtures/api/wc2026_summary.json"],
  ["/api/hikes/walks", "fixtures/api/hikes_walks.json"],
  ["/api/ships/snapshot", "fixtures/api/ships_snapshot.json"],
  ["/api/stars/sites", "fixtures/api/stars_sites.json"],
  ["/api/trips/trips", "fixtures/api/trips_trips.json"],
  ["/api/knowledge/public/graph", "fixtures/api/knowledge_graph.json"],
  ["/api/grimoire/books", "fixtures/api/grimoire_books.json"],
  ["/api/grimoire/books/mm/read", "fixtures/api/grimoire_read.json"],
  ["/api/grimoire/books/mm/sections", "fixtures/api/grimoire_sections.json"],
  ["/api/grimoire/entities", "fixtures/api/grimoire_entities.json"],
  ["/api/grimoire/adventures", "fixtures/api/grimoire_adventures.json"],
  ["/api/grimoire/explore/ego", "fixtures/api/grimoire_explore_ego.json"],
  // /ember/postgres's proxies are all POST or GET against the same raw
  // /api/ember/postgres/* paths; this server ignores req.method entirely (see
  // startMock below), so one fixture per path covers both the page's GET
  // polls (status, savings) and its POST calls on mount (session, query).
  ["/api/ember/postgres/status", "fixtures/api/ember_postgres_status.json"],
  ["/api/ember/postgres/savings", "fixtures/api/ember_postgres_savings.json"],
  ["/api/ember/postgres/session", "fixtures/api/ember_postgres_session.json"],
  ["/api/ember/postgres/query", "fixtures/api/ember_postgres_query.json"],
  // /ember/bazel fires a fire-and-forget session mint plus an SSR savings
  // read on mount (no unprompted query, unlike postgres); the session and
  // savings fixtures are what capture actually exercises, the query fixture
  // is here for parity/coverage.
  ["/api/ember/bazel/session", "fixtures/api/ember_bazel_session.json"],
  ["/api/ember/bazel/query", "fixtures/api/ember_bazel_query.json"],
  ["/api/ember/bazel/savings", "fixtures/api/ember_bazel_savings.json"],
];
const PREFIX = [["/api/trips/trip/", "fixtures/api/trips_trip.json"]];

// Chunk image requests (book covers, entity art): /api/grimoire/chunks/:id/image.
// Matched separately from the JSON ROUTES/PREFIX tables since it serves a
// binary PNG, not a fixture file; reuses the same committed placeholder the
// basemap/imgproxy interception uses.
const CHUNK_IMAGE_RE = /^\/api\/grimoire\/chunks\/[^/]+\/image$/;

export function resolveFixture(pathname) {
  for (const [p, f] of ROUTES) if (pathname === p) return f;
  for (const [p, f] of PREFIX) if (pathname.startsWith(p)) return f;
  return null;
}

export function startMock(port) {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://localhost");
    if (CHUNK_IMAGE_RE.test(url.pathname)) {
      const body = readFileSync(join(HERE, "fixtures/basemap/placeholder.png"));
      res.writeHead(200, {
        "content-type": "image/png",
        "access-control-allow-origin": "*",
        etag: '"mock"',
        "last-modified": "Thu, 01 Jan 1970 00:00:00 GMT",
      });
      res.end(body);
      return;
    }
    const fixture = resolveFixture(url.pathname);
    if (!fixture) {
      res.writeHead(404, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "no fixture", path: url.pathname }));
      return;
    }
    const body = readFileSync(join(HERE, fixture));
    res.writeHead(200, {
      "content-type": "application/json",
      "access-control-allow-origin": "*",
      etag: '"mock"',
      "last-modified": "Thu, 01 Jan 1970 00:00:00 GMT",
    });
    res.end(body);
  });
  return new Promise((resolve) =>
    server.listen(port, "127.0.0.1", () => resolve(server)),
  );
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const port = Number(process.env.MOCK_PORT || 8099);
  startMock(port).then(() => console.log(`mock api on :${port}`));
}
